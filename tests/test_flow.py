import os
import sys
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from Data.Model.model import Base, Plant, add_equipment, add_instrument, connect
from Data.build_test_plant import PLANT_NAME, build_test_plant
from Data.chemistry import seed_chemistry
from Data.loader import load_plant
from Data.snapshots import list_snapshots, load_snapshot, save_snapshot
from Modules.Flow.chemistry import (COMPONENTS, DEFAULT_COMPONENTS, DEFAULT_REACTIONS, REACTIONS, Component, Reaction,
                                    set_chemistry, use_default_chemistry)
from Modules.Flow.flow import Material
from Modules.Entity.Process_equipment.Junction.junction import Tee
from Modules.Entity.Process_equipment.Pipe.Pipe import Elbow, Pipe
from Modules.Entity.Process_equipment.Valve.Valve import CheckValve, Valve
from Modules.Entity.Process_equipment.Vessel.StorageTank.storage_tank import StorageTank
from Modules.Entity.Process_equipment.Vessel.Reactor.CSTR.CSTR import ContinuousStirredTankReactor
from Modules.Entity.Process_equipment.Boundary.boundary import Sink, Source
from Modules.Plant.plant_model import PlantModel

DT = 0.1


def run(plant, seconds):
    for _ in range(round(seconds / DT)):
        plant.step(DT)


def tank(name, volume, composition=None, **kw):
    return StorageTank(name, inlets=["in1"], outlets=["out1"], max_capacity=1000.0,
                       initial_volume=volume, composition=composition or {"water": 1.0}, **kw)


def open_valve(name, **kw):
    return Valve(name, inlets=["in"], outlets=["out"], initial_position=1.0, **kw)


class FlowTests(unittest.TestCase):
    def tearDown(self):
        use_default_chemistry()

    def test_gravity_equalizes_and_conserves_volume(self):
        p = PlantModel()
        a, b = p.add_equipment(tank("A", 800)), p.add_equipment(tank("B", 100))
        p.add_equipment(open_valve("V"))
        p.connect("A", "out1", "V", "in")
        p.connect("V", "out", "B", "in1")
        total = a.volume + b.volume
        run(p, 600)
        self.assertAlmostEqual(a.volume + b.volume, total, places=6)
        self.assertGreater(b.volume, 100)
        self.assertLess(abs(a.pressure - b.headspace_pressure) - abs(b.hydrostatic_pressure), 0.5)

    def test_source_valve_sink_matches_formula(self):
        p = PlantModel()
        p.add_equipment(Source("S", composition={"water": 1.0}, supply_pressure=2.0))
        p.add_equipment(open_valve("V", rated_dp=0.5, max_flow=4.0))
        p.add_equipment(Sink("K"))
        p.connect("S", "out", "V", "in")
        p.connect("V", "out", "K", "in")
        p.step(DT)
        self.assertAlmostEqual(p.equipment["V"].flow, 4.0 * ((2.0 - 1.013) / 0.5) ** 0.5, places=3)

    def test_closed_valve_shows_full_differential_pressure(self):
        p = PlantModel()
        p.add_equipment(Source("S", composition={"water": 1.0}, supply_pressure=2.0))
        p.add_equipment(Valve("V", inlets=["in"], outlets=["out"]))
        p.add_equipment(Sink("K"))
        p.connect("S", "out", "V", "in")
        p.connect("V", "out", "K", "in")
        p.step(DT)
        self.assertEqual(p.equipment["V"].flow, 0.0)
        self.assertAlmostEqual(p.equipment["V"].differential_pressure, 2.0 - 1.013, places=6)

    def test_tee_merges_flows_and_mixes_composition(self):
        p = PlantModel()
        p.add_equipment(Source("SA", composition={"A": 1.0}, supply_pressure=2.0, temperature=20.0))
        p.add_equipment(Source("SB", composition={"B": 1.0}, supply_pressure=2.0, temperature=60.0))
        p.add_equipment(Tee("T", inlets=["in1", "in2"], outlets=["out1"]))
        p.add_equipment(open_valve("V1"))
        p.add_equipment(open_valve("V2"))
        p.add_equipment(Sink("K"))
        p.connect("SA", "out", "V1", "in")
        p.connect("V1", "out", "T", "in1")
        p.connect("SB", "out", "V2", "in")
        p.connect("V2", "out", "T", "in2")
        p.connect("T", "out1", "K", "in")
        run(p, 5)
        self.assertAlmostEqual(p.equipment["V1"].flow + p.equipment["V2"].flow, p.equipment["K"].flow, places=6)
        got = p.equipment["K"].received
        self.assertGreater(got.moles["A"], 0)
        self.assertGreater(got.moles["B"], 0)
        self.assertTrue(20.0 < got.temperature < 60.0)

    def _backflow_plant(self, element):
        # A high-pressure source pushes into the tee, which is connected back to tank A through `element`.
        p = PlantModel()
        p.add_equipment(tank("A", 100))
        p.add_equipment(Source("S", composition={"water": 1.0}, supply_pressure=3.0))
        p.add_equipment(open_valve("V1"))
        p.add_equipment(open_valve("V2"))
        p.add_equipment(Tee("T", inlets=["in1", "in2"], outlets=["out1"]))
        p.add_equipment(element)
        p.add_equipment(Sink("K"))
        p.connect("A", "out1", element.name, "in")
        p.connect(element.name, "out", "T", "in1")
        p.connect("S", "out", "V1", "in")
        p.connect("V1", "out", "T", "in2")
        p.connect("T", "out1", "V2", "in")
        p.connect("V2", "out", "K", "in")
        return p

    def test_check_valve_blocks_reverse_flow(self):
        p = self._backflow_plant(CheckValve("E", inlets=["in"], outlets=["out"]))
        run(p, 20)
        self.assertAlmostEqual(p.equipment["A"].volume, 100, places=3)

    def test_plain_valve_lets_liquid_flow_backwards(self):
        p = self._backflow_plant(open_valve("E"))
        run(p, 20)
        self.assertGreater(p.equipment["A"].volume, 105)
    def test_elbow_loses_more_than_straight_pipe(self):
        self.assertGreater(Elbow("E", length=5.0).loss_coefficient(), Pipe("P", length=5.0).loss_coefficient())

    def test_reaction_enthalpy_from_formation(self):
        self.assertAlmostEqual(REACTIONS["A_B_to_C"].enthalpy(), -140e3)

    def test_unconnected_transfer_is_rejected(self):
        p = PlantModel()
        p.add_equipment(Valve("V", inlets=["in"], outlets=["out"]))
        with self.assertRaises(ValueError):
            p.step(DT)

    def test_cstr_conserves_atoms_and_releases_heat(self):
        p = PlantModel()
        r = p.add_equipment(ContinuousStirredTankReactor(
            "R", inlets=["in1"], outlets=["out1"], max_capacity=1000.0, initial_volume=100.0,
            composition={"A": 0.1, "B": 0.1, "water": 0.8}, temperature=25.0, reactions=["A_B_to_C"]))
        before = dict(r.contents.moles)
        run(p, 100)
        m = r.contents.moles
        self.assertAlmostEqual(m["A"], m["B"], places=6)
        self.assertAlmostEqual(m["A"] + m["C"], before["A"], places=6)
        self.assertAlmostEqual(m["water"], before["water"], places=6)
        self.assertGreater(m["C"], 0)
        self.assertGreater(r.temperature, 25.0)

    def test_custom_reaction_and_component_attributes(self):
        d = Component("D", 120.0, 1100.0, 150.0, -400e3, {"hazard": "corrosive", "boiling_point_c": 150.0})
        rx = Reaction("2A_to_D", {"A": -2, "D": 1}, k_ref=0.05, activation_energy=40e3)
        set_chemistry(list(DEFAULT_COMPONENTS) + [d], list(DEFAULT_REACTIONS) + [rx])
        self.assertAlmostEqual(rx.enthalpy(), -400e3 + 200e3)
        self.assertEqual(rx.orders, {"A": 2})
        p = PlantModel()
        r = p.add_equipment(ContinuousStirredTankReactor(
            "R", inlets=["in1"], outlets=["out1"], initial_volume=100.0,
            composition={"A": 0.2, "water": 0.8}, reactions=["2A_to_D"]))
        a0 = r.contents.moles["A"]
        run(p, 50)
        self.assertAlmostEqual(r.contents.moles["A"] + 2 * r.contents.moles["D"], a0, places=6)
        self.assertTrue(100.0 < r.contents.attribute_average("boiling_point_c") < 150.0)

    def test_snapshot_roundtrip_is_deterministic(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as s:
            build_test_plant(s)
            a, b = load_plant(s, PLANT_NAME), load_plant(s, PLANT_NAME)
            for plant in (a, b):
                for t in ("XV111", "XV112", "XV113", "XV201"):
                    plant.equipment[t].command = 0.8
                for t in ("P101", "P102", "P103"):
                    plant.equipment[t].run_cmd = True
            run(a, 60)
            save_snapshot(s, PLANT_NAME, a, "mid")
            self.assertEqual(list_snapshots(s, PLANT_NAME)[0][0], "mid")
            with self.assertRaises(ValueError):
                save_snapshot(s, PLANT_NAME, a, "mid")
            run(a, 60)
            load_snapshot(s, PLANT_NAME, b, "mid")
            self.assertAlmostEqual(b.time, 60.0, places=6)
            run(b, 60)
            for tag in ("R201", "TK101", "TK103"):
                self.assertAlmostEqual(a.equipment[tag].volume, b.equipment[tag].volume, places=6)
                self.assertAlmostEqual(a.equipment[tag].temperature, b.equipment[tag].temperature, places=6)
            self.assertAlmostEqual(a.equipment["R201"].concentration("C"), b.equipment["R201"].concentration("C"), places=6)

    def test_analyzer_reads_concentration(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as s:
            build_test_plant(s)
            plant = load_plant(s, PLANT_NAME)
        r = plant.equipment["R201"]
        r.contents = Material.from_fractions({"C": 0.1, "water": 0.9}, 300.0)
        plant.sample_instruments()
        self.assertAlmostEqual(plant.instruments["AT201"].value, r.concentration("C"), delta=0.1)


if __name__ == "__main__":
    unittest.main()
