import os
import sys
import tempfile
import unittest

import numpy as np

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from Modules.Entity.Process_equipment.Motor.motor import Motor
from Modules.Plant.power_supply import PowerSupply
from Modules.Watchdog.dataset import build_dataset, rising_edges
from Modules.Watchdog.features import window_features
from Modules.Watchdog.model import AnomalyDetector, RiskModel, auc
from Modules.Watchdog.recorder import SignalRecorder, load_csv
from Modules.Watchdog.watchdog import Watchdog


def run_motor(motor, supply, seconds, dt=1.0, load=1.0):
    motor.supply = supply
    reason = ""
    t = 0.0
    while t < seconds and not reason:
        supply.update(t, dt)
        reason = motor.update(dt, load, True)
        t += dt
    return reason


class PowerSupplyTests(unittest.TestCase):
    def test_perfect_by_default(self):
        s = PowerSupply()
        s.update(0.0, 1.0)
        self.assertEqual(s.voltage_factor, 1.0)

    def test_seeded_noise_is_deterministic(self):
        a, b = PowerSupply(noise=0.02, seed=7), PowerSupply(noise=0.02, seed=7)
        for i in range(50):
            a.update(i, 1.0)
            b.update(i, 1.0)
        self.assertEqual(a.voltage_factor, b.voltage_factor)
        self.assertNotEqual(a.voltage_factor, 1.0)

    def test_dip_event(self):
        s = PowerSupply()
        s.add_event(10.0, 5.0, 0.3)
        s.update(5.0, 1.0)
        self.assertAlmostEqual(s.voltage_factor, 1.0)
        s.update(12.0, 1.0)
        self.assertAlmostEqual(s.voltage_factor, 0.7)
        s.update(20.0, 1.0)
        self.assertAlmostEqual(s.voltage_factor, 1.0)

    def test_state_round_trip(self):
        s = PowerSupply(noise=0.01, seed=3)
        s.offset = -0.05
        s.add_event(1, 2, 0.1)
        t = PowerSupply()
        t.set_state(s.get_state())
        self.assertEqual(t.offset, -0.05)
        self.assertEqual(t.events, s.events)


class MotorTests(unittest.TestCase):
    def test_nominal_supply_is_neutral(self):
        m = Motor()
        self.assertEqual(run_motor(m, PowerSupply(), 3600), "")
        self.assertAlmostEqual(m.current, m.rated_current, delta=0.01 * m.rated_current)
        self.assertLess(m.insulation_used, 0.01)

    def test_sag_raises_current_and_overheats(self):
        m = Motor()
        s = PowerSupply()
        s.offset = -0.20
        self.assertEqual(run_motor(m, s, 6000), "overtemperature")
        self.assertGreater(m.current, 1.1 * m.rated_current)

    def test_undervoltage_trip(self):
        m = Motor()
        s = PowerSupply()
        s.offset = -0.35
        self.assertEqual(run_motor(m, s, 10), "undervoltage")

    def test_burnout_needs_repair(self):
        m = Motor()
        m.insulation_used = 1.0
        m.supply = PowerSupply()
        self.assertEqual(m.update(1.0, 1.0, True), "burnout")
        self.assertFalse(m.reset())
        m.repair()
        self.assertTrue(m.reset())
        self.assertEqual(m.update(1.0, 1.0, True), "")

    def test_insulation_ages_faster_when_hot(self):
        cool, hot = Motor(), Motor()
        cool.winding_temp, hot.winding_temp = 80.0, 125.0
        cool.slow_update(3600, 20.0)
        hot.slow_update(3600, 20.0)
        self.assertGreater(hot.insulation_used, 10 * cool.insulation_used)

    def test_state_round_trip(self):
        m = Motor()
        m.insulation_used, m.winding_temp = 0.4, 90.0
        n = Motor()
        n.set_state(m.get_state())
        self.assertEqual(n.insulation_used, 0.4)
        self.assertEqual(n.winding_temp, 90.0)


class FeatureAndDatasetTests(unittest.TestCase):
    def test_window_features_on_ramp(self):
        w = np.arange(10, dtype=float).reshape(-1, 1)
        last, mean, std, trend = window_features(w)
        self.assertEqual(last, 9.0)
        self.assertAlmostEqual(trend, 9.0)
        self.assertAlmostEqual(mean, 4.5)
        self.assertGreater(std, 0)

    def test_rising_edges(self):
        t = np.arange(8.0)
        self.assertEqual(list(rising_edges(t, [0, 1, 1, 0, 0, 1, 1, 1])), [1.0, 5.0])

    def test_build_dataset_labels_and_hides_target(self):
        n = 100
        times = np.arange(n, dtype=float)
        matrix = np.zeros((n, 2))
        matrix[:, 0] = np.arange(n)
        matrix[80:, 1] = 1.0                    # trip at t = 80
        X, y, channels = build_dataset(times, matrix, ["A.x", "T.Fault"], "T.Fault", horizon=10, window=5)
        self.assertEqual(channels, ["A.x"])
        self.assertEqual(X.shape[1], 4)
        self.assertEqual(len(X), len(y))
        self.assertEqual(y.sum(), 10)           # window ends in [70, 80) are positive
        self.assertEqual(len(y), 80 - 4)        # rows after the trip are dropped

    def test_missing_target(self):
        with self.assertRaises(ValueError):
            build_dataset(np.arange(5.0), np.zeros((5, 1)), ["a"], "nope", 1, 2)


class ModelTests(unittest.TestCase):
    def test_auc(self):
        self.assertEqual(auc(np.array([0, 0, 1, 1]), np.array([.1, .2, .8, .9])), 1.0)
        self.assertEqual(auc(np.array([0, 1, 0, 1]), np.array([.5, .5, .5, .5])), 0.5)

    def test_risk_model_fit_and_save_load(self):
        rng = np.random.default_rng(0)
        X = rng.normal(size=(400, 4))
        y = (X[:, 0] + 0.2 * rng.normal(size=400) > 1.0).astype(float)
        m = RiskModel(channels=["a"], feature_names=["a.last", "a.mean", "a.std", "a.trend"],
                      window=5, period=1.0, horizon=10.0, target="T.Fault")
        m.fit(X, y)
        self.assertGreater(auc(y, m.predict_proba(X)), 0.9)
        m.metadata = {"training_sources": [{"kind": "historian"}]}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "m.npz")
            m.save(path)
            loaded = RiskModel.load(path)
        np.testing.assert_allclose(loaded.predict_proba(X), m.predict_proba(X))
        self.assertEqual(loaded.channels, ["a"])
        self.assertEqual(loaded.model_version, "plsim-watchdog-v2")
        self.assertEqual(loaded.metadata, m.metadata)
        self.assertTrue(m.explain(X[0]))

    def test_anomaly_detector(self):
        rng = np.random.default_rng(1)
        d = AnomalyDetector()
        d.fit(rng.normal(size=(300, 3)))
        self.assertGreater(d.score(np.array([[10.0, 0, 0]]))[0], d.score(np.zeros((1, 3)))[0])


class RecorderAndWatchdogTests(unittest.TestCase):
    def test_recorder_period_and_csv(self):
        r = SignalRecorder(period=2.0)
        stored = [r.record(t, {"b": t, "a": 2 * t}) for t in range(6)]
        self.assertEqual(stored, [True, False, True, False, True, False])
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "r.csv")
            r.save_csv(path)
            times, matrix, names = load_csv(path)
        self.assertEqual(names, ["a", "b"])
        np.testing.assert_allclose(times, [0, 2, 4])
        np.testing.assert_allclose(matrix[:, 0], [0, 4, 8])

    def test_alarm_hysteresis(self):
        class Fixed:
            channels, window, period = ["a"], 2, 1.0
            risk = 0.0

            def predict_proba(self, X):
                return np.array([self.risk])

            def explain(self, x, top=3):
                return [("a.last", 1.0)]

        model = Fixed()
        w = Watchdog(model, alarm_threshold=0.8, clear_threshold=0.5, alarm_samples=1, clear_samples=1)
        self.assertIsNone(w.update(0, {"a": 0}))                # window not full yet
        for t, risk, expected in [(1, 0.6, False), (2, 0.9, True), (3, 0.6, True), (4, 0.4, False)]:
            model.risk = risk
            self.assertEqual(w.update(t, {"a": 0}).alarm, expected)
        with self.assertRaises(KeyError):
            w.update(5, {})

    def test_watchdog_rejects_invalid_samples_without_consuming_them(self):
        class Fixed:
            channels, window, period = ["a"], 2, 1.0

            def predict_proba(self, X):
                return np.array([0.2])

            def explain(self, x, top=3):
                return []

        w = Watchdog(Fixed(), alarm_threshold=0.8)
        self.assertIsNone(w.update(0.0, {"a": 1.0}))
        with self.assertRaisesRegex(ValueError, "non-finite"):
            w.update(1.0, {"a": float("nan")})
        self.assertIsNotNone(w.update(1.0, {"a": 1.0}))
        with self.assertRaisesRegex(ValueError, "backwards"):
            w.update(0.5, {"a": 1.0})


class ScenarioTests(unittest.TestCase):
    def test_episode_and_training_beat_chance(self):
        from Modules.Watchdog.training import train_watchdog
        result = train_watchdog(train_seeds=range(0, 8), test_seeds=range(100, 104))
        self.assertIsNotNone(result)

    def test_generated_episodes_use_feedback_not_commands_or_hidden_labels(self):
        from Modules.Watchdog.scenarios import FAILURE_MODES, run_episode
        for seed, mode in enumerate(FAILURE_MODES[:-1]):
            times, matrix, names = run_episode(seed, max_seconds=20.0, sample_period=1.0, failure_mode=mode)
            self.assertIn("P101.Fault", names)
            self.assertFalse(any(name.endswith(("RunCmd", "SpeedSP")) for name in names))
            X, y, channels = build_dataset(times, matrix, names, "P101.Fault", 5.0, 3)
            self.assertFalse(any(name.endswith(".Fault") or name.startswith(("EVENT.", "TRUTH."))
                                 for name in channels))
            self.assertEqual(X.shape[0], len(y))

    def test_episode_metrics_count_lead_and_false_alarms(self):
        from Modules.Watchdog.training import _evaluate_recordings

        class SensorThreshold:
            target = "T.Fault"
            horizon = 3.0
            window = 2
            channels = ["A.x"]
            period = 1.0

            def predict_proba(self, X):
                return X[:, 0]

        times = np.arange(10.0)
        matrix = np.zeros((10, 2))
        matrix[6:, 1] = 1.0
        matrix[8:, 0] = 1.0
        metrics = _evaluate_recordings(SensorThreshold(), [("test", (times, matrix, ["T.Fault", "A.x"]))],
                                       threshold=0.5, stride=1)
        self.assertEqual(metrics["detected_events"], 1)
        self.assertEqual(metrics["false_alarm_episodes"], 0)
        self.assertEqual(metrics["median_lead_time_s"], 1.0)
        self.assertEqual(metrics["event_recall"], 1.0)


if __name__ == "__main__":
    unittest.main()
