import os
import sys
import tempfile
import unittest

import numpy as np

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from Data.loader import load_plant
from Modules.Editor.document import PlantDocument
from Modules.Scenario.scenario import Event, Scenario, ScenarioPlayer
from Modules.Watchdog.model import RiskModel, auc
from Modules.Watchdog.recorder import SignalRecorder, plant_sampler
from Modules.Watchdog.residuals import ResidualMonitor, add_residuals, discover_residuals, with_residuals
from Modules.Watchdog.trees import BoostedTrees


class BoostedTreeTests(unittest.TestCase):
    def data(self, n=1500, seed=0):
        rng = np.random.default_rng(seed)
        X = rng.normal(size=(n, 6))
        y = ((X[:, 0] > 0.0) != (X[:, 1] > 0.0)).astype(float)       # XOR: no linear model can represent it
        return X, y

    def test_learns_an_interaction_better_than_linear(self):
        X, y = self.data()
        Xt, yt = self.data(seed=1)
        tree = BoostedTrees(rounds=40).fit(X, y)
        linear = RiskModel(kind="logistic").fit(X, y)
        self.assertGreater(auc(yt, tree.predict_proba(Xt)), 0.95)
        self.assertGreater(auc(yt, tree.predict_proba(Xt)), auc(yt, linear.predict_proba(Xt)) + 0.05)
        self.assertEqual(set(np.argsort(tree.importance)[-2:]), {0, 1})

    def test_contributions_add_up_to_the_score(self):
        X, y = self.data(600)
        tree = BoostedTrees(rounds=20).fit(X, y)
        x = X[y > 0.5][0]
        total = tree.base + sum(tree.value[r, 0] for r in range(len(tree.value))) + tree.contributions(x).sum()
        self.assertAlmostEqual(total, float(tree.decision(x)[0]), places=6)

    def test_risk_model_boosted_save_load_and_explain(self):
        X, y = self.data(600)
        names = [f"c{i}:last" for i in range(6)]
        m = RiskModel(["c"], names, 5, 1.0, 10.0, "T.Fault", kind="boosted").fit(X, y, rounds=15)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "m.npz")
            m.save(path)
            loaded = RiskModel.load(path)
        self.assertEqual(loaded.kind, "boosted")
        np.testing.assert_allclose(loaded.predict_proba(X), m.predict_proba(X))
        self.assertTrue(loaded.explain(X[y > 0.5][0]))

    def test_unknown_kind(self):
        with self.assertRaises(ValueError):
            RiskModel(kind="magic").fit(np.zeros((4, 2)), np.zeros(4))


class ResidualTests(unittest.TestCase):
    def setUp(self):
        self.session = Session(create_engine("sqlite://"))
        doc = PlantDocument.create(self.session, "Line")
        s = doc.add_node("source")
        v = doc.add_node("valve", params={"initial_position": 1.0})
        p = doc.add_node("pipe")
        k = doc.add_node("sink")
        for a, b in ((s, v), (v, p), (p, k)):
            doc.connect_auto(a, b)
        self.fa = doc.add_probe("flow_sensor", v)
        self.fb = doc.add_probe("flow_sensor", p)
        self.pipe = p
        self.specs = discover_residuals(doc.nodes(), doc.edges(), doc.probes())
        self.plant = load_plant(self.session, "Line")

    def tearDown(self):
        self.session.close()

    def record(self, seconds, player=None):
        rec = SignalRecorder(5.0)
        sample = with_residuals(plant_sampler(self.plant), self.specs)
        for _ in range(int(seconds / 0.5)):
            self.plant.step(0.5)
            if player:
                player.update()
            rec.record(self.plant.time, sample())
        return rec.arrays()

    def test_discovers_the_balance_between_two_flow_sensors(self):
        self.assertEqual([s.name for s in self.specs], [f"RES.{self.fa}-{self.fb}.balance"])

    def test_leak_is_visible_as_a_flow_difference(self):
        self.plant.set_fault(self.pipe, "leak_rate", 0.4)
        for _ in range(10):
            self.plant.step(0.5)
        flows = {f"{t}.Value": self.plant.instruments[t].true_value() for t in (self.fa, self.fb)}
        self.assertGreater(flows[f"{self.fa}.Value"] - flows[f"{self.fb}.Value"], 0.15)

    def test_monitor_detects_a_leak_and_stays_quiet_when_healthy(self):
        times, matrix, names = self.record(900)
        names_res = [s.name for s in self.specs]
        monitor = ResidualMonitor(names_res).fit(times, matrix, names)
        cols = [names.index(n) for n in names_res]

        def worst(rows):
            scores = []
            m = ResidualMonitor(names_res)
            m.detector = monitor.detector
            for row in rows:
                out = m.update(dict(zip(names_res, row[cols])))
                if out:
                    scores.append(out[0])
            return max(scores)

        healthy = self.record(600)
        self.assertLess(worst(healthy[1]), 5.0)
        scenario = Scenario("leak")
        scenario.add(Event(0, "fault", self.pipe, "leak_rate", 0.4))
        leaking = self.record(600, ScenarioPlayer(self.plant, scenario))
        self.assertGreater(worst(leaking[1]), 5.0)

    def test_add_residuals_to_a_recording(self):
        times, matrix, names = self.record(60)
        raw_names = [n for n in names if not n.startswith("RES.")]
        cols = [names.index(n) for n in raw_names]
        t2, m2, n2 = add_residuals(times, matrix[:, cols], raw_names, self.specs)
        self.assertEqual(n2[-1], self.specs[0].name)
        self.assertEqual(m2.shape[1], len(raw_names) + 1)


if __name__ == "__main__":
    unittest.main()
