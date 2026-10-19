import math

from Modules.Entity.Entity import Signal, TO_PLC, FROM_PLC
from Modules.Entity.Process_equipment.process_equipment import ProcessEquipment, P_ATM
from Modules.Entity.Process_equipment.Motor.motor import Motor
from Modules.Entity.Process_equipment.transfer import Transfer
from Modules.Entity.Process_equipment.Valve.Valve import ActuatedMixin
from Modules.Flow.chemistry import COMPONENTS, R_GAS
from Modules.Flow.flow import Gas, gas_pressure_factor

# =====================================================================
# GAS NETWORK EQUIPMENT
#
# Gas lives in its own network next to the liquid one (solved in Modules/Plant/gas_network.py).
#   NODES (hold or fix a pressure):  GasVolume (receiver, header, tank headspace), GasSource, GasSink,
#                                    and FlashDrum's vapor ports.
#   ELEMENTS (between two nodes):    GasPipe, GasValve, GasCheckValve, Compressor.
# There are no gas junctions: use a GasVolume as a manifold. A node connected to several lines
# is simply a volume they all feed or draw from.
#
# Units: bar (absolute), mol/s, J, degrees C. Flow through a line element follows an isothermal
# compressible law:   P_in^2 - P_out^2 = R * Q^2     (R in bar^2/(mol/s)^2, summed over the elements in series)
# so flow rises with the square root of the pressure-squared difference and cannot exceed what the
# volumes can equalize in one step. Choked (sonic) flow, Joule-Thomson cooling and condensation inside
# lines are not modelled.
# Flow sensors on gas elements read mol/s: give them a range, e.g. range_max=50.
# =====================================================================

GAS_MOLAR_MASS = 0.029      # kg/mol, used only to size the line resistances (air-like)
GAS_LINE_TEMP_K = 300.0
RATED_P_IN, RATED_P_OUT = 5.0, 1.0      # bar: the pressures at which a gas valve's max_flow is quoted


def _pipe_resistance(k_total, diameter):
    """bar^2/(mol/s)^2 for a velocity-head loss coefficient K in a pipe of this diameter."""
    area = math.pi * diameter ** 2 / 4.0
    return k_total * GAS_MOLAR_MASS * R_GAS * GAS_LINE_TEMP_K / area ** 2 / 1e10


# ---------------------------------------------------------------- nodes
class GasHolder:
    """Mixin for anything that holds gas in a fixed volume (self.gas). It supplies the node protocol the
    gas solver uses: pressure, a sample of the gas, how much can be drawn, and a buffer size.
    Subclasses implement gas_volume_l()."""
    gas = None

    def gas_volume_l(self):
        raise NotImplementedError

    @property
    def gas_pressure_bar(self):
        return self.gas.pressure(self.gas_volume_l())

    def gas_pressure(self, port):
        return self.gas_pressure_bar

    def gas_stream(self, port):
        return self.gas.copy()

    def gas_inventory(self):
        """mol available to draw"""
        return self.gas.total_moles

    def gas_buffer(self):
        """mol per bar: how much gas it takes to change the pressure by 1 bar"""
        return self.gas_volume_l() / gas_pressure_factor(self.gas.temperature)

    def gas_draw(self, gas):
        self.gas.remove(gas)

    def gas_receive(self, gas):
        self.gas.add(gas)

    def can_supply_gas(self, port):
        return bool(self.status)

    def can_accept_gas(self, port):
        return bool(self.status)


class GasVolume(GasHolder, ProcessEquipment):
    """A closed gas volume: receiver, buffer tank or header. Pressure = n R T / V.
    ambient_ua (W/K) lets the gas drift to the outdoor temperature (slow clock), which moves the pressure.
    relief_pressure (bar) is a safety valve that vents the excess to atmosphere (counted in `vented`)."""
    domain = "gas"
    FAULTS = ("leak_rate",)

    def __init__(self, name, description="", status=1, inlets=None, outlets=None,
                 volume=1000.0, temperature=25.0, initial_pressure=P_ATM, composition=None,
                 ambient_ua=0.0, relief_pressure=None):
        super().__init__(name, description, status, inlets or ["in"], outlets or ["out"])
        if composition is None:
            if "N2" not in COMPONENTS:
                raise ValueError(f"{name}: give a gas composition, e.g. {{'N2': 1.0}}")
            composition = {"N2": 1.0}
        self.volume = volume                    # L
        self.ambient_ua = ambient_ua
        self.relief_pressure = relief_pressure
        self.gas = Gas.at_pressure(composition, initial_pressure, volume, temperature)
        self.relieving = False
        self.vented = Gas({}, temperature)      # everything the relief valve and a leak have let out

    def gas_volume_l(self):
        return self.volume

    @property
    def pressure(self):
        return self.gas_pressure_bar

    @property
    def temperature(self):
        return self.gas.temperature

    @temperature.setter
    def temperature(self, value):
        self.gas.temperature = value

    def port_pressure(self, port):
        return self.pressure

    def run_step(self, dt):
        if self.leak_rate > 0 and self.gas.total_moles > 0:
            lost = self.gas.scaled_to_moles(min(self.leak_rate * dt, self.gas.total_moles))
            self.gas.remove(lost)
            self.vented.add(lost)
        self.relieving = False
        if self.relief_pressure is not None and self.pressure > self.relief_pressure:
            excess = (self.pressure - self.relief_pressure) * self.gas_buffer()
            lost = self.gas.scaled_to_moles(min(excess, self.gas.total_moles))
            self.gas.remove(lost)
            self.vented.add(lost)
            self.relieving = True

    def slow_update(self, dt, ambient):
        cp = self.gas.heat_capacity
        if self.ambient_ua > 0 and cp > 0:
            self.temperature = ambient + (self.temperature - ambient) * math.exp(-self.ambient_ua * dt / cp)

    def get_state(self):
        return {"gas": self.gas.to_state(), "vented": self.vented.to_state()}

    def set_state(self, state):
        self.gas = Gas.from_state(state["gas"])
        self.vented = Gas.from_state(state["vented"])


class GasSource(ProcessEquipment):
    """Gas supply at a fixed pressure (plant air, nitrogen header, a gas well, ...)."""
    domain = "gas"

    def __init__(self, name, description="", status=1, inlets=None, outlets=None,
                 composition=None, temperature=25.0, supply_pressure=7.0):
        super().__init__(name, description, status, [], outlets or ["out"])
        if composition is None:
            composition = {"N2": 1.0}
        self.composition = dict(composition)
        self.temperature = temperature
        self.supply_pressure = supply_pressure
        self.supplied = Gas({}, temperature)

    @property
    def pressure(self):
        return self.supply_pressure

    def port_pressure(self, port):
        return self.supply_pressure

    def gas_pressure(self, port):
        return self.supply_pressure

    def gas_stream(self, port):
        return Gas.from_fractions(self.composition, 1.0, self.temperature)

    def gas_inventory(self):
        return float("inf")

    def gas_buffer(self):
        return float("inf")

    def gas_draw(self, gas):
        self.supplied.add(gas)

    def gas_receive(self, gas):
        pass

    def can_supply_gas(self, port):
        return bool(self.status)

    def can_accept_gas(self, port):
        return False

    def get_state(self):
        return {"supplied": self.supplied.to_state()}

    def set_state(self, state):
        self.supplied = Gas.from_state(state["supplied"])


class GasSink(ProcessEquipment):
    """A vent, flare or downstream consumer at a fixed pressure."""
    domain = "gas"

    def __init__(self, name, description="", status=1, inlets=None, outlets=None, sink_pressure=P_ATM):
        super().__init__(name, description, status, inlets or ["in"], [])
        self.sink_pressure = sink_pressure
        self.received = Gas({}, 25.0)

    @property
    def pressure(self):
        return self.sink_pressure

    def port_pressure(self, port):
        return self.sink_pressure

    def gas_pressure(self, port):
        return self.sink_pressure

    def gas_stream(self, port):
        return Gas({}, 25.0)

    def gas_inventory(self):
        return 0.0

    def gas_buffer(self):
        return float("inf")

    def gas_draw(self, gas):
        pass

    def gas_receive(self, gas):
        self.received.add(gas)

    def can_supply_gas(self, port):
        return False

    def can_accept_gas(self, port):
        return bool(self.status)

    def get_state(self):
        return {"received": self.received.to_state()}

    def set_state(self, state):
        self.received = Gas.from_state(state["received"])


# ---------------------------------------------------------------- line elements
class GasTransfer(Transfer):
    """One inlet, one outlet, no holdup, in the gas network. Subclasses describe themselves through
    gas_resistance() (bar^2/(mol/s)^2) and is_blocked()."""
    domain = "gas"
    is_compressor = False
    FAULTS = ("leak_rate",)

    def gas_resistance(self):
        return 0.0

    def condition(self, gas, rate):
        """Called with the gas moved through this element in one step (and its rate, mol/s), in flow
        order. Elements that change the gas (a compressor heating it) modify it in place."""


class GasPipe(GasTransfer):
    """Straight gas line. K = friction * length / diameter + fitting_k. `blockage` (0..1, a fault) closes the flow area."""
    FAULTS = ("leak_rate", "blockage")

    def __init__(self, name, description="", status=1, inlets=None, outlets=None,
                 diameter=0.05, length=5.0, friction_factor=0.02, fitting_k=0.0):
        super().__init__(name, description, status, inlets, outlets)
        self.diameter = diameter
        self.length = length
        self.friction_factor = friction_factor
        self.fitting_k = fitting_k
        self.blockage = 0.0

    def is_blocked(self):
        return self.blockage >= 0.99

    def gas_resistance(self):
        k = self.friction_factor * self.length / self.diameter + self.fitting_k
        return _pipe_resistance(k, self.diameter) / max(1.0 - self.blockage, 1e-3) ** 2

    def repair(self):
        super().repair()
        self.blockage = 0.0

    def get_state(self):
        return {"blockage": float(self.blockage)}

    def set_state(self, state):
        self.blockage = state["blockage"]


class GasValve(ActuatedMixin, GasTransfer):
    """Control/on-off valve in a gas line. max_flow (mol/s) is the flow when fully open between
    5 bar and 1 bar absolute."""
    FAULTS = ("leak_rate", "stuck")

    def __init__(self, name, description="", status=1, inlets=None, outlets=None,
                 max_flow=20.0, travel_time=3.0, fail_position=0.0, initial_position=0.0):
        super().__init__(name, description, status, inlets, outlets)
        self.max_flow = max_flow
        self._init_actuator(travel_time, fail_position, initial_position)

    def is_blocked(self):
        return not self.status or self.position <= 0.005

    def gas_resistance(self):
        pos = max(self.position, 0.005)
        return (RATED_P_IN ** 2 - RATED_P_OUT ** 2) / self.max_flow ** 2 / pos ** 2

    def advance(self, dt):
        self._move(dt)

    def signals(self):
        return self._actuator_signals()

    def get_state(self):
        return self._actuator_state()

    def set_state(self, state):
        self._set_actuator_state(state)


class GasCheckValve(GasTransfer):
    """Lets gas through in one direction only."""
    one_way = True

    def __init__(self, name, description="", status=1, inlets=None, outlets=None, max_flow=20.0):
        super().__init__(name, description, status, inlets, outlets)
        self.max_flow = max_flow

    def gas_resistance(self):
        return 0.05 * (RATED_P_IN ** 2 - RATED_P_OUT ** 2) / self.max_flow ** 2


# ---------------------------------------------------------------- compressor
COMPRESSOR_WEAR_FLOW_LOSS = 0.4     # capacity lost when completely worn
COMPRESSOR_WEAR_RATIO_LOSS = 0.3
SURGE_LIFE = 3.6e3                  # s of continuous surge that wear a compressor out (1 h)
OVERTEMP_LIFE = 1.8e4               # s of running above the discharge temperature limit (5 h)


class Compressor(GasTransfer):
    """Variable-speed centrifugal-style compressor.

    At speed n (0..1) it can deliver up to max_flow * n (mol/s) and reaches a pressure ratio of
    1 + (max_ratio - 1) * n^2 at zero flow, following a falling characteristic in between:
        ratio(Q) = 1 + (ratio_at_zero_flow - 1) * (1 - (Q / Q_max)^2)
    so the flow settles where the characteristic meets what the downstream side needs.
    Discharge temperature follows the isentropic compression with `efficiency`; the work is reported as Power.

    Protection and abuse:
      * surge: running below surge_fraction of the speed-scaled max_flow (typically a closed or blocked
        discharge). It wears the machine and trips it after `surge_trip` seconds of surge.
      * over-temperature: a discharge temperature above max_discharge_temp trips it immediately and wears it.
      * wear (0..1) cuts capacity by up to 40 % and pressure ratio by 30 %, set by repair() or set_fault(tag, "wear", 0).
    A trip stays until the PLC sends ResetCmd. A stopped compressor blocks the line (like a pump).
    """
    is_compressor = True
    one_way = True
    FAULTS = ("leak_rate", "wear")

    def __init__(self, name, description="", status=1, inlets=None, outlets=None,
                 max_flow=10.0, max_ratio=4.0, efficiency=0.75, gamma=1.4, ramp_time=3.0,
                 surge_fraction=0.25, surge_trip=10.0, max_discharge_temp=180.0, rated_kw=30.0):
        super().__init__(name, description, status, inlets, outlets)
        self.motor = Motor(rated_kw)    # electrical drive, see Motor/motor.py (feels the plant's PowerSupply)
        self.max_flow = max_flow
        self.max_ratio = max_ratio
        self.efficiency = efficiency
        self.gamma = gamma
        self.ramp_time = ramp_time
        self.surge_fraction = surge_fraction
        self.surge_trip = surge_trip
        self.max_discharge_temp = max_discharge_temp
        self.run_cmd = False
        self.speed_sp = 100.0       # %
        self._last_speed_written = 0.0
        self.speed = 0.0            # % actual
        self.tripped = False
        self.wear = 0.0
        self.surge = False
        self.surge_time = 0.0
        self.pressure_ratio = 1.0
        self.power = 0.0            # W of gas work during the last step
        self.discharge_temp = 25.0

    @property
    def running(self):
        return bool(self.status) and self.speed > 1.0 and not self.tripped

    def is_blocked(self):
        return not self.status or self.speed < 1.0 or self.tripped

    # ---- characteristic (used by the gas solver)
    def capacity(self):
        """mol/s at zero pressure ratio"""
        return self.max_flow * self.speed / 100.0 * (1.0 - COMPRESSOR_WEAR_FLOW_LOSS * self.wear)

    def shutoff_ratio(self):
        return 1.0 + (self.max_ratio - 1.0) * (self.speed / 100.0) ** 2 * (1.0 - COMPRESSOR_WEAR_RATIO_LOSS * self.wear)

    def ratio_at(self, q):
        cap = self.capacity()
        if cap <= 0 or q >= cap:
            return 1.0
        return 1.0 + (self.shutoff_ratio() - 1.0) * (1.0 - (q / cap) ** 2)

    def advance(self, dt):
        target = self.speed_sp * self.motor.speed_factor() if self.status and self.run_cmd and not self.tripped else 0.0
        step = 100.0 * dt / self.ramp_time if self.ramp_time > 0 else 100.0
        self.speed += max(-step, min(step, target - self.speed))

    def condition(self, gas, rate):
        """Heat the gas by the compression (isentropic temperature rise divided by the efficiency)."""
        self.power = 0.0
        if rate <= 0 or gas.total_moles <= 0:
            return
        ratio = max(self.p_out / self.p_in, 1.0) if self.p_in > 0 else 1.0
        self.pressure_ratio = ratio
        eff = max(self.efficiency * (1.0 - COMPRESSOR_WEAR_RATIO_LOSS * self.wear), 0.05)
        t_in_k = gas.temperature + 273.15
        rise = t_in_k * (ratio ** ((self.gamma - 1.0) / self.gamma) - 1.0) / eff
        cp_mean = gas.heat_capacity / gas.total_moles
        gas.temperature += rise
        self.discharge_temp = gas.temperature
        self.power = rate * cp_mean * rise

    def after_step(self, dt, starved, fluid=None):
        minimum = self.surge_fraction * self.max_flow * self.speed / 100.0
        self.surge = self.running and self.flow < minimum
        if self.surge:
            self.surge_time += dt
            if self.surge_time >= self.surge_trip:
                self.tripped = True
        else:
            self.surge_time = max(0.0, self.surge_time - dt)
        if not self.running or abs(self.flow) < 1e-9:
            self.power = 0.0
        elif self.discharge_temp > self.max_discharge_temp:
            self.tripped = True
        # Shaft power = gas work / efficiency, plus friction that grows with speed; the motor turns it into amps and heat.
        n = self.speed / 100.0
        shaft = self.power / max(self.efficiency, 0.05)
        load = 0.2 * n + shaft / (self.motor.rated_kw * 1000.0)
        vibration = 4.0 * self.wear + (3.0 if self.surge else 0.0)
        if self.motor.update(dt, load, self.running, vibration):
            self.tripped = True

    def slow_update(self, dt, ambient):
        self.motor.slow_update(dt, ambient)
        rate = (1.0 / SURGE_LIFE) if self.surge else 0.0
        if self.running and self.discharge_temp > self.max_discharge_temp:
            rate += 1.0 / OVERTEMP_LIFE
        self.wear = min(1.0, self.wear + rate * dt)

    def repair(self):
        super().repair()
        self.wear = 0.0
        self.surge_time = 0.0
        self.motor.repair()

    def _write_speed(self, value):
        """Same rule as the pump: an untouched PLC variable reads 0 and must not count as a command."""
        if value != self._last_speed_written:
            self._last_speed_written = value
            self.speed_sp = min(100.0, max(0.0, value))

    def _reset(self, value):
        if value and self.motor.reset():     # a burnt-out motor stays tripped
            self.tripped = False
            self.surge_time = 0.0

    def signals(self):
        return [
            Signal("RunCmd", FROM_PLC, "BOOL", get=lambda: self.run_cmd, set=lambda v: setattr(self, "run_cmd", v)),
            Signal("SpeedSP", FROM_PLC, "REAL", "%", get=lambda: self.speed_sp, set=self._write_speed),
            Signal("ResetCmd", FROM_PLC, "BOOL", get=lambda: False, set=self._reset),
            Signal("RunningFb", TO_PLC, "BOOL", get=lambda: self.running),
            Signal("Fault", TO_PLC, "BOOL", get=lambda: self.tripped),
            Signal("Surge", TO_PLC, "BOOL", get=lambda: self.surge),
            Signal("Speed", TO_PLC, "REAL", "%", get=lambda: self.speed),
            Signal("Power", TO_PLC, "REAL", "kW", get=lambda: self.power / 1000.0),
            Signal("DischargeTemp", TO_PLC, "REAL", "C", get=lambda: self.discharge_temp),
            Signal("Current", TO_PLC, "REAL", "A", get=lambda: self.motor.current),
            Signal("WindingTemp", TO_PLC, "REAL", "C", get=lambda: self.motor.winding_temp),
            Signal("Voltage", TO_PLC, "REAL", "V", get=lambda: self.motor.voltage),
            Signal("Vibration", TO_PLC, "REAL", "mm/s", get=lambda: self.motor.vibration),
        ]

    def get_state(self):
        return {"run_cmd": bool(self.run_cmd), "speed_sp": float(self.speed_sp), "speed": float(self.speed),
                "tripped": bool(self.tripped), "wear": float(self.wear), "surge_time": float(self.surge_time),
                "discharge_temp": float(self.discharge_temp), "motor": self.motor.get_state()}

    def set_state(self, state):
        self.run_cmd, self.speed_sp, self.speed = state["run_cmd"], state["speed_sp"], state["speed"]
        self.tripped = state["tripped"]
        self.wear = state.get("wear", 0.0)
        self.surge_time = state.get("surge_time", 0.0)
        self.discharge_temp = state.get("discharge_temp", 25.0)
        self.motor.set_state(state.get("motor", {}))
        self._last_speed_written = 0.0

    def apply_failsafe(self):
        self.run_cmd = False
