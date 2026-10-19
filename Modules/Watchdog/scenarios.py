import contextlib
import dataclasses
import math
import random

from Modules.Entity.Instrument.Flow_sensor.Flow_sensor import FlowSensor
from Modules.Entity.Instrument.Level_sensor.Level_sensor import LevelSensor
from Modules.Entity.Instrument.Pressure_sensor.Pressure_sensor import PressureSensor
from Modules.Entity.Instrument.Temperature_sensor.Temperature_sensor import TemperatureSensor
from Modules.Entity.Process_equipment.Boundary.boundary import Sink
from Modules.Entity.Process_equipment.HeatExchanger.heat_exchanger import HeatExchanger
from Modules.Entity.Process_equipment.Pipe.Pipe import Pipe
from Modules.Entity.Process_equipment.Pump.Pump import Pump
from Modules.Entity.Process_equipment.Valve.Valve import UtilityValve, Valve
from Modules.Entity.Process_equipment.Vessel.Reactor.CSTR.CSTR import ContinuousStirredTankReactor
from Modules.Entity.Process_equipment.Vessel.Reactor.PFR.PFR import PlugFlowReactor
from Modules.Entity.Process_equipment.Vessel.StorageTank.storage_tank import StorageTank
from Modules.Flow.chemistry import COMPONENTS
from Modules.Plant.environment import Environment
from Modules.Plant.plant_model import PlantModel
from Modules.Watchdog.access import OperatorView
from Modules.Watchdog.faults import random_profile
from Modules.Watchdog.recorder import SignalRecorder

# =====================================================================
# Reproducible generator of "bad days" for the pump trip predictor. Everything here is simulation side: it
# builds plants, injects faults and plays the operator and the control loops. The AI only ever sees the
# resulting operator-visible channels (and the evaluation-only TRUTH.* column that marks when a fault began).
#
# Variety is the point, because a model trained on a few fixed scenarios learns the scenarios, not the faults:
#
#   plant families   loop      tank -> pump -> valve -> line -> tank (valve on suction or discharge side)
#                    transfer  two tanks, pump one way, gravity return line the other way
#                    heated    the loop plus a heat exchanger and a temperature controller
#                    reactor   feed tank -> pump -> valve -> cooled CSTR (exothermic A + B -> C) -> drain, with a PI
#                              cooling-water loop and a high-high temperature interlock that trips the pump
#                    tubular   the same with a small-holdup jacketed PFR, which heats up much faster
#   failure modes    cavitation (throttled suction, hot liquid, intermittent), grid sag, line fouling,
#                    line blockage, tank leak, pump wear, a valve that seizes, heater runaway, loss of
#                    reactor cooling (cooling valve seizes), coolant-side fouling, warm cooling water,
#                    and two-fault compounds; plus fault-free "nuisance" days (operator changes, brownouts, noisy or
#                    frozen sensors, stiction) that must NOT raise alarms, and clean baselines
#   randomised       tank sizes, pump curve, valve size and speed, ambient, ageing speed, onset time, severity,
#                    fault shape (ramp / accelerating / sudden / saw-tooth / sporadic bursts), operator actions,
#                    flow and temperature control loops, and the quality of the instruments (noise, drift, lag,
#                    offset, spikes, freezes, valve stiction)
#
# A stop is only labelled if it happens. Faults that never reach a trip still give realistic non-stop data.
# =====================================================================

PUMP = "P101"
TARGET = f"{PUMP}.Fault"
FAMILIES = ("loop", "transfer", "heated", "reactor", "tubular")
REACTOR_FAMILIES = ("reactor", "tubular")
LAYOUTS = ("suction_control", "discharge_control")
FAILURE_MODES = ("suction_valve_cavitation", "hot_liquid_cavitation", "grid_voltage_drift", "line_fouling",
                 "line_blockage", "tank_leak", "pump_wear", "valve_stuck", "intermittent_cavitation",
                 "heater_runaway", "compound", "benign_transients", "sensor_nuisance", "baseline",
                 "cooling_loss", "coolant_fouling", "coolant_warm", "wall_scaling")
FAULT_FREE_MODES = ("benign_transients", "sensor_nuisance", "baseline")
GENERATOR_VERSION = 5
MODE_FAMILIES = {"heater_runaway": ("heated",), "hot_liquid_cavitation": ("loop", "transfer"),
                 "cooling_loss": REACTOR_FAMILIES, "coolant_fouling": REACTOR_FAMILIES,
                 "coolant_warm": REACTOR_FAMILIES, "wall_scaling": REACTOR_FAMILIES}
SUCTION_MODES = ("suction_valve_cavitation", "intermittent_cavitation")
VALVE_MODES = SUCTION_MODES + ("valve_stuck", "compound")     # fault programs that move or seize XV101
COMPOUND_PARTS = ("suction_throttle", "hot_tank", "line_fouling", "line_blockage", "tank_leak", "pump_wear",
                  "valve_stuck", "grid_sag")
REACTOR_COMPOUND_PARTS = ("cooling_loss", "coolant_fouling", "coolant_warm", "wall_scaling")
PROGRAM_OF_MODE = {"suction_valve_cavitation": "suction_throttle", "hot_liquid_cavitation": "hot_tank",
                   "grid_voltage_drift": "grid_sag", "intermittent_cavitation": "suction_throttle_bursts",
                   "heater_runaway": "runaway"}


def episode_plan(seed, failure_mode=None, family=None, layout=None):
    """The mode, plant family and valve layout of an episode. Deterministic in the seed."""
    mode = failure_mode or FAILURE_MODES[seed % len(FAILURE_MODES)]
    if mode not in FAILURE_MODES:
        raise ValueError(f"Unknown failure mode '{mode}'")
    if family is not None and family not in FAMILIES:
        raise ValueError(f"Unknown plant family '{family}'")
    rng = random.Random(seed * 7919 + 17)
    family = family or rng.choice(MODE_FAMILIES.get(mode, FAMILIES))
    layout = layout or ("suction_control" if mode in SUCTION_MODES else rng.choice(LAYOUTS))
    return {"mode": mode, "family": family, "layout": layout}


class _Probe(PressureSensor):
    """A pressure transmitter on a pump port rather than on the pump body."""

    def __init__(self, name, target, variable):
        self.variable = variable
        super().__init__(name, target)


# ---------------------------------------------------------------------------------------------- plant building
def _size_jacket(plant, seconds=240.0, dt=0.5):
    """Jacket UA that holds the reactor at its setpoint with the cooling valve at the plant's design opening.

    The reactor is pinned at its setpoint while the feed flows, the reaction heat is read, and the UA follows
    from the heat balance. This is design sizing on a throw-away build (the real one is rebuilt from scratch)."""
    reactor, sp = plant.equipment["R101"], plant.reactor_setpoint
    plant.equipment["XV210"].position = plant.equipment["XV210"].command = 1.0
    heat, count = 0.0, 0
    for i in range(int(seconds / dt)):
        reactor.contents.temperature = sp
        plant.step(dt)
        if i * dt > seconds / 2:
            heat += reactor.heat_released
            count += 1
    heat /= max(count, 1)
    ua = heat / (plant.jacket_fraction * max(sp - reactor.coolant_temp, 5.0))
    return min(80000.0, max(2000.0, ua))


def build_plant(seed, failure_mode=None, layout=None, family=None, _jacket_ua=None):
    """A randomised plant of the chosen family. Returns (plant, rng); the rng continues the episode's randomness."""
    plan = episode_plan(seed, failure_mode, family, layout)
    layout, family = plan["layout"], plan["family"]
    rng = random.Random(seed)
    plant = PlantModel()
    heated = family == "heated"
    reactive = family in REACTOR_FAMILIES
    if reactive:
        volume = rng.uniform(40000.0, 90000.0)          # a big feed tank: the reactor consumes it, it is not recycled
        feed = rng.uniform(0.03, 0.04) if family == "reactor" else rng.uniform(0.05, 0.065)
        composition = {"A": feed, "B": feed, "water": 1.0 - 2.0 * feed}
    else:
        volume = rng.uniform(400.0, 1500.0) if heated else rng.uniform(1500.0, 6000.0)
        composition = {"water": 1.0}
    plant.add_equipment(StorageTank("T101", inlets=["in"], outlets=["out"], max_capacity=volume * rng.uniform(1.4, 2.2),
                                    height=3.0, initial_volume=volume, composition=composition,
                                    temperature=rng.uniform(22.0, 45.0) if heated else rng.uniform(12.0, 32.0)))
    plant.add_equipment(Valve("XV101", inlets=["in"], outlets=["out"], rated_dp=rng.uniform(0.03, 0.08),
                              travel_time=rng.uniform(2.0, 12.0), initial_position=1.0))
    plant.add_equipment(Pump(PUMP, inlets=["in"], outlets=["out"],
                             shutoff_head=rng.uniform(2.6, 3.6), max_flow=rng.uniform(5.0, 7.0),
                             npsh_required=rng.uniform(0.3, 0.5),
                             cavitation_trip=math.exp(rng.uniform(math.log(40.0), math.log(600.0))),
                             low_flow_trip_time=rng.uniform(60.0, 300.0), low_flow_fraction=rng.uniform(0.25, 0.45),
                             vibration_trip=rng.uniform(2.5, 3.5)))
    plant.add_equipment(Pipe("L101", inlets=["in"], outlets=["out"], length=rng.uniform(3.0, 12.0)))
    if layout == "suction_control":
        chain = [("T101", "out", "XV101", "in"), ("XV101", "out", PUMP, "in"), (PUMP, "out", "L101", "in")]
    else:
        chain = [("T101", "out", PUMP, "in"), (PUMP, "out", "XV101", "in"), ("XV101", "out", "L101", "in")]
    last = "L101"
    if heated:
        plant.add_equipment(HeatExchanger("HX101", inlets=["in"], outlets=["out"], ua=rng.uniform(3000.0, 8000.0),
                                          utility_temp=rng.uniform(85.0, 110.0)))
        chain.append(("L101", "out", "HX101", "in"))
        last = "HX101"
    if family == "transfer":
        plant.add_equipment(StorageTank("T102", inlets=["in"], outlets=["out"], max_capacity=volume * 2.0, height=3.0,
                                        initial_volume=volume * rng.uniform(0.3, 0.7), composition={"water": 1.0},
                                        temperature=plant.equipment["T101"].contents.temperature))
        plant.add_equipment(Valve("XV102", inlets=["in"], outlets=["out"], rated_dp=0.03, travel_time=4.0,
                                  initial_position=1.0))
        plant.add_equipment(Pipe("L102", inlets=["in"], outlets=["out"], length=rng.uniform(3.0, 10.0)))
        chain += [("L101", "out", "T102", "in"), ("T102", "out", "XV102", "in"), ("XV102", "out", "L102", "in"),
                  ("L102", "out", "T101", "in")]
    elif reactive:
        cooling = plant.add_equipment(UtilityValve("XV210", fail_position=1.0, initial_position=0.5,
                                                   travel_time=rng.uniform(3.0, 10.0)))
        options = {"reactions": ["A_B_to_C"], "temperature": 20.0, "coolant_temp": rng.uniform(8.0, 18.0)}
        # Heat released goes with feed concentration squared, so the jacket is sized to the feed (see
        # _size_jacket): the design cooling-valve opening is a random 80-130 % of what the reaction heat alone needs,
        # because the throughput carries part of it away, so a healthy loop settles at roughly 30-60 % open.
        plant.reactor_setpoint, plant.jacket_fraction = rng.uniform(40.0, 48.0), rng.uniform(0.8, 1.3)
        ua = _jacket_ua if _jacket_ua is not None else 15000.0
        if family == "reactor":
            reactor = ContinuousStirredTankReactor("R101", inlets=["in1"], outlets=["out1"], max_capacity=1000.0,
                                                   height=3.0, initial_volume=rng.uniform(250.0, 350.0),
                                                   composition={"water": 1.0}, cooling_ua=ua, **options)
        else:
            reactor = PlugFlowReactor("R101", inlets=["in1"], outlets=["out1"], tube_volume=rng.uniform(250.0, 400.0),
                                      cooling_ua=ua, **options)
        plant.add_equipment(reactor).cooling_valve = cooling
        # A long, poorly insulated product line: its wall follows the weather, so a product that is less soluble
        # when cold slowly plates out on it (see the wall_scaling mode).
        plant.add_equipment(Pipe("L201", inlets=["in"], outlets=["out"], length=rng.uniform(15.0, 40.0),
                                 ambient_ua=rng.uniform(400.0, 900.0), fouling_rate_scale=0.0))
        plant.add_equipment(Valve("XV201", inlets=["in"], outlets=["out"], rated_dp=0.05, initial_position=1.0))
        plant.add_equipment(Sink("SK101", inlets=["in"]))
        chain += [("L101", "out", "R101", "in1"), ("R101", "out1", "L201", "in"), ("L201", "out", "XV201", "in"),
                  ("XV201", "out", "SK101", "in")]
    else:
        chain.append((last, "out", "T101", "in"))
    for a, ap, b, bp in chain:
        plant.connect(a, ap, b, bp)
    plant.equipment[PUMP].run_cmd = True
    pump, tank = plant.equipment[PUMP], plant.equipment["T101"]
    # The transmitters a plant like this would carry. Their own noise stays off: the OperatorView adds it.
    plant.add_instrument(FlowSensor("FT101", pump))
    plant.add_instrument(_Probe("PT101", pump, "p_in"))
    plant.add_instrument(_Probe("PT102", pump, "p_out"))
    plant.add_instrument(LevelSensor("LT101", tank))
    plant.add_instrument(TemperatureSensor("TT101", tank, range_min=0.0, range_max=150.0))
    if family == "transfer":
        plant.add_instrument(LevelSensor("LT102", plant.equipment["T102"]))
    if reactive:
        plant.add_instrument(TemperatureSensor("TT201", plant.equipment["R101"], range_min=0.0, range_max=150.0))
        plant.add_instrument(LevelSensor("LT201", plant.equipment["R101"]))
        plant.add_instrument(_Probe("PT201", plant.equipment["L201"], "p_in"))     # before and after the product
        plant.add_instrument(_Probe("PT202", plant.equipment["L201"], "p_out"))    # line: a plug raises one, drops the other
        if _jacket_ua is None:
            return build_plant(seed, failure_mode, layout, family, _jacket_ua=_size_jacket(plant))
    return plant, rng


# ---------------------------------------------------------------------------------------------- the rig
class _Pi:
    """Velocity-form PI controller: the PLC logic of the simulated plant."""

    def __init__(self, kp, ki, low, high, start):
        self.kp, self.ki, self.low, self.high, self.output, self.last_error = kp, ki, low, high, start, 0.0

    def update(self, error, dt):
        self.output += self.kp * (error - self.last_error) + self.ki * error * dt
        self.output = min(self.high, max(self.low, self.output))
        self.last_error = error
        return self.output


class _Injection:
    def __init__(self, name, profile, apply):
        self.name, self.profile, self.apply = name, profile, apply

    @property
    def onset(self):
        return self.profile.onset

    def tick(self, t):
        self.apply(self.profile(t))


class _Rig:
    """Everything that happens to the plant besides physics: fault programs, operator actions, controllers."""

    def __init__(self, plant, rng, plan, horizon):
        self.plant, self.rng, self.horizon = plant, rng, horizon
        self.mode, self.family, self.layout = plan["mode"], plan["family"], plan["layout"]
        self.valve, self.pump, self.tank = plant.equipment["XV101"], plant.equipment[PUMP], plant.equipment["T101"]
        self.heater = plant.equipment.get("HX101")
        self.base_offset = rng.choice([0.0, 0.0, -0.01, -0.02, -0.04])
        self.dips, self.offset_fault, self.runaway = [], 0.0, 0.0
        self.flow_factor, self.trim = 1.0, None
        self.base_temp = self.tank.contents.temperature
        self.base_utility = self.heater.utility_temp if self.heater else 0.0
        self.valve.stiction = rng.choice([0.0, 0.0, 0.01, 0.02, 0.04])
        self.vehicle = self.mode in VALVE_MODES             # a fault program owns the valve command
        self.flow_loop = self.layout == "discharge_control" and not self.vehicle and rng.random() < 0.5
        self.flow_sp = None
        self.flow_pi = _Pi(0.25, 0.004, 0.08, 1.0, 1.0)
        self.temp_pi = _Pi(2.0, 0.03, 0.0, 100.0, 50.0)
        self.temp_sp = rng.uniform(40.0, 65.0) if self.heater else 0.0
        self.reactor, self.cooling = plant.equipment.get("R101"), plant.equipment.get("XV210")
        if self.reactor is not None:
            self.base_coolant = self.reactor.coolant_temp
            self.rx_sp = plant.reactor_setpoint
            self.trip_temp = self.rx_sp + 20.0          # the high-high interlock that stops the feed pump
            self.rx_pi = _Pi(0.05, 0.0015, 0.0, 1.0, 0.5)
            self.cooling.stiction = rng.choice([0.0, 0.0, 0.01, 0.03])
        self.events = self._operator_events()
        self.injections = self._build_injections()

    # ------------------------------------------------------------ operator behaviour
    def _operator_events(self):
        rng = self.rng
        if self.mode == "baseline":
            return []
        busy = self.mode in ("benign_transients", "sensor_nuisance")
        count = rng.randint(3, 6) if busy else (rng.randint(1, 3) if rng.random() < 0.7 else 0)
        events = []
        for _ in range(count):
            kind = rng.choice(("speed", "speed", "brownout", "flow_setpoint", "trim"))
            events.append((rng.uniform(0.05, 0.9) * self.horizon, kind, rng.uniform(0.0, 1.0), rng.uniform(120.0, 1500.0)))
        return sorted(events)

    def _apply_events(self, t):
        speed, self.trim, self.flow_factor = 100.0, None, 1.0
        self.dips = []
        for start, kind, level, length in self.events:
            if not start <= t < start + length:
                continue
            if kind == "speed":
                speed = 65.0 + 35.0 * level
            elif kind == "brownout":
                self.dips.append(0.02 + 0.06 * level)
            elif kind == "flow_setpoint":
                self.flow_factor = 0.7 + 0.4 * level
            elif kind == "trim" and self.layout == "discharge_control" and not self.flow_loop and not self.vehicle:
                self.trim = 0.55 + 0.45 * level
        self.pump.speed_sp = speed

    # ------------------------------------------------------------ fault programs
    def _build_injections(self):
        if self.mode in FAULT_FREE_MODES:
            return []
        if self.mode == "compound":
            parts = REACTOR_COMPOUND_PARTS + ("grid_sag", "pump_wear", "line_fouling") if self.reactor is not None \
                else COMPOUND_PARTS
            return [self._injection(p, 0.9) for p in self.rng.sample(parts, 2)]
        return [self._injection(PROGRAM_OF_MODE.get(self.mode, self.mode), 1.0)]

    def _injection(self, name, scale):
        rng, plant = self.rng, self.plant

        def new(peak, kinds=None):
            low, high = peak[0] * scale, peak[1] * scale
            return random_profile(rng, self.horizon, (low, high), **({"kinds": kinds} if kinds else {}))

        if name in ("suction_throttle", "suction_throttle_bursts"):
            profile = new((0.78, 0.93), ("bursts",) if name.endswith("bursts") else None)
            return _Injection(name, profile, lambda v: setattr(self.valve, "command", 1.0 - v))
        if name == "hot_tank":
            profile = new((88.0 - self.base_temp, 105.0 - self.base_temp))
            return _Injection(name, profile, lambda v: setattr(
                self.tank.contents, "temperature", max(self.tank.contents.temperature, self.base_temp + v)))
        if name == "line_fouling":
            profile = new((0.7, 0.995), ("ramp", "exp", "sawtooth", "step"))
            return _Injection(name, profile, lambda v: setattr(plant.equipment["L101"], "fouling", v))
        if name == "line_blockage":
            profile = new((0.8, 0.995))
            return _Injection(name, profile, lambda v: setattr(plant.equipment["L101"], "blockage", v))
        if name == "tank_leak":
            volume = self.tank.contents.volume
            profile = new((volume / 6000.0, volume / 700.0))
            target = rng.choice([e for e in ("T101", "L101", "T102") if e in plant.equipment])
            return _Injection(name, profile, lambda v: setattr(plant.equipment[target], "leak_rate", v))
        if name == "pump_wear":
            profile = new((0.4, 1.0), ("ramp", "exp", "sawtooth", "bursts"))
            return _Injection(name, profile, lambda v: setattr(
                self.pump, "wear", v if profile.kind in ("sawtooth", "bursts") else max(self.pump.wear, v)))
        if name == "valve_stuck":
            profile = new((1.0, 1.0), ("step", "bursts"))
            position = rng.uniform(0.08, 0.7)

            def seize(v):
                if v > 0.5 and not self.valve.stuck:
                    self.valve.position = self.valve.command = position
                    self.valve.stuck = True
                elif v <= 0.5 and self.valve.stuck:
                    self.valve.stuck = False
            return _Injection(name, profile, seize)
        if name == "grid_sag":
            profile = new((0.24, 0.45), ("ramp", "exp", "step", "bursts"))
            return _Injection(name, profile, lambda v: setattr(self, "offset_fault", -v))
        if name == "runaway":
            profile = new((20.0, 60.0))
            return _Injection(name, profile, lambda v: setattr(self, "runaway", v))
        if name == "cooling_loss":
            profile = new((1.0, 1.0), ("step", "bursts"))
            position = rng.uniform(0.0, 0.12)

            def seize_cooling(v):
                if v > 0.5 and not self.cooling.stuck:
                    self.cooling.position = self.cooling.command = position
                    self.cooling.stuck = True
                elif v <= 0.5 and self.cooling.stuck:
                    self.cooling.stuck = False
            return _Injection(name, profile, seize_cooling)
        if name == "coolant_fouling":
            profile = new((0.8, 0.98), ("ramp", "exp", "sawtooth"))
            return _Injection(name, profile, lambda v: setattr(
                self.reactor, "fouling", v if profile.kind == "sawtooth" else max(self.reactor.fouling, v)))
        if name == "coolant_warm":
            profile = new((22.0, 42.0), ("ramp", "exp", "sawtooth"))
            return _Injection(name, profile, lambda v: setattr(self.reactor, "coolant_temp", self.base_coolant + v))
        if name == "wall_scaling":
            # The product dissolves less when cold, so it plates out on the weather-cooled product line a little
            # at a time (real chemistry in Pipe.slow_update). The profile is the rate: a colder season, a richer
            # batch. Pressure rises before the line, falls after it, and the flow sags; all of it gradual.
            profile = new((0.4, 1.0), ("ramp", "exp", "sawtooth"))
            peak = rng.uniform(0.003, 0.03)
            return _Injection(name, profile, lambda v: setattr(plant.equipment["L201"], "fouling_rate_scale", peak * v))
        raise ValueError(f"Unknown fault program '{name}'")

    @property
    def fault_onset(self):
        return min((i.onset for i in self.injections), default=math.inf)

    # ------------------------------------------------------------ one control tick (every sample)
    def tick(self, t, measured, dt):
        self._apply_events(t)
        for injection in self.injections:
            injection.tick(t)
        self.plant.power.offset = self.base_offset + self.offset_fault - sum(self.dips)
        if self.flow_loop:
            flow = measured.get("FT101.Value", 0.0)
            if self.flow_sp is None and t > 60.0:
                self.flow_sp = 0.75 * max(flow, 0.1)
            if self.flow_sp:
                sp = self.flow_sp * self.flow_factor
                self.valve.command = self.flow_pi.update((sp - flow) / sp, dt)
        elif self.trim is not None:
            self.valve.command = self.trim
        elif self.layout == "discharge_control" and not self.vehicle:
            self.valve.command = 1.0
        if self.heater is not None:
            temperature = measured.get("TT101.Value", self.base_temp)
            self.heater.utility_temp = self.base_utility + self.runaway
            if self.runaway > 0.05:
                self.heater.duty_command = 100.0
            else:
                self.heater.duty_command = self.temp_pi.update(self.temp_sp - temperature, dt)
        if self.reactor is not None:
            temperature = measured.get("TT201.Value", self.reactor.temperature)
            if not self.cooling.stuck:
                self.cooling.command = self.rx_pi.update(temperature - self.rx_sp, dt)
            if self.reactor.temperature > self.trip_temp:
                self.pump.tripped = True


def _instrument_quality(rng, mode):
    """Noise, drift, lag, calibration offset, spikes and freezes of this episode's transmitters."""
    quality = {"noise_fraction": rng.choice([0.003, 0.01, 0.02, 0.04]),
               "drift_fraction_per_hour": rng.choice([0.0, 0.002, 0.005]),
               "lag_seconds": rng.choice([0.0, 0.0, 2.0, 5.0, 15.0]),
               "offset_fraction": rng.choice([0.0, 0.01, 0.03]),
               "spike_probability": rng.choice([0.0, 0.0, 0.002, 0.01]),
               "freeze_per_hour": rng.choice([0.0, 0.0, 0.0, 0.2])}
    if mode == "sensor_nuisance":
        quality.update(spike_probability=rng.uniform(0.01, 0.03), freeze_per_hour=rng.uniform(1.0, 3.0),
                       offset_fraction=rng.uniform(0.03, 0.06))
    return quality


@contextlib.contextmanager
def _precipitating_product(plan_family, seed):
    """Make the reactor product (C) less soluble when cold for the duration of one run, then restore it.

    At the reactor's own temperature C stays dissolved; on a cold product line wall it is over-saturated. Only
    the reactor families, and only the wall_scaling program (which raises the line's deposition rate from
    zero), ever depend on this."""
    if plan_family not in REACTOR_FAMILIES:
        yield
        return
    original = COMPONENTS["C"]
    rng = random.Random(seed * 131 + 7)
    COMPONENTS["C"] = dataclasses.replace(original, attributes={
        **original.attributes, "solubility_25c": rng.uniform(0.7, 1.1), "solubility_slope": 0.08})
    try:
        yield
    finally:
        COMPONENTS["C"] = original


def run_episode(seed, max_seconds=7200.0, dt=0.5, sample_period=5.0, failure_mode=None, layout=None,
                noise_fraction=None, drift_fraction_per_hour=None, family=None):
    """Run one seeded operating day and return (times, matrix, names) of operator-visible channels.

    Training labels come from the PLC-visible trip feedback (`P101.Fault`); the model inputs are the other
    PLC feedback channels as the transmitters report them. The TRUTH.FaultActive column (0 until the first
    fault program starts) is for evaluation only and is never a model input. Pass `noise_fraction` or
    `drift_fraction_per_hour` to pin those instead of randomising them.
    """
    plan = episode_plan(seed, failure_mode, family, layout)
    with _precipitating_product(plan["family"], seed):
        return _run_episode(seed, plan, max_seconds, dt, sample_period, noise_fraction, drift_fraction_per_hour)


def _run_episode(seed, plan, max_seconds, dt, sample_period, noise_fraction, drift_fraction_per_hour):
    plant, rng = build_plant(seed, plan["mode"], plan["layout"], plan["family"])
    plant.environment = Environment(mean_c=rng.uniform(10.0, 35.0))
    plant.slow_time_scale = rng.uniform(500.0, 4000.0)
    power = plant.power
    power.seed, power._rng = seed, random.Random(seed)
    power.noise = rng.choice([0.005, 0.01, 0.03])
    rig = _Rig(plant, rng, plan, max_seconds)
    quality = _instrument_quality(rng, plan["mode"])
    if noise_fraction is not None:
        quality["noise_fraction"] = noise_fraction
    if drift_fraction_per_hour is not None:
        quality["drift_fraction_per_hour"] = drift_fraction_per_hour

    recorder = SignalRecorder(sample_period)
    view = OperatorView(plant, seed=seed, **quality)
    every = max(1, int(round(sample_period / dt)))
    stop_seen_at = None
    for step in range(1, int(max_seconds / dt) + 1):
        plant.step(dt)
        if step % every:
            continue
        values = view.feedback()
        values["TRUTH.FaultActive"] = float(plant.time >= rig.fault_onset)
        recorder.record(plant.time, values)
        rig.tick(plant.time, values, every * dt)
        if values.get(TARGET, 0.0) > 0.5 and stop_seen_at is None:
            stop_seen_at = plant.time
        if stop_seen_at is not None and plant.time - stop_seen_at > 2 * sample_period:
            break
    return recorder.arrays()


# ---------------------------------------------------------------------------------------------- campaigns
class _Shifted:
    """A fault profile that starts `start` seconds into a long run instead of at time zero."""

    def __init__(self, profile, start):
        self.profile, self.start = profile, start
        self.kind, self.onset = profile.kind, profile.onset + start

    def __call__(self, t):
        return self.profile(t - self.start)


def _incident_programs(family, layout):
    modes = [m for m in FAILURE_MODES if m not in FAULT_FREE_MODES and m != "compound"
             and family in MODE_FAMILIES.get(m, FAMILIES) and not (m in SUCTION_MODES and layout != "suction_control")]
    return [PROGRAM_OF_MODE.get(m, m) for m in modes]


def _operator_fix(rig, plant, tank_material):
    """The operator's repair after an incident: maintenance on everything, injected conditions removed and (as after
    a delivery or a batch change) the feed tank refilled and brought back to its normal state. The pump is
    restarted by the campaign loop, once it is safe."""
    plant.repair_all()
    if "L201" in plant.equipment:
        plant.equipment["L201"].fouling_rate_scale = 0.0
    rig.injections, rig.offset_fault, rig.runaway = [], 0.0, 0.0
    rig.valve.command = 1.0
    if rig.heater is not None:
        rig.heater.utility_temp = rig.base_utility
    if rig.reactor is not None:
        rig.reactor.coolant_temp = rig.base_coolant
    rig.tank.contents = tank_material()


def run_campaign(seed, hours=6.0, sample_period=5.0, dt=0.5, family=None, mean_gap=2400.0, log=None):
    """Run a long multi-incident "training plant" (see `_run_campaign`), with the cold-wall product chemistry
    applied for reactor families."""
    family = family or random.Random(seed * 31 + 5).choice(FAMILIES)
    with _precipitating_product(family, seed):
        return _run_campaign(seed, hours, sample_period, dt, family, mean_gap, log)


def _run_campaign(seed, hours, sample_period, dt, family, mean_gap, log):
    """A long "training plant": one plant that lives through several incidents instead of a single bad day.

    Incidents (any fault program valid for the plant family) arrive at random times, each with its own random
    shape and severity. A trip is followed by a short outage, then the operator repairs the plant, resets the
    pump and the plant resumes; an incident that never trips is repaired when its window ends. Between incidents
    the operator changes speed, brownouts happen and the instruments misbehave as in any benign day. Ambient
    temperature follows a day/night cycle (plus a slow seasonal offset) that spans several days in the run, so
    heat losses, cooling water and tank temperatures all drift with the weather.

    Returns (times, matrix, names) like run_episode, with TRUTH.FaultActive = 1 while an incident is active.
    `log`, if a list, receives one dict per incident: program, start, end, tripped_at (None if it never tripped)."""
    horizon = hours * 3600.0
    plan = episode_plan(seed, "benign_transients", family)
    plant, rng = build_plant(seed, plan["mode"], plan["layout"], plan["family"])
    plant.slow_time_scale = rng.uniform(500.0, 4000.0)
    days = rng.uniform(2.0, 6.0)                       # day/night cycles in the run
    plant.environment = Environment(mean_c=rng.uniform(5.0, 30.0), daily_amp=rng.uniform(3.0, 9.0),
                                    annual_amp=rng.uniform(4.0, 12.0), start_day=rng.uniform(0.0, 365.0),
                                    start_hour=rng.uniform(0.0, 24.0),
                                    day_length=horizon * plant.slow_time_scale / days)
    power = plant.power
    power.seed, power._rng = seed, random.Random(seed)
    power.noise = rng.choice([0.005, 0.01, 0.03])
    rig = _Rig(plant, rng, plan, horizon)
    rig.vehicle, rig.flow_loop = True, False          # the fault programs, not a controller, own the valve command
    tank = rig.tank
    full = tank.contents.copy()
    refill = full.copy
    programs = _incident_programs(plan["family"], plan["layout"])

    schedule, t = [], rng.uniform(600.0, 1800.0)
    while t < horizon - 900.0:
        span = rng.uniform(1200.0, 4500.0)
        schedule.append((t, min(span, horizon - t)))
        t += span + 600.0 + rng.expovariate(1.0 / mean_gap)

    quality = _instrument_quality(rng, "benign_transients")
    recorder = SignalRecorder(sample_period)
    view = OperatorView(plant, seed=seed, **quality)
    every = max(1, int(round(sample_period / dt)))
    incident, outage = None, rng.uniform(120.0, 400.0)
    for step in range(1, int(horizon / dt) + 1):
        plant.step(dt)
        if step % every:
            continue
        now = plant.time
        if incident is None and schedule and now >= schedule[0][0]:
            start, span = schedule.pop(0)
            rig.horizon = span
            injection = rig._injection(rng.choice(programs), 1.0)
            injection.profile = _Shifted(injection.profile, start)
            rig.injections = [injection]
            incident = {"program": injection.name, "start": start, "end": start + span, "tripped_at": None}
        values = view.feedback()
        values["TRUTH.FaultActive"] = float(incident is not None and now >= rig.fault_onset)
        recorder.record(now, values)
        rig.tick(now, values, every * dt)
        if incident is not None:
            if incident["tripped_at"] is None and values.get(TARGET, 0.0) > 0.5:
                incident["tripped_at"] = now
            tripped = incident["tripped_at"] is not None and now - incident["tripped_at"] > outage
            if tripped or now >= incident["end"]:
                incident["end"] = now
                if log is not None:
                    log.append(incident)
                _operator_fix(rig, plant, refill)
                incident, outage = None, rng.uniform(120.0, 400.0)
        elif rig.tank.contents.volume < 0.3 * full.volume:
            rig.tank.contents = refill()               # a delivery tops up a feed tank that is being consumed
        if incident is None and rig.pump.tripped and (rig.reactor is None or rig.reactor.temperature < rig.trip_temp - 5.0):
            rig.pump._reset(True)                      # the operator restarts once a hot reactor has cooled down
    if incident is not None and log is not None:
        incident["end"] = plant.time
        log.append(incident)
    return recorder.arrays()
