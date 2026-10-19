import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sqlalchemy import select
from sqlalchemy.orm import Session

from Data.Model.model import Base, ChemComponent, Plant
from Data.chemistry import seed_chemistry
from Data.database import get_engine
from Data.loader import load_plant
from Data.scenarios import save_scenario
from Data.tags import export_gvl, load_tags, sync_tags
from Modules.Editor.document import PlantDocument
from Modules.Scenario.scenario import Event, Scenario, snow_day

TRAINING_NAME = "reactor_training_plant"

# =====================================================================
# The reactor training plant: the same kind of plant the watchdog's synthetic generator trains on, laid out so
# you can open it, watch it and break it by hand. Two reaction lines make a product C from A + B:
#
#   Lane 1  STIRRED REACTOR   TK401 (feed) -> P401 -> PL401 -> R401 (CSTR, jacket valve UV401) -> PL402 -> XV402 -> SK401
#   Lane 2  TUBULAR REACTOR   TK402 (feed) -> P402 -> PL403 -> TR401 (PFR, jacket valve UV402) -> PL404 -> XV404 -> SK402
#
# PL402 and PL404 are long, poorly insulated product lines. C is less soluble when cold (set below), so in cold
# weather it slowly plates out on those walls: the line narrows, the pressure before it rises, the pressure after
# it falls and the flow sags, all gradually. The saved scenarios start the plant and then bring one of the
# slow problems (cooling loss, coolant fouling, wall scaling) or a winter cold snap.
# =====================================================================

FEED = {"A": 0.035, "B": 0.035, "water": 0.93}
TUBE_FEED = {"A": 0.055, "B": 0.055, "water": 0.89}
REACTOR_PARAMS = {"temperature": 20.0, "reactions": ["A_B_to_C"], "coolant_temp": 12.0}

# tag: (type, x, y, z, params)       x and y are canvas units (20 units = 1 m), z is the elevation in metres
NODES = {
    "TK401": ("storage_tank", 60, 120, 1.5, {"max_capacity": 100000.0, "height": 3.0, "initial_volume": 60000.0,
                                              "composition": FEED, "temperature": 20.0, "ambient_ua": 50.0}),
    "P401": ("pump", 240, 120, 0, {"shutoff_head": 3.0, "max_flow": 6.0}),
    "PL401": ("pipe", 400, 120, 0, {"diameter": 0.05, "length": 6.0}),
    "R401": ("cstr", 580, 120, 0, {"max_capacity": 1000.0, "height": 3.0, "initial_volume": 300.0,
                                    "composition": {"water": 1.0}, "cooling_ua": 15000.0,
                                    "cooling_valve": "UV401", **REACTOR_PARAMS}),
    "UV401": ("utility_valve", 580, 300, 0, {"fail_position": 1.0}),
    "PL402": ("pipe", 800, 120, 0, {"diameter": 0.05, "length": 30.0, "ambient_ua": 600.0,
                                     "fouling_rate_scale": 0.0}),
    "XV402": ("valve", 1000, 120, 0, {"rated_dp": 0.05, "max_flow": 8.0}),
    "SK401": ("sink", 1140, 120, 0, {}),

    "TK402": ("storage_tank", 60, 520, 1.5, {"max_capacity": 100000.0, "height": 3.0, "initial_volume": 60000.0,
                                              "composition": TUBE_FEED, "temperature": 20.0, "ambient_ua": 50.0}),
    "P402": ("pump", 240, 520, 0, {"shutoff_head": 3.0, "max_flow": 6.0}),
    "PL403": ("pipe", 400, 520, 0, {"diameter": 0.05, "length": 6.0}),
    "TR401": ("pfr", 580, 520, 0, {"tube_volume": 320.0, "cooling_ua": 15000.0, "cooling_valve": "UV402",
                                    **REACTOR_PARAMS}),
    "UV402": ("utility_valve", 580, 700, 0, {"fail_position": 1.0}),
    "PL404": ("pipe", 800, 520, 0, {"diameter": 0.05, "length": 30.0, "ambient_ua": 600.0,
                                     "fouling_rate_scale": 0.0}),
    "XV404": ("valve", 1000, 520, 0, {"rated_dp": 0.05, "max_flow": 8.0}),
    "SK402": ("sink", 1140, 520, 0, {}),
}

EDGES = [
    ("TK401", "out1", "P401", "in"), ("P401", "out", "PL401", "in"), ("PL401", "out", "R401", "in1"),
    ("R401", "out1", "PL402", "in"), ("PL402", "out", "XV402", "in"), ("XV402", "out", "SK401", "in"),
    ("TK402", "out1", "P402", "in"), ("P402", "out", "PL403", "in"), ("PL403", "out", "TR401", "in1"),
    ("TR401", "out1", "PL404", "in"), ("PL404", "out", "XV404", "in"), ("XV404", "out", "SK402", "in"),
]

PROBES = [
    ("LT401", "level_sensor", "TK401", {}), ("FT401", "flow_sensor", "P401", {}),
    ("PT401", "pressure_sensor", "P401", {}), ("TT401", "temperature_sensor", "R401", {}),
    ("LT402", "level_sensor", "R401", {}), ("AT401", "analyzer", "R401", {"component": "C"}),
    ("PDT402", "differential_pressure_sensor", "PL402", {}), ("PT402", "pressure_sensor", "PL402", {}),
    ("FT402", "flow_sensor", "P402", {}), ("TT402", "temperature_sensor", "TR401", {}),
    ("AT402", "analyzer", "TR401", {"component": "C"}), ("PDT404", "differential_pressure_sensor", "PL404", {}),
]

STARTUP = [      # (delay s, target, command signal, value)
    (1, "UV401", "PositionSP", 75.0), (2, "UV402", "PositionSP", 85.0), (3, "XV402", "PositionSP", 100.0),
    (4, "XV404", "PositionSP", 100.0), (5, "P401", "RunCmd", True), (6, "P402", "RunCmd", True),
]


def with_startup(scenario):
    for delay, target, name, value in STARTUP:
        scenario.add(Event(float(delay), "command", target, name, float(value), label=f"start: {target}.{name}"))
    return scenario


def _make_c_soluble_when_warm(session):
    """C stays dissolved at reactor temperature but is over-saturated on a cold pipe wall."""
    row = session.scalars(select(ChemComponent).where(ChemComponent.name == "C")).one()
    row.attributes = {**(row.attributes or {}), "solubility_25c": 0.9, "solubility_slope": 0.08}
    session.commit()


def build_training_plant(session):
    """(Re)create the reactor training plant with a hand-made layout, its PLC tags and saved scenarios."""
    Base.metadata.create_all(session.get_bind())
    seed_chemistry(session)
    _make_c_soluble_when_warm(session)
    old = session.scalars(select(Plant).where(Plant.name == TRAINING_NAME)).first()
    if old is not None:
        session.delete(old)
        session.commit()
    doc = PlantDocument.create(session, TRAINING_NAME, "Stirred and tubular reactor lines with cold product pipes")
    for tag, (type_name, x, y, z, params) in NODES.items():
        doc.add_node(type_name, x, y, tag=tag, params=params, z=z)
    for edge in EDGES:
        doc.connect(*edge)
    for tag, type_name, target, params in PROBES:
        doc.add_probe(type_name, target, tag=tag, params=params)
    problems = doc.validate()
    if problems:
        raise RuntimeError("Training plant does not build: " + "; ".join(problems))
    doc.save()

    model = load_plant(session, TRAINING_NAME)
    sync_tags(session, TRAINING_NAME, model)
    save_scenario(session, TRAINING_NAME, with_startup(Scenario("Normal operation", "Both reactor lines run healthy")))
    loss = Scenario("Cooling loss", "The R401 jacket valve is throttled back: the reaction heats up unless caught")
    loss.add(Event(900.0, "command", "UV401", "PositionSP", 5.0, label="cooling throttled back"))
    save_scenario(session, TRAINING_NAME, with_startup(loss))
    scale = Scenario("Wall scaling", "Product C slowly plates out on the cold PL402 wall: flow sags, pressure shifts")
    scale.add(Event(600.0, "param", "PL402", "fouling_rate_scale", 0.02, ramp=3600.0, label="deposition on PL402"))
    save_scenario(session, TRAINING_NAME, with_startup(scale))
    save_scenario(session, TRAINING_NAME, with_startup(snow_day(duration=6 * 3600.0)))
    return TRAINING_NAME


if __name__ == "__main__":
    with Session(get_engine()) as session:
        build_training_plant(session)
        gvl = os.path.join(PROJECT_ROOT, "PLC", "GVL_Sim_training.st")
        with open(gvl, "w", encoding="utf-8") as f:
            f.write(export_gvl(load_tags(session, TRAINING_NAME)))
        print(f"Plant '{TRAINING_NAME}' stored; TwinCAT variables written to {gvl}")
