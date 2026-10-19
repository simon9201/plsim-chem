import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sqlalchemy import select
from sqlalchemy.orm import Session

from Data.Model.model import Base, Plant
from Data.chemistry import seed_chemistry
from Data.database import get_engine
from Data.loader import load_plant
from Data.scenarios import save_scenario
from Data.tags import export_gvl, load_tags, sync_tags
from Modules.Editor.document import PlantDocument
from Modules.Scenario.scenario import Event, Scenario, leak, snow_day

DEMO_NAME = "demo_plant"

# =====================================================================
# The demo plant: a small ethanol/water process slice that touches most modules. Open it from the launcher
# (Build demo plant) and look at it in 2D (connections, signals) and 3D (elevation, vessels).
#
#   Lane 1  FEED & SEPARATION   S301 -> XV301 -> TK301 (raised feed tank) -> P301 -> HX301 (pre-heater) -> C301
#                               C301 column: distillate -> TK302 -> P302 -> SK302 (product)
#                                            bottoms    -> XV302 -> SK301 (waste water)
#   Lane 2  EVAPORATION         S302 -> P303 -> EV301 (evaporator) -> SK303; steam leaves through GP302 to GSK302
#   Lane 3  GAS & COMPRESSION   GS301 (N2) -> CP301 -> GP301 -> GV301 (buffer) -> GVV301 -> GSK301
#
# Things to try: the TK301 -> P301 pump lifts the liquid (z is in metres, see the 3D view); the column splits
# ethanol from water; the "Leak on TK301", "Snow day" and "Demo upset" scenarios are saved with the plant.
# =====================================================================

# tag: (type, x, y, z, params)       x and y are canvas units (20 units = 1 m), z is the elevation in metres
NODES = {
    "S301": ("source", 60, 80, 0, {"composition": {"ethanol": 0.2, "water": 0.8}, "temperature": 20.0,
                                   "supply_pressure": 2.0}),
    "XV301": ("valve", 190, 80, 0, {"rated_dp": 0.5, "max_flow": 4.0}),
    "TK301": ("storage_tank", 330, 80, 2.0, {"max_capacity": 2000.0, "height": 3.0, "initial_volume": 1400.0,
                                              "composition": {"ethanol": 0.2, "water": 0.8}, "temperature": 15.0,
                                              "ambient_ua": 50.0}),
    "P301": ("pump", 470, 80, 0, {"shutoff_head": 0.8, "max_flow": 2.4}),
    "HX301": ("heat_exchanger", 610, 80, 0, {"ua": 2000.0, "utility_temp": 95.0, "length": 4.0}),
    "C301": ("distillation_column", 780, 140, 0, {"composition": {"ethanol": 0.2, "water": 0.8},
                                                   "initial_volume": 300.0, "heater_power": 100000.0}),
    "PL302": ("pipe", 930, 60, 0, {"diameter": 0.05, "length": 8.0}),
    "TK302": ("storage_tank", 1070, 60, 0, {"max_capacity": 800.0, "height": 2.5, "initial_volume": 100.0,
                                             "composition": {"ethanol": 0.9, "water": 0.1}, "temperature": 20.0}),
    "P302": ("pump", 1210, 60, 0, {"shutoff_head": 0.5, "max_flow": 1.0}),
    "SK302": ("sink", 1340, 60, 0, {}),
    "XV302": ("valve", 930, 220, 0, {"rated_dp": 0.3, "max_flow": 3.0}),
    "SK301": ("sink", 1070, 220, 0, {}),

    "S302": ("source", 60, 380, 0, {"composition": {"water": 1.0}, "temperature": 20.0, "supply_pressure": 2.0}),
    "P303": ("pump", 220, 380, 0, {"shutoff_head": 0.5, "max_flow": 1.5}),
    "EV301": ("evaporator", 420, 380, 0, {"initial_volume": 400.0, "composition": {"water": 1.0},
                                           "heater_power": 100000.0}),
    "SK303": ("sink", 620, 340, 0, {}),
    "GP302": ("gas_pipe", 620, 440, 4.0, {"diameter": 0.1, "length": 6.0}),
    "GSK302": ("gas_sink", 780, 440, 4.0, {}),

    "GS301": ("gas_source", 60, 620, 0, {"supply_pressure": 7.0, "composition": {"N2": 1.0}}),
    "CP301": ("compressor", 220, 620, 0, {"max_flow": 10.0, "max_ratio": 4.0}),
    "GP301": ("gas_pipe", 380, 620, 0, {"diameter": 0.05, "length": 5.0}),
    "GV301": ("gas_volume", 540, 620, 0, {"volume": 1000.0, "composition": {"N2": 1.0}}),
    "GVV301": ("gas_valve", 700, 620, 0, {"max_flow": 20.0}),
    "GSK301": ("gas_sink", 850, 620, 0, {}),
}

EDGES = [
    ("S301", "out", "XV301", "in"), ("XV301", "out", "TK301", "in1"), ("TK301", "out1", "P301", "in"),
    ("P301", "out", "HX301", "in"), ("HX301", "out", "C301", "feed"),
    ("C301", "distillate", "PL302", "in"), ("PL302", "out", "TK302", "in1"), ("TK302", "out1", "P302", "in"),
    ("P302", "out", "SK302", "in"),
    ("C301", "bottoms", "XV302", "in"), ("XV302", "out", "SK301", "in"),
    ("S302", "out", "P303", "in"), ("P303", "out", "EV301", "in1"), ("EV301", "out1", "SK303", "in"),
    ("EV301", "vapor_out", "GP302", "in"), ("GP302", "out", "GSK302", "in"),
    ("GS301", "out", "CP301", "in"), ("CP301", "out", "GP301", "in"), ("GP301", "out", "GV301", "in1"),
    ("GV301", "out1", "GVV301", "in"), ("GVV301", "out", "GSK301", "in"),
]

PROBES = [
    ("LT301", "level_sensor", "TK301", {}), ("FT301", "flow_sensor", "P301", {}),
    ("TT301", "temperature_sensor", "TK301", {}), ("PDT301", "differential_pressure_sensor", "XV301", {}),
    ("LT302", "level_sensor", "TK302", {}), ("AT302", "analyzer", "TK302", {"component": "ethanol"}),
    ("TT302", "temperature_sensor", "C301", {}), ("FT302", "flow_sensor", "P302", {}),
    ("LT303", "level_sensor", "EV301", {}), ("TT303", "temperature_sensor", "EV301", {}),
    ("PT301", "pressure_sensor", "GV301", {}),
]


STARTUP = [      # (delay s, target, command signal, value): what an operator (or the PLC program) does to start up
    (1, "XV301", "PositionSP", 12.0), (2, "P301", "RunCmd", True), (3, "HX301", "DutyCmd", 60.0),
    (4, "C301", "HeaterCmd", 80.0), (5, "C301", "RefluxCmd", 20.0), (6, "XV302", "PositionSP", 10.0),
    (7, "P303", "RunCmd", True), (8, "EV301", "HeaterCmd", 50.0),
    (9, "CP301", "RunCmd", True), (10, "GVV301", "PositionSP", 40.0),
    (900, "P302", "SpeedSP", 2.0), (901, "P302", "RunCmd", True),     # product pump waits for TK302 to fill
]


def with_startup(scenario):
    """Start the plant, then play the scenario's own events on top of the running plant."""
    for delay, target, name, value in STARTUP:
        scenario.add(Event(float(delay), "command", target, name, float(value), label=f"start: {target}.{name}"))
    return scenario


def build_demo_plant(session):
    """(Re)create the demo plant with a hand-made layout, its PLC tags and four saved scenarios."""
    Base.metadata.create_all(session.get_bind())
    seed_chemistry(session)
    old = session.scalars(select(Plant).where(Plant.name == DEMO_NAME)).first()
    if old is not None:
        session.delete(old)
        session.commit()
    doc = PlantDocument.create(session, DEMO_NAME, "Ethanol/water separation, evaporation and gas compression slice")
    for tag, (type_name, x, y, z, params) in NODES.items():
        doc.add_node(type_name, x, y, tag=tag, params=params, z=z)
    for edge in EDGES:
        doc.connect(*edge)
    for tag, type_name, target, params in PROBES:
        doc.add_probe(type_name, target, tag=tag, params=params)
    problems = doc.validate()
    if problems:
        raise RuntimeError("Demo plant does not build: " + "; ".join(problems))
    doc.save()

    model = load_plant(session, DEMO_NAME)
    sync_tags(session, DEMO_NAME, model)
    normal = Scenario("Normal operation", "Start every pump, valve, heater and the compressor; nothing goes wrong")
    save_scenario(session, DEMO_NAME, with_startup(normal))
    save_scenario(session, DEMO_NAME, with_startup(leak("TK301", rate=0.15, start=120.0, grow_over=300.0)))
    save_scenario(session, DEMO_NAME, with_startup(snow_day(duration=6 * 3600.0)))
    upset = Scenario("Demo upset", "Feed valve sticks at 3 min and the feed pump wears badly at 6 min")
    upset.add(Event(180.0, "fault", "XV301", "stuck", 1.0, label="feed valve sticks"))
    upset.add(Event(360.0, "fault", "P301", "wear", 0.5, ramp=300.0, label="feed pump wear"))
    save_scenario(session, DEMO_NAME, with_startup(upset))
    return DEMO_NAME


if __name__ == "__main__":
    with Session(get_engine()) as session:
        build_demo_plant(session)
        gvl = os.path.join(PROJECT_ROOT, "PLC", "GVL_Sim_demo.st")
        with open(gvl, "w", encoding="utf-8") as f:
            f.write(export_gvl(load_tags(session, DEMO_NAME)))
        print(f"Plant '{DEMO_NAME}' stored; TwinCAT variables written to {gvl}")
