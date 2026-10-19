import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from Data.loader import load_plant
from Modules.Editor.document import PlantDocument
from Modules.Watchdog.health import ALARM, HealthMonitor, measurement_sampler
from Modules.Watchdog.residuals import discover_residuals, with_residuals


class HealthMonitorTests(unittest.TestCase):
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
        self.valve, self.pipe, self.sink = v, p, k
        specs = discover_residuals(doc.nodes(), doc.edges(), doc.probes())
        self.plant = load_plant(self.session, "Line")
        self.sample = with_residuals(measurement_sampler(self.plant), specs)
        self.monitor = HealthMonitor(doc.nodes(), doc.probes(), doc.edges(), specs, learn_samples=40)

    def tearDown(self):
        self.session.close()

    def run_for(self, seconds, every=2.0):
        for _ in range(int(seconds / every)):
            for _ in range(4):
                self.plant.step(every / 4)
            self.monitor.update(self.sample(), self.plant.time)

    def test_healthy_plant_stays_ok_after_learning(self):
        self.run_for(600)
        self.assertLess(max(self.monitor.risks().values()), 0.3)
        self.assertEqual(self.monitor.health[self.pipe].status, "ok")

    def test_leak_is_charged_to_the_equipment_on_that_line(self):
        self.run_for(300)
        self.plant.set_fault(self.pipe, "leak_rate", 0.4)
        self.run_for(300)
        self.assertGreaterEqual(self.monitor.health[self.pipe].risk, ALARM)
        self.assertTrue(self.monitor.reasons(self.pipe))
        self.assertLess(self.monitor.health[self.sink].risk, 0.3)

    def test_repair_resets_the_picture(self):
        self.run_for(300)
        self.plant.set_fault(self.pipe, "leak_rate", 0.4)
        self.run_for(200)
        self.assertGreaterEqual(self.monitor.health[self.pipe].risk, ALARM)
        self.plant.repair(self.pipe)
        self.assertEqual(self.plant.maintenance_log[-1][1], self.pipe)
        self.monitor.maintenance(self.pipe, self.plant.time)
        self.assertEqual(self.monitor.health[self.pipe].status, "learning")
        self.run_for(400)
        self.assertLess(self.monitor.health[self.pipe].risk, 0.3)
        self.assertEqual(self.monitor.health[self.pipe].repairs, 1)

    def test_a_growing_leak_gives_a_forecast_before_the_alarm(self):
        self.run_for(300)
        seen = None
        for k in range(1, 200):
            self.plant.set_fault(self.pipe, "leak_rate", 0.002 * k)
            self.run_for(10)
            h = self.monitor.health[self.pipe]
            if 0.1 < h.risk < ALARM and self.monitor.forecast(self.pipe) is not None:
                seen = self.monitor.forecast(self.pipe)
                break
        self.assertIsNotNone(seen)
        self.assertGreater(seen, 0.0)

    def test_equipment_without_sensors_is_reported_as_unmonitored(self):
        self.run_for(100)
        self.assertEqual(self.monitor.health[self.sink].status, "unmonitored")


if __name__ == "__main__":
    unittest.main()
