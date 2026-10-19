import random

from Modules.Entity.Process_equipment.Pipe.Pipe import Pipe
from Modules.Entity.Process_equipment.Pump.Pump import Pump
from Modules.Entity.Process_equipment.Valve.Valve import Valve
from Modules.Entity.Process_equipment.Vessel.StorageTank.storage_tank import StorageTank
from Modules.Plant.environment import Environment
from Modules.Plant.plant_model import PlantModel
from Modules.Watchdog.access import OperatorView
from Modules.Watchdog.recorder import SignalRecorder

# =====================================================================
# Reproducible supervised-data generator for a pump trip predictor. It alternates suction/discharge valve
# layouts and varies cavitation, steady grid sag, voltage drift, motor age, ambient temperature and healthy
# baseline conditions. The recorder captures PLC feedback with seeded measurement noise and drift; fault bits
# supply training labels but are removed from model features. Unannounced scheduled grid dips are not included
# because there is no pre-event signal from which a predictor could learn.
# =====================================================================

PUMP = "P101"
TARGET = f"{PUMP}.Fault"
FAILURE_MODES = ("suction_valve_cavitation", "hot_liquid_cavitation", "grid_voltage_drift", "baseline")
LAYOUTS = ("suction_control", "discharge_control")


def build_plant(seed, failure_mode=None, layout=None):
    rng = random.Random(seed)
    plant = PlantModel()
    plant.add_equipment(StorageTank("T101", inlets=["in"], outlets=["out"], max_capacity=20000.0, height=3.0,
                                    initial_volume=15000.0, composition={"water": 1.0}, temperature=20.0))
    failure_mode = failure_mode or FAILURE_MODES[seed % len(FAILURE_MODES)]
    layout = layout or ("suction_control" if failure_mode == "suction_valve_cavitation"
                        else LAYOUTS[seed % len(LAYOUTS)])
    throttle = failure_mode == "suction_valve_cavitation"
    plant.add_equipment(Valve("XV101", inlets=["in"], outlets=["out"], rated_dp=0.05,
                              initial_position=rng.uniform(0.11, 0.14) if throttle else 1.0))
    plant.add_equipment(Pump(PUMP, inlets=["in"], outlets=["out"],
                             npsh_required=0.5 if failure_mode == "hot_liquid_cavitation" else 0.3,
                             cavitation_trip=rng.uniform(450.0, 900.0)
                             if failure_mode in ("suction_valve_cavitation", "hot_liquid_cavitation")
                             else 10.0))
    plant.add_equipment(Pipe("L101", inlets=["in"], outlets=["out"]))
    if layout == "suction_control":
        connections = (("T101", "out", "XV101", "in"), ("XV101", "out", PUMP, "in"),
                       (PUMP, "out", "L101", "in"), ("L101", "out", "T101", "in"))
    else:
        connections = (("T101", "out", PUMP, "in"), (PUMP, "out", "XV101", "in"),
                       ("XV101", "out", "L101", "in"), ("L101", "out", "T101", "in"))
    for a, ap, b, bp in connections:
        plant.connect(a, ap, b, bp)
    plant.equipment["XV101"].command = 1.0 if not throttle else plant.equipment["XV101"].position
    plant.equipment[PUMP].run_cmd = True
    if failure_mode == "hot_liquid_cavitation":
        plant.equipment["T101"].contents.temperature = rng.uniform(92.0, 98.0)
    return plant, rng


def run_episode(seed, max_seconds=7200.0, dt=0.5, sample_period=5.0, failure_mode=None, layout=None,
                noise_fraction=0.01, drift_fraction_per_hour=0.002):
    """Run a seeded operating regime on one of two layouts.

    Training labels come from the PLC-visible trip feedback; the model inputs are the other PLC feedback
    channels, with simulated measurement noise and drift. No degradation variables enter the input dataset.
    """
    failure_mode = failure_mode or FAILURE_MODES[seed % len(FAILURE_MODES)]
    layout = layout or ("suction_control" if failure_mode == "suction_valve_cavitation"
                        else LAYOUTS[seed % len(LAYOUTS)])
    plant, rng = build_plant(seed, failure_mode, layout)
    ambient = rng.uniform(10.0, 35.0)
    plant.environment = Environment(mean_c=ambient)
    plant.slow_time_scale = rng.uniform(500.0, 4000.0)
    power = plant.power
    power.seed, power._rng = seed, random.Random(seed)
    power.noise = rng.choice([0.005, 0.01, 0.03])
    base_offset = rng.choice([0.0, -0.02, -0.04]) if failure_mode == "baseline" else \
        rng.choice([0.0, -0.02, -0.04, -0.08, -0.1, -0.12])
    drift_per_hour = 0.0 if failure_mode == "baseline" else rng.choice([0.0, 0.0, -0.02, -0.04, -0.06])
    if failure_mode == "grid_voltage_drift":
        base_offset = rng.uniform(-0.04, -0.01)
        drift_per_hour = rng.uniform(-0.18, -0.14)
    # A future lightning/grid dip is not predictable from pre-event data. Such scenarios are useful for
    # testing alarms and recovery, but are deliberately excluded from this stoppage-prediction label set.

    recorder = SignalRecorder(sample_period)
    view = OperatorView(plant, noise_fraction=noise_fraction, drift_fraction_per_hour=drift_fraction_per_hour,
                        seed=seed)
    sample = view.feedback
    stop_seen_at = None
    steps = int(max_seconds / dt)
    for _ in range(steps):
        power.offset = base_offset + drift_per_hour * plant.time / 3600.0
        plant.step(dt)
        values = sample()
        recorder.record(plant.time, values)
        if values.get(TARGET, 0.0) > 0.5 and stop_seen_at is None:
            stop_seen_at = plant.time
        if stop_seen_at is not None and plant.time - stop_seen_at > 2 * sample_period:
            break
    return recorder.arrays()
