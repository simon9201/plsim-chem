import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from Data.build_test_plant import PLANT_NAME, build_test_plant
from Data.loader import load_plant
from Modules.Watchdog.access import OperatorView, audit_hidden_state
from Modules.Watchdog.evaluation import GroundTruth
from Modules.Watchdog.recorder import plant_sampler

# Modules that make up the AI. They may read signals, lab results and work orders (via OperatorView) and
# nothing else about the plant: never equipment objects, fault lists or saved state.
AI_MODULES = ("health", "features", "dataset", "model", "trees", "residuals", "watchdog", "access", "recorder",
              "training")
FORBIDDEN = re.compile(r"\.equipment\b|\.instruments\b|get_state\(|\bFAULTS\b|\.motor\b|"
                       r"Modules\.Watchdog\.evaluation|\bGroundTruth\b")


class AiAccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.session = Session(create_engine("sqlite://"))
        build_test_plant(cls.session)
        cls.plant = load_plant(cls.session, PLANT_NAME)
        for _ in range(20):
            cls.plant.step(0.5)

    @classmethod
    def tearDownClass(cls):
        cls.session.close()

    def test_no_signal_reads_hidden_degradation_directly(self):
        self.assertEqual(audit_hidden_state(self.plant), [])

    def test_audit_catches_a_leaking_signal(self):
        pipe = next(e for e in self.plant.equipment.values() if hasattr(e, "fouling"))
        from Modules.Entity.Entity import Signal, TO_PLC
        original = pipe.signals
        pipe.signals = lambda: original() + [Signal("Cheat", TO_PLC, "REAL", get=lambda: pipe.fouling)]
        try:
            leaks = audit_hidden_state(self.plant)
        finally:
            del pipe.signals
        self.assertIn((pipe.name, "fouling", f"{pipe.name}.Cheat"), leaks)

    def test_view_exposes_no_plant_or_equipment(self):
        view = OperatorView(self.plant)
        public = [n for n in dir(view) if not n.startswith("_")]
        self.assertEqual(sorted(public), ["commands", "feedback", "lab_channels", "measured", "read", "time",
                                          "work_orders"])
        self.assertTrue(view.measured())
        self.assertTrue(all("." in k for k in view.read()))

    def test_plant_sampler_goes_through_the_view(self):
        self.assertEqual(set(plant_sampler(self.plant)()), set(OperatorView(self.plant).feedback()))

    def test_simulated_noise_is_repeatable_and_perturbs_feedback_only(self):
        first = OperatorView(self.plant, noise_fraction=0.02, drift_fraction_per_hour=0.01, seed=91)
        second = OperatorView(self.plant, noise_fraction=0.02, drift_fraction_per_hour=0.01, seed=91)
        ideal = OperatorView(self.plant).feedback()
        noisy_a, noisy_b = first.feedback(), second.feedback()
        self.assertEqual(noisy_a, noisy_b)
        self.assertTrue(any(noisy_a[name] != ideal[name] for name in ideal if name.endswith(("Current", "Speed"))))
        self.assertNotIn("P101.RunCmd", noisy_a)

    def test_simulated_sensor_drift_accumulates_with_time(self):
        from Modules.Entity.Entity import Signal, TO_PLC

        class Clock:
            time = 0.0
            maintenance_log = []

            def signal_table(self):
                return {("M1", "Current"): Signal("Current", TO_PLC, "REAL", "A", get=lambda: 10.0)}

        plant = Clock()
        view = OperatorView(plant, drift_fraction_per_hour=0.1, seed=7)
        initial = view.measured()["M1.Current"]
        plant.time = 3600.0
        after_hour = view.measured()["M1.Current"]
        self.assertNotEqual(after_hour, initial)

    def test_view_exposes_lab_results_only_after_release(self):
        from Modules.Lab.lab import LabResult

        result = LabResult(1, "T101", 1.0, 100.0, {"wt.water": 99.5, "density": 997.0})
        self.plant.lab.pending.append(result)
        view = OperatorView(self.plant, lab=self.plant.lab)
        self.assertFalse(any(name.startswith("LAB.") for name in view.measured()))
        self.plant.lab.pending.clear()
        self.plant.lab.results.append(result)
        values = view.measured()
        self.assertEqual(values["LAB.T101.wt.water"], 99.5)
        self.assertGreater(values["LAB.T101.age"], 0.0)

    def test_work_orders_follow_repairs(self):
        tag = next(iter(self.plant.equipment))
        before = len(OperatorView(self.plant).work_orders())
        self.plant.repair(tag)
        orders = OperatorView(self.plant).work_orders()
        self.assertEqual(len(orders), before + 1)
        self.assertEqual(orders[-1][1], tag)

    def test_ground_truth_is_separate_and_sees_hidden_state(self):
        tag = next(t for t, e in self.plant.equipment.items() if hasattr(e, "fouling"))
        self.plant.equipment[tag].fouling = 0.5
        try:
            self.assertEqual(GroundTruth(self.plant).condition(tag).get("fouling"), 0.5)
            self.assertTrue(GroundTruth(self.plant).unhealthy(tag))
        finally:
            self.plant.equipment[tag].fouling = 0.0

    def test_ai_modules_do_not_touch_plant_internals(self):
        offenders = []
        for name in AI_MODULES:
            path = os.path.join(ROOT, "Modules", "Watchdog", f"{name}.py")
            with open(path, encoding="utf-8") as f:
                for n, line in enumerate(f, 1):
                    if name == "access" and line.startswith("def audit_hidden_state"):
                        break           # the audit deliberately pokes hidden state; the AI never calls it
                    code = line.split("#")[0]
                    if FORBIDDEN.search(code):
                        offenders.append(f"{name}.py:{n}: {line.strip()}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
