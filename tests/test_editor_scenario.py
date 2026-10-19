import os
import sys
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from Data.loader import load_plant
from Data.scenarios import delete_scenario, list_scenarios, load_scenario, save_scenario
from Modules.Editor.document import PlantDocument
from Modules.Scenario.scenario import Event, PRESETS, Scenario, ScenarioPlayer, brownout, leak, snow_day


def make_loop(session):
    """source -> valve -> pipe -> sink, built through the editor."""
    doc = PlantDocument.create(session, "Loop")
    s = doc.add_node("source", 0, 0)
    v = doc.add_node("valve", 100, 0, params={"initial_position": 1.0})
    p = doc.add_node("pipe", 200, 0)
    k = doc.add_node("sink", 300, 0)
    doc.connect_auto(s, v)
    doc.connect_auto(v, p)
    doc.connect_auto(p, k)
    return doc, (s, v, p, k)


class EditorTests(unittest.TestCase):
    def setUp(self):
        self.session = Session(create_engine("sqlite://"))

    def tearDown(self):
        self.session.close()

    def test_build_validate_and_run(self):
        doc, (s, v, p, k) = make_loop(self.session)
        self.assertEqual(doc.validate(), [])
        self.assertEqual([n.tag for n in doc.nodes()], [s, v, p, k])
        self.assertEqual(len(doc.edges()), 3)
        model = load_plant(self.session, "Loop")
        for _ in range(20):
            model.step(0.5)
        self.assertGreater(model.equipment[p].flow, 0.0)

    def test_position_is_stored_and_ignored_by_loader(self):
        doc, (s, *_rest) = make_loop(self.session)
        doc.move_node(s, 12, 34)
        node = doc.node(s)
        self.assertEqual((node.x, node.y), (12.0, 34.0))
        self.assertNotIn("_ui", node.params)
        load_plant(self.session, "Loop")

    def test_rejects_bad_connections(self):
        doc = PlantDocument.create(self.session, "Bad")
        tank = doc.add_node("storage_tank")
        gas = doc.add_node("gas_volume")
        with self.assertRaises(ValueError):
            doc.connect(tank, "out1", gas, "in1")           # liquid -> gas
        with self.assertRaises(ValueError):
            doc.connect(tank, "out1", tank, "in1")          # itself
        pump = doc.add_node("pump")
        doc.connect(tank, "out1", pump, "in")
        with self.assertRaises(ValueError):
            doc.connect(tank, "out1", pump, "in")           # already used

    def test_delete_node_removes_connections_and_probes(self):
        doc, (s, v, p, k) = make_loop(self.session)
        doc.add_probe("flow_sensor", p)
        doc.delete_node(p)
        self.assertEqual(len(doc.edges()), 1)
        self.assertEqual(doc.probes(), [])
        self.assertEqual(len(doc.nodes()), 3)

    def test_disconnect_frees_ports(self):
        doc, (s, v, p, k) = make_loop(self.session)
        doc.disconnect(v, "out", p, "in")
        self.assertEqual(doc.free_ports(p, "in"), ["in"])
        self.assertEqual(len(doc.edges()), 2)

    def test_unique_generated_tags_and_unknown_type(self):
        doc = PlantDocument.create(self.session, "Tags")
        a, b = doc.add_node("pump"), doc.add_node("pump")
        self.assertNotEqual(a, b)
        with self.assertRaises(ValueError):
            doc.add_node("warp_core")

    def test_save_and_revert(self):
        doc, _ = make_loop(self.session)
        doc.save()
        doc.add_node("pump")
        doc.revert()
        self.assertEqual(len(doc.nodes()), 4)

    def test_port_added_to_manifold(self):
        doc = PlantDocument.create(self.session, "M")
        m = doc.add_node("manifold")
        self.assertEqual(doc.add_port(m, "in"), "in3")
        self.assertEqual(doc.node(m).inlets, ["in1", "in2", "in3"])


class ScenarioTests(unittest.TestCase):
    def setUp(self):
        self.session = Session(create_engine("sqlite://"))
        self.doc, self.tags = make_loop(self.session)
        self.plant = load_plant(self.session, "Loop")

    def tearDown(self):
        self.session.close()

    def run_for(self, player, seconds, dt=1.0):
        for _ in range(int(seconds / dt)):
            self.plant.step(dt)
            player.update()

    def test_leak_event_sets_and_reverts(self):
        pipe = self.tags[2]
        s = Scenario("t")
        s.add(Event(5, "fault", pipe, "leak_rate", 0.3, duration=10))
        player = ScenarioPlayer(self.plant, s)
        self.run_for(player, 8)
        self.assertEqual(self.plant.equipment[pipe].leak_rate, 0.3)
        self.run_for(player, 10)
        self.assertEqual(self.plant.equipment[pipe].leak_rate, 0.0)
        self.assertTrue(player.finished)

    def test_ramp_grows_gradually(self):
        pipe = self.tags[2]
        player = ScenarioPlayer(self.plant, leak(pipe, 1.0, start=0, grow_over=100))
        self.run_for(player, 50)
        self.assertTrue(0.3 < self.plant.equipment[pipe].leak_rate < 0.7)
        self.run_for(player, 60)
        self.assertAlmostEqual(self.plant.equipment[pipe].leak_rate, 1.0)

    def test_weather_and_supply_events(self):
        s = brownout(0.2, duration=20, start=0)
        s.add(Event(0, "ambient_offset", value=-10, duration=20))
        player = ScenarioPlayer(self.plant, s)
        self.run_for(player, 10)
        self.assertAlmostEqual(self.plant.power.offset, -0.2)
        self.assertAlmostEqual(self.plant.environment.offset, -10)
        self.run_for(player, 20)
        self.assertEqual(self.plant.power.offset, 0.0)
        self.assertEqual(self.plant.environment.offset, 0.0)

    def test_supply_dip_event(self):
        s = Scenario("t")
        s.add(Event(2, "supply_dip", value=0.3, duration=5))
        player = ScenarioPlayer(self.plant, s)
        self.run_for(player, 4)
        self.assertAlmostEqual(self.plant.power.voltage_factor, 0.7)

    def test_validation(self):
        s = Scenario("t")
        for bad in (Event(0, "explode"), Event(0, "fault"), Event(0, "fault", "X"), Event(-1, "repair", "X"),
                    Event(0, "supply_dip", value=0.1)):
            with self.assertRaises(ValueError):
                s.add(bad)

    def test_param_must_be_numeric(self):
        s = Scenario("t")
        s.add(Event(0, "param", self.tags[2], "no_such_attribute", 1.0))
        with self.assertRaises(ValueError):
            ScenarioPlayer(self.plant, s).update()

    def test_presets_and_persistence(self):
        for name, factory in PRESETS.items():
            self.assertTrue((factory("X") if name == "leak" else factory()).events)
        sc = snow_day(duration=100)
        save_scenario(self.session, "Loop", sc)
        self.assertEqual(load_scenario(self.session, "Loop", sc.name).to_dict(), sc.to_dict())
        self.assertEqual(list_scenarios(self.session, "Loop"), [sc.name])
        delete_scenario(self.session, "Loop", sc.name)
        self.assertEqual(list_scenarios(self.session, "Loop"), [])


class FreezeThawTests(unittest.TestCase):
    def setUp(self):
        self.session = Session(create_engine("sqlite://"))
        self.doc, self.tags = make_loop(self.session)
        self.doc.set_params(self.tags[2], {"diameter": 0.05, "length": 8.0, "fatigue_per_cycle": 0.05,
                                           "ambient_ua": 50.0, "thermal_mass": 1e3})
        self.plant = load_plant(self.session, "Loop")
        for _ in range(5):
            self.plant.step(1.0)            # the pipe learns what it carries
        self.pipe = self.plant.equipment[self.tags[2]]
        self.pipe.flowing = False

    def tearDown(self):
        self.session.close()

    def test_cycles_accumulate_fatigue_and_leak(self):
        self.assertIsNotNone(self.pipe.contents)
        self.assertEqual(self.pipe.effective_leak, 0.0)
        for _ in range(20):
            for outdoor in (-10.0, 10.0):
                for _ in range(60):
                    self.pipe.slow_update(10.0, outdoor)
        self.assertGreaterEqual(self.pipe.freeze_thaw_cycles, 19)
        self.assertGreater(self.pipe.fatigue, 0.5)
        self.assertGreater(self.pipe.effective_leak, 0.0)
        self.pipe.repair()
        self.assertEqual((self.pipe.fatigue, self.pipe.freeze_thaw_cycles, self.pipe.effective_leak), (0.0, 0, 0.0))

    def test_fatigue_state_round_trip(self):
        self.pipe.fatigue, self.pipe.freeze_thaw_cycles = 0.4, 7
        state = self.pipe.get_state()
        self.pipe.fatigue, self.pipe.freeze_thaw_cycles = 0.0, 0
        self.pipe.set_state(state)
        self.assertEqual((self.pipe.fatigue, self.pipe.freeze_thaw_cycles), (0.4, 7))

    def test_no_cycles_without_freezing(self):
        for _ in range(500):
            self.pipe.slow_update(10.0, 8.0)
        self.assertEqual(self.pipe.freeze_thaw_cycles, 0)

    def test_leak_reduces_flow_to_the_sink(self):
        self.pipe.fatigue = 1.0
        self.pipe.flowing = True
        self.plant.step(1.0)
        self.assertGreater(self.pipe.effective_leak, 0.4)


if __name__ == "__main__":
    unittest.main()
