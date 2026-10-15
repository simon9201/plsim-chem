import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sqlalchemy import select
from sqlalchemy.orm import Session

from Data.Model.model import (Base, Plant, add_equipment, add_instrument, connect)
from Data.chemistry import seed_chemistry
from Data.database import get_engine, DB_PATH
from Data.loader import load_plant
from Data.tags import sync_tags, export_gvl, load_tags

PLANT_NAME = "test_plant"

# =====================================================================
# The base test plant:
#
#   S101 -XV101-> TK101 -P101-XV111-PL111-\
#   S102 -XV102-> TK102 -P102-XV112-PL112--> R201 -XV201-PL201-> SK201
#   S103 -XV103-> TK103 -P103-XV113-PL113-/        (cooling water: XV210)
#
# S10x are external supplies (arriving goods), SK201 takes the product away.
# Edit this file to build other plants: every plant is just rows in the database.
# =====================================================================


def _reuse_plant(session):
    """Return the plant row, emptied of equipment/instruments/connections.
    The row itself is kept so edited PLC symbols and saved snapshots survive a rebuild."""
    plant = session.scalars(select(Plant).where(Plant.name == PLANT_NAME)).first()
    if plant is None:
        plant = Plant(name=PLANT_NAME, description="3 feed tanks -> pumps/valves -> CSTR -> sink")
        session.add(plant)
        session.flush()
        return plant
    plant.connections.clear()
    plant.instruments.clear()
    session.flush()
    plant.equipment.clear()
    session.flush()
    return plant


def build_test_plant(session):
    Base.metadata.create_all(session.get_bind())
    seed_chemistry(session)
    plant = _reuse_plant(session)
    eq = {}

    def add(tag, type, params=None, inlets=(), outlets=()):
        eq[tag] = add_equipment(plant, tag, type, params, inlets, outlets)
        return eq[tag]

    feeds = {"1": {"A": 0.1, "water": 0.9},
             "2": {"B": 0.1, "water": 0.9},
             "3": {"water": 1.0}}

    for n, composition in feeds.items():
        add(f"S10{n}", "source", {"composition": composition, "temperature": 20.0, "supply_pressure": 2.0},
            outlets=["out"])
        add(f"XV10{n}", "valve", {"rated_dp": 0.5, "max_flow": 4.0}, ["in"], ["out"])
        add(f"TK10{n}", "storage_tank",
            {"max_capacity": 2000.0, "height": 3.0, "initial_volume": 1200.0,
             "composition": composition, "temperature": 20.0},
            ["in1"], ["out1"])
        add(f"P10{n}", "pump", {"shutoff_head": 0.5, "max_flow": 2.4}, ["in"], ["out"])
        add(f"XV11{n}", "valve", {"rated_dp": 0.5, "max_flow": 3.0}, ["in"], ["out"])
        add(f"PL11{n}", "pipe", {"diameter": 0.05, "length": 8.0}, ["in"], ["out"])

    add("XV210", "utility_valve", {"fail_position": 1.0})
    add("R201", "cstr",
        {"max_capacity": 1000.0, "height": 3.0, "initial_volume": 300.0, "composition": {"water": 1.0},
         "temperature": 20.0, "reactions": ["A_B_to_C"], "cooling_ua": 5000.0, "coolant_temp": 15.0,
         "cooling_valve": "XV210"},
        ["in1", "in2", "in3"], ["out1"])
    add("XV201", "valve", {"rated_dp": 0.2, "max_flow": 4.0}, ["in"], ["out"])
    add("PL201", "pipe", {"diameter": 0.05, "length": 10.0}, ["in"], ["out"])
    add("SK201", "sink", inlets=["in"])

    for n in feeds:
        connect(plant, eq[f"S10{n}"], "out", eq[f"XV10{n}"], "in")
        connect(plant, eq[f"XV10{n}"], "out", eq[f"TK10{n}"], "in1")
        connect(plant, eq[f"TK10{n}"], "out1", eq[f"P10{n}"], "in")
        connect(plant, eq[f"P10{n}"], "out", eq[f"XV11{n}"], "in")
        connect(plant, eq[f"XV11{n}"], "out", eq[f"PL11{n}"], "in")
        connect(plant, eq[f"PL11{n}"], "out", eq["R201"], f"in{n}")
    connect(plant, eq["R201"], "out1", eq["XV201"], "in")
    connect(plant, eq["XV201"], "out", eq["PL201"], "in")
    connect(plant, eq["PL201"], "out", eq["SK201"], "in")

    for n in feeds:
        add_instrument(plant, f"LT10{n}", "level_sensor", eq[f"TK10{n}"], {"noise_std": 0.05})
        add_instrument(plant, f"FT10{n}", "flow_sensor", eq[f"P10{n}"], {"noise_std": 0.02})
    add_instrument(plant, "PT101", "pressure_sensor", eq["TK101"], {"noise_std": 0.005})
    add_instrument(plant, "PDT111", "differential_pressure_sensor", eq["XV111"], {"noise_std": 0.005})
    add_instrument(plant, "LT201", "level_sensor", eq["R201"], {"noise_std": 0.05})
    add_instrument(plant, "TT201", "temperature_sensor", eq["R201"], {"noise_std": 0.1})
    add_instrument(plant, "FT201", "flow_sensor", eq["XV201"], {"noise_std": 0.02})
    add_instrument(plant, "AT201", "analyzer", eq["R201"], {"component": "C", "noise_std": 0.01})
    session.commit()
    return plant


if __name__ == "__main__":
    engine = get_engine()
    with Session(engine) as session:
        build_test_plant(session)
        model = load_plant(session, PLANT_NAME)
        sync_tags(session, PLANT_NAME, model)
        gvl_path = os.path.join(PROJECT_ROOT, "PLC", "GVL_Sim.st")
        with open(gvl_path, "w", encoding="utf-8") as f:
            f.write(export_gvl(load_tags(session, PLANT_NAME)))
        print(f"Plant '{PLANT_NAME}' stored in {DB_PATH}")
        print(f"Wrote {gvl_path} - paste it into your TwinCAT GVL_Sim")
