import os
import random
import sys
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from Data.chemistry import load_chemistry, seed_chemistry
from Data.Model.model import Base
from Modules.Entity.Process_equipment.Boundary.boundary import Sink, Source
from Modules.Entity.Process_equipment.Vessel.Reactor.CSTR.CSTR import ContinuousStirredTankReactor
from Modules.Entity.Process_equipment.Vessel.Vessel import Vessel
from Modules.Flow import kinetics
from Modules.Flow.batch import make_lot, random_lot
from Modules.Flow.chemistry import COMPONENTS, REACTIONS, Reaction, use_default_chemistry
from Modules.Flow.flow import Material
from Modules.Lab.lab import Laboratory, with_lab
from Modules.Plant.plant_model import PlantModel
from Modules.Scenario.scenario import Event, Scenario, ScenarioPlayer


def mass(moles):
    return sum(n * COMPONENTS[k].molar_mass for k, n in moles.items())


class KineticsTests(unittest.TestCase):
    def setUp(self):
        use_default_chemistry()

    def test_reversible_reaction_reaches_equilibrium(self):
        m = Material.from_fractions({"water": 0.9, "C": 0.1}, 100.0, 25.0)
        moles = dict(m.moles)
        temp = 25.0
        for _ in range(3000):
            temp, _ = kinetics.advance(moles, temp, [REACTIONS["C_dimerization"]], 10.0)
        v = sum(n * COMPONENTS[k].molar_volume for k, n in moles.items())
        k_eq = REACTIONS["C_dimerization"].equilibrium_constant(temp)
        self.assertAlmostEqual((moles["D"] / v) / (moles["C"] / v) ** 2 / k_eq, 1.0, delta=0.02)
        self.assertGreater(moles["D"], 0)

    def test_equilibrium_shifts_with_temperature(self):
        rx = REACTIONS["C_dimerization"]
        self.assertLess(rx.equilibrium_constant(80.0), rx.equilibrium_constant(25.0))   # exothermic: hot favours C
        self.assertIsNone(REACTIONS["A_B_to_C"].equilibrium_constant(25.0))

    def test_reversible_needs_equilibrium_constant(self):
        with self.assertRaises(ValueError):
            Reaction("bad", {"A": -1, "C": 1}, 1.0, 0.0, reversible=True)

    def test_mass_is_conserved_with_parallel_and_consecutive_reactions(self):
        m = Material.from_fractions({"water": 0.7, "A": 0.1, "B": 0.1, "I": 0.05, "C": 0.05}, 100.0, 40.0)
        moles, before = dict(m.moles), mass(m.moles)
        kinetics.advance(moles, 40.0, [REACTIONS[n] for n in ("A_B_to_C", "A_I_to_X", "C_dimerization")], 600.0)
        self.assertAlmostEqual(mass(moles) / before, 1.0, places=9)
        self.assertGreater(moles["X"], 0)
        self.assertGreater(moles["D"], 0)

    def test_heat_follows_extent_and_kirchhoff(self):
        rx = REACTIONS["A_B_to_C"]
        self.assertAlmostEqual(rx.enthalpy_at(25.0), -140e3)
        self.assertNotAlmostEqual(rx.enthalpy_at(85.0), -140e3, places=0)
        m = Material.from_fractions({"water": 0.9, "A": 0.05, "B": 0.05}, 100.0, 25.0)
        moles, a0 = dict(m.moles), m.moles["A"]
        temp, heat = kinetics.advance(moles, 25.0, [rx], 200.0)
        extent = a0 - moles["A"]
        self.assertGreater(extent, 0)
        self.assertAlmostEqual(heat / (-rx.enthalpy_at(30.0) * extent), 1.0, delta=0.02)
        self.assertGreater(temp, 25.0)

    def test_adiabatic_reactor_temperature_matches_energy_balance(self):
        r = ContinuousStirredTankReactor("R", initial_volume=100.0, composition={"water": 0.9, "A": 0.05, "B": 0.05},
                                         temperature=25.0, reactions=["A_B_to_C"])
        cp0 = r.contents.heat_capacity
        a0 = r.contents.moles["A"]
        for _ in range(20):
            r.update(10.0)
        extent = a0 - r.contents.moles["A"]
        rise = r.temperature - 25.0
        self.assertGreater(rise, 0.0)
        self.assertAlmostEqual(rise, 140e3 * extent / cp0, delta=0.05 * rise)

    def test_fast_hot_step_stays_finite_and_non_negative(self):
        r = ContinuousStirredTankReactor("R", initial_volume=100.0, composition={"A": 0.45, "B": 0.45, "water": 0.1},
                                         temperature=60.0, reactions=["A_B_to_C", "A_I_to_X"])
        r.update(500.0)
        self.assertTrue(all(n >= 0 for n in r.contents.moles.values()))
        self.assertTrue(r.temperature < 2000)


class MigrationTests(unittest.TestCase):
    def test_old_reaction_table_is_migrated(self):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        with Session(engine) as s:
            for col in ("reversible", "k_equilibrium_ref", "reverse_orders"):
                s.execute(text(f"ALTER TABLE reaction DROP COLUMN {col}"))
            s.commit()
            seed_chemistry(s)
            load_chemistry(s)
            self.assertTrue(REACTIONS["C_dimerization"].reversible)
            self.assertAlmostEqual(REACTIONS["C_dimerization"].k_equilibrium_ref, 50.0)
        use_default_chemistry()


class BatchTests(unittest.TestCase):
    def setUp(self):
        use_default_chemistry()

    def test_make_lot_assay_and_impurities(self):
        base = {"water": 0.8, "A": 0.2}
        self.assertAlmostEqual(sum(make_lot(base).values()), 1.0)
        weak = make_lot(base, assay=0.5)
        self.assertLess(weak["A"], 0.2)
        dirty = make_lot(base, impurities={"I": 0.02})
        self.assertAlmostEqual(dirty["I"], 0.02 / 1.02)
        self.assertAlmostEqual(sum(dirty.values()), 1.0)
        with self.assertRaises(ValueError):
            make_lot(base, assay=-1)

    def test_random_lot_is_reproducible(self):
        a = random_lot({"water": 0.8, "A": 0.2}, random.Random(3), impurity="I")
        b = random_lot({"water": 0.8, "A": 0.2}, random.Random(3), impurity="I")
        self.assertEqual(a, b)

    def test_impure_lot_makes_off_spec_product(self):
        def run(composition):
            r = ContinuousStirredTankReactor("R", initial_volume=100.0, composition=composition,
                                             temperature=30.0, reactions=["A_B_to_C", "A_I_to_X"])
            for _ in range(30):
                r.update(20.0)
            return r.contents.moles
        base = {"water": 0.8, "A": 0.1, "B": 0.1}
        clean = run(make_lot(base))
        dirty = run(make_lot(base, impurities={"I": 0.03}))
        self.assertEqual(clean.get("X", 0.0), 0.0)
        self.assertGreater(dirty["X"], 0.0)
        self.assertLess(dirty["C"], clean["C"])

    def test_source_lot_state_round_trip(self):
        s = Source("S", composition={"water": 1.0})
        s.set_lot({"water": 0.9, "A": 0.1}, "L1")
        state = s.get_state()
        s.set_lot({"water": 1.0}, "")
        s.set_state(state)
        self.assertEqual(s.lot, "L1")
        self.assertAlmostEqual(s.composition["A"], 0.1)


def lab_plant():
    p = PlantModel()
    p.add_equipment(Source("S", composition={"water": 0.9, "A": 0.1}, outlets=["out"]))
    p.add_equipment(Vessel("T", inlets=["in"], outlets=["out"], max_capacity=100.0, initial_volume=50.0,
                           composition={"water": 0.9, "A": 0.1}))
    return p


class LabTests(unittest.TestCase):
    def setUp(self):
        use_default_chemistry()

    def test_sample_removes_liquid_and_result_is_delayed(self):
        p = lab_plant()
        p.lab = Laboratory(p, turnaround=60.0, seed=1)
        before = p.equipment["T"].volume
        p.lab.take_sample("T")
        self.assertAlmostEqual(before - p.equipment["T"].volume, p.lab.sample_volume, places=6)
        self.assertIsNone(p.lab.latest("T"))
        for _ in range(100):
            p.step(0.5)
        self.assertIsNone(p.lab.latest("T"))        # 50 s: still in the lab
        for _ in range(30):
            p.step(0.5)
        result = p.lab.latest("T")
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result.taken_at, 0.0)
        self.assertGreater(result.values["wt.A"], 0.0)

    def test_measurement_noise_and_detection_limit(self):
        p = lab_plant()
        p.lab = Laboratory(p, turnaround=0.0, seed=2, rel_error=0.05)
        truth = 100.0 * p.equipment["T"].contents.mass_fractions()["A"]
        for _ in range(60):
            p.lab.take_sample("T")
        p.step(0.5)
        values = [r.values["wt.A"] for r in p.lab.results]
        self.assertGreater(max(values) - min(values), 0.0)
        self.assertAlmostEqual(sum(values) / len(values), truth, delta=0.05 * truth)
        self.assertEqual(p.lab.results[0].values.get("wt.I", 0.0), 0.0)

    def test_source_sampling_and_spec_check(self):
        p = lab_plant()
        p.equipment["S"].set_lot(make_lot({"water": 0.9, "A": 0.1}, assay=0.5), "weak-1")
        p.lab = Laboratory(p, turnaround=0.0, seed=3)
        p.lab.specs["S"] = {"wt.A": (20.0, None)}
        p.lab.take_sample("S")
        p.step(0.5)
        r = p.lab.latest("S")
        self.assertEqual(r.lot, "weak-1")
        self.assertIn("wt.A", r.out_of_spec)

    def test_cannot_sample_the_wrong_things(self):
        p = lab_plant()
        p.add_equipment(Sink("K", inlets=["in"]))
        with self.assertRaises(KeyError):
            p.lab.take_sample("nope")
        with self.assertRaises(ValueError):
            p.lab.take_sample("K")
        p.equipment["T"].contents = Material({}, 20.0)
        with self.assertRaises(ValueError):
            p.lab.take_sample("T")

    def test_lab_channels_have_a_fixed_shape_and_hold_values(self):
        p = lab_plant()
        p.lab = Laboratory(p, turnaround=1.0, seed=4)
        sampler = with_lab(lambda: {"X.value": 1.0}, p.lab, ["T"])
        first = sampler()
        self.assertEqual(first["LAB.T.wt.A"], 0.0)
        p.lab.take_sample("T")
        for _ in range(10):
            p.step(0.5)
        second = sampler()
        self.assertEqual(set(first), set(second))
        self.assertGreater(second["LAB.T.wt.A"], 0.0)
        self.assertLess(second["LAB.T.age"], first["LAB.T.age"] + 100)


class ScenarioBatchTests(unittest.TestCase):
    def setUp(self):
        use_default_chemistry()

    def test_feed_lot_changes_then_restores_the_source(self):
        p = lab_plant()
        sc = Scenario("bad lot")
        sc.add(Event(1.0, "feed_lot", "S", value=0.8, duration=5.0, data={"impurities": {"I": 0.05}, "lot": "L9"}))
        player = ScenarioPlayer(p, sc)
        for _ in range(4):
            p.step(0.5)
            player.update()
        self.assertIn("I", p.equipment["S"].composition)
        self.assertEqual(p.equipment["S"].lot, "L9")
        for _ in range(20):
            p.step(0.5)
            player.update()
        self.assertNotIn("I", p.equipment["S"].composition)
        self.assertEqual(p.equipment["S"].lot, "")

    def test_charge_adds_a_batch_and_sample_event_queues_a_sample(self):
        p = lab_plant()
        sc = Scenario("batch")
        sc.add(Event(0.0, "charge", "T", data={"composition": {"B": 0.5, "water": 0.5}, "volume": 10.0,
                                               "temperature": 40.0}))
        sc.add(Event(1.0, "sample", "T"))
        player = ScenarioPlayer(p, sc)
        v0 = p.equipment["T"].volume
        player.update()
        self.assertAlmostEqual(p.equipment["T"].volume, v0 + 10.0, places=3)
        self.assertGreater(p.equipment["T"].contents.moles["B"], 0.0)
        p.time = 1.0
        player.update()
        self.assertEqual(len(p.lab.pending), 1)

    def test_new_events_validate_and_round_trip(self):
        with self.assertRaises(ValueError):
            Scenario("x").add(Event(0.0, "charge", "T", data={}))
        with self.assertRaises(ValueError):
            Scenario("x").add(Event(0.0, "sample"))
        sc = Scenario("rt")
        sc.add(Event(2.0, "feed_lot", "S", value=0.9, data={"impurities": {"I": 0.01}}))
        again = Scenario.from_dict(sc.to_dict())
        self.assertEqual(again.events[0].data, {"impurities": {"I": 0.01}})
        old = {"name": "old", "events": [{"time": 1.0, "action": "repair", "target": "T"}]}
        self.assertEqual(Scenario.from_dict(old).events[0].data, {})


if __name__ == "__main__":
    unittest.main()
