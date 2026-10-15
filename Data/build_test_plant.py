import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sqlalchemy import select
from sqlalchemy.orm import Session

from Data.database import get_engine
from Data.Model.model import Base, Plant, add_equipment, add_instrument, connect
from Modules.Flow.flow import Material

# =====================================================================
# Test plant: three feed tanks -> pumps and valves -> one reactor -> sink
#
#   SRC101 -> XV101 -> TK101 -> P101 -> XV111 --+
#   SRC102 -> XV102 -> TK102 -> P102 -> XV112 --+--> R201 -> XV201 -> PI301 -> SNK301
#   SRC103 -> XV103 -> TK103 -> P103 -> XV113 --+
#
# Feeds: A, B (reactants) and water (solvent). Reaction in R201: A + B -> C.
# =====================================================================

PLANT_NAME = "test_plant_3tank_reactor"
FEEDS = [("101", "A"), ("102", "B"), ("103", "water")]


def liquid(component, volume_l):
    """Mole amounts for a given volume of one pure component."""
    return Material.from_fractions({component: 1.0}, volume_l).moles


def build_test_plant(session):
    old = session.scalars(select(Plant).where(Plant.name == PLANT_NAME)).first()
    if old:
        session.delete(old)
        session.flush()

    plant = Plant(name=PLANT_NAME, description="3 feed tanks into one CSTR (A + B -> C in water)")
    session.add(plant)

    reactor = add_equipment(plant, "R201", "cstr",
                            {"max_capacity": 1000.0, "initial_moles": liquid("water", 200.0)},
                            inlets=["in1", "in2", "in3"], outlets=["out1"])
    out_valve = add_equipment(plant, "XV201", "valve", {"max_flow": 3.0},
                              inlets=["in1"], outlets=["out1"])
    out_pipe = add_equipment(plant, "PI301", "pipe", inlets=["in1"], outlets=["out1"])
    sink = add_equipment(plant, "SNK301", "sink", inlets=["in1"])
    connect(plant, reactor, "out1", out_valve, "in1")
    connect(plant, out_valve, "out1", out_pipe, "in1")
    connect(plant, out_pipe, "out1", sink, "in1")

    add_instrument(plant, "LT201", "level_sensor", reactor, {"noise_std": 0.05})
    add_instrument(plant, "TT201", "temperature_sensor", reactor, {"noise_std": 0.05})
    add_instrument(plant, "FT201", "flow_sensor", out_valve, {"noise_std": 0.02})

    for i, (n, component) in enumerate(FEEDS, start=1):
        src = add_equipment(plant, f"SRC{n}", "source", {"composition": {component: 1.0}}, outlets=["out1"])
        xv_in = add_equipment(plant, f"XV{n}", "valve", {"max_flow": 2.0}, inlets=["in1"], outlets=["out1"])
        tank = add_equipment(plant, f"TK{n}", "storage_tank",
                             {"max_capacity": 5000.0, "initial_moles": liquid(component, 3000.0)},
                             inlets=["in1"], outlets=["out1"])
        pump = add_equipment(plant, f"P{n}", "pump", {"max_flow": 2.0}, inlets=["in1"], outlets=["out1"])
        xv_out = add_equipment(plant, f"XV{int(n) + 10}", "valve", {"max_flow": 2.0},
                               inlets=["in1"], outlets=["out1"])
        connect(plant, src, "out1", xv_in, "in1")
        connect(plant, xv_in, "out1", tank, "in1")
        connect(plant, tank, "out1", pump, "in1")
        connect(plant, pump, "out1", xv_out, "in1")
        connect(plant, xv_out, "out1", reactor, f"in{i}")

        add_instrument(plant, f"LT{n}", "level_sensor", tank, {"noise_std": 0.05})
        add_instrument(plant, f"FT{n}", "flow_sensor", pump, {"noise_std": 0.02})

    session.commit()
    return plant


def main():
    engine = get_engine()
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        plant = build_test_plant(session)
        print(f"Stored plant '{plant.name}': {len(plant.equipment)} equipment, "
              f"{len(plant.connections)} connections, {len(plant.instruments)} instruments")


if __name__ == "__main__":
    main()
