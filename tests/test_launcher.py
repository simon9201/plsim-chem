import json
import os
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from Data.exchange import (delete_snapshot, export_design, export_snapshot, import_design, import_snapshot,
                           read_json, write_json)
from Data.loader import load_plant
from Data.snapshots import list_snapshots, load_snapshot, save_snapshot
from Modules.Editor.document import PlantDocument
from tests.test_editor_scenario import make_loop
import launcher


class ExchangeTests(unittest.TestCase):
    def setUp(self):
        self.session = Session(create_engine("sqlite://", poolclass=StaticPool))
        self.doc, self.tags = make_loop(self.session)
        self.doc.set_params(self.tags[1], {"initial_position": 0.4})
        self.session.commit()

    def tearDown(self):
        self.session.close()

    def test_design_round_trip_through_a_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "loop.json")
            write_json(path, export_design(self.session, "Loop"))
            data = read_json(path)
        import_design(self.session, data, "Loop copy")
        a, b = PlantDocument(self.session, "Loop"), PlantDocument(self.session, "Loop copy")
        self.assertEqual([(n.tag, n.type, n.x, n.y, n.z, n.params) for n in a.nodes()],
                         [(n.tag, n.type, n.x, n.y, n.z, n.params) for n in b.nodes()])
        self.assertEqual(a.edges(), b.edges())
        self.assertEqual(b.node(self.tags[1]).params["initial_position"], 0.4)
        load_plant(self.session, "Loop copy").step(0.5)

    def test_import_refuses_existing_name_and_bad_files(self):
        data = export_design(self.session, "Loop")
        with self.assertRaises(ValueError):
            import_design(self.session, data)            # same name
        with self.assertRaises(ValueError):
            import_design(self.session, {"format": "other", "version": 1})
        broken = json.loads(json.dumps(data))
        broken["edges"][0]["to"] = ["NOPE", "in1"]
        with self.assertRaises(Exception):
            import_design(self.session, broken, "Broken")
        self.assertNotIn("Broken", PlantDocument.list_plants(self.session))

    def test_snapshot_export_import_and_delete(self):
        model = load_plant(self.session, "Loop")
        for _ in range(40):
            model.step(0.5)
        save_snapshot(self.session, "Loop", model, "t20")
        data = export_snapshot(self.session, "Loop", "t20")
        import_snapshot(self.session, "Loop", data, "copy")
        self.assertEqual(sorted(n for n, _, _ in list_snapshots(self.session, "Loop")), ["copy", "t20"])
        fresh = load_plant(self.session, "Loop")
        load_snapshot(self.session, "Loop", fresh, "copy")
        self.assertAlmostEqual(fresh.time, model.time)
        with self.assertRaises(ValueError):
            import_snapshot(self.session, "Loop", data, "copy")      # exists
        data["states"]["GHOST"] = {}
        with self.assertRaises(ValueError):
            import_snapshot(self.session, "Loop", data, "bad", overwrite=True)
        delete_snapshot(self.session, "Loop", "copy")
        self.assertEqual([n for n, _, _ in list_snapshots(self.session, "Loop")], ["t20"])


class DemoPlantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from Data.build_demo_plant import build_demo_plant, DEMO_NAME
        cls.session = Session(create_engine("sqlite://", poolclass=StaticPool))
        cls.name = build_demo_plant(cls.session)
        assert cls.name == DEMO_NAME

    @classmethod
    def tearDownClass(cls):
        cls.session.close()

    def test_layout_is_hand_made_and_nodes_do_not_overlap(self):
        doc = PlantDocument(self.session, self.name)
        self.assertFalse(doc.needs_layout())
        nodes = list(doc.nodes())
        spots = {(n.x, n.y) for n in nodes}
        self.assertEqual(len(spots), len(nodes))
        self.assertGreater(len({n.z for n in nodes}), 1)       # some equipment is elevated for the 3D view

    def test_saved_scenarios_start_the_plant_and_it_runs_without_trips(self):
        from Data.scenarios import load_scenario
        from Modules.Scenario.scenario import ScenarioPlayer
        plant = load_plant(self.session, self.name)
        player = ScenarioPlayer(plant, load_scenario(self.session, self.name, "Normal operation"))
        for _ in range(1200):
            player.update()
            plant.step(0.5)
        eq = plant.equipment
        self.assertTrue(eq["P301"].running and eq["P303"].running and eq["CP301"].running)
        self.assertGreater(eq["GV301"].pressure, 3.0)         # the compressor fills the buffer
        self.assertFalse(any(getattr(e, "tripped", False) for e in eq.values()))
        for name in ("Demo upset", "Leak on TK301"):
            try:
                load_scenario(self.session, self.name, name)
            except Exception:
                self.fail(f"scenario '{name}' is not saved with the demo plant")

    def test_command_action_writes_only_plc_command_signals(self):
        from Modules.Scenario.scenario import Event, Scenario, ScenarioPlayer
        plant = load_plant(self.session, self.name)
        scenario = Scenario("t")
        scenario.add(Event(0.0, "command", "P301", "RunCmd", 1.0))
        ScenarioPlayer(plant, scenario).update()
        self.assertTrue(plant.equipment["P301"].run_cmd)
        bad = Scenario("bad")
        bad.add(Event(0.0, "command", "P301", "RunningFb", 1.0))
        with self.assertRaises(ValueError):
            ScenarioPlayer(plant, bad).update()


class TrainingPlantTests(unittest.TestCase):
    def test_reactor_training_plant_builds_runs_and_scales(self):
        from Data.build_training_plant import build_training_plant
        from Data.scenarios import load_scenario
        from Modules.Scenario.scenario import ScenarioPlayer
        session = Session(create_engine("sqlite://", poolclass=StaticPool))
        try:
            name = build_training_plant(session)
            self.assertFalse(PlantDocument(session, name).needs_layout())
            plant = load_plant(session, name)
            plant.slow_time_scale = 1000.0
            player = ScenarioPlayer(plant, load_scenario(session, name, "Wall scaling"))
            for _ in range(2 * 3600):
                player.update()
                plant.step(0.5)
            eq = plant.equipment
            self.assertTrue(eq["P401"].running and eq["P402"].running)
            self.assertGreater(eq["PL402"].fouling, 0.02)         # C plates out on the cold wall
            self.assertLess(eq["PL404"].fouling, 0.01)            # the other line is untouched
            self.assertFalse(any(getattr(e, "tripped", False) for e in eq.values()))
        finally:
            session.close()


class LauncherWindowTests(unittest.TestCase):
    def test_auto_layout_spreads_nodes_by_flow(self):
        session = Session(create_engine("sqlite://", poolclass=StaticPool))
        doc, (s, v, p, k) = make_loop(session)
        self.assertFalse(doc.needs_layout())        # make_loop spaces its nodes explicitly
        doc2 = PlantDocument.create(session, "Stack")
        a, b = doc2.add_node("source"), doc2.add_node("sink")
        doc2.connect_auto(a, b)
        self.assertTrue(doc2.needs_layout())        # both at (0, 0)
        doc2.auto_layout()
        self.assertFalse(doc2.needs_layout())
        self.assertLess(doc2.node(a).x, doc2.node(b).x)
        session.close()

    def test_plant_tab_shows_controls_and_data_of_selected_node(self):
        import tkinter as tk
        try:
            tk.Tk().destroy()
        except tk.TclError:
            self.skipTest("needs a display")
        engine = create_engine("sqlite://", poolclass=StaticPool)
        with Session(engine) as s:
            doc, tags = make_loop(s)
            s.commit()
        win = launcher.Launcher(engine)
        try:
            win.plant_tab.open("Loop")
            win.plant_tab.show_interface(tags[1])    # the valve
            text = win.plant_tab.interface.get("1.0", "end")
            self.assertIn("CONTROLS", text)
            self.assertIn("DATA", text)
            self.assertIn(tags[1], text)
        finally:
            win.close()

    def test_launcher_hosts_every_tab_and_keeps_the_plant_in_sync(self):
        import tkinter as tk
        try:
            tk.Tk().destroy()
        except tk.TclError:
            self.skipTest("needs a display")
        engine = create_engine("sqlite://", poolclass=StaticPool)
        with Session(engine) as s:
            PlantDocument.create(s, "A")
            PlantDocument.create(s, "B")
            s.commit()
        win = launcher.Launcher(engine)
        try:
            tabs = [win.notebook.tab(t, "text") for t in win.notebook.tabs()]
            self.assertEqual(tabs, ["Launcher", "Plant", "Scenario", "Run", "AI health", "PLC"])
            self.assertEqual(win.plant.get(), "A")
            win.plant.set("B")
            win.choose_plant()
            self.assertEqual(win.plant_tab.doc.name, "B")
            self.assertEqual(win.scenario_tab.plant_name, "B")
            win.plant_tab.open("A")
            self.assertEqual(win.plant.get(), "A")
        finally:
            win.close()
    def test_runtime_command(self):
        cmd = launcher.plant_command("p", True, "1.2.3.4.1.1", 851, 2.0, 60, "snapA", "snapB", "out.csv")
        self.assertIn("--offline", cmd)
        self.assertNotIn("--ams-net-id", cmd)
        for flag, value in (("--plant", "p"), ("--speed", "2.0"), ("--duration", "60"),
                            ("--load-snapshot", "snapA"), ("--save-snapshot", "snapB"), ("--record", "out.csv")):
            self.assertEqual(cmd[cmd.index(flag) + 1], value)
        ads = launcher.plant_command("p", False, "1.2.3.4.1.1", 852, 1.0, None, None, None, None)
        self.assertEqual(ads[ads.index("--ams-net-id") + 1], "1.2.3.4.1.1")
        self.assertNotIn("--duration", ads)

    def test_process_manager_runs_and_stops_a_process(self):
        manager = launcher.ProcessManager()
        manager.start("t", [sys.executable, "-u", "-c", "print('hello')"])
        label, line = manager.output.get(timeout=10)
        self.assertEqual((label, line), ("t", "hello"))
        self.assertEqual(manager.output.get(timeout=10)[1], "[exited with code 0]")
        self.assertFalse(manager.running("t"))

    def test_clean_stop_lets_run_plant_save_a_snapshot(self):
        import subprocess
        import time
        from Data.build_test_plant import PLANT_NAME  # noqa: F401  (plant name only; a temp DB is used below)
        with tempfile.TemporaryDirectory() as folder:
            url = "sqlite:///" + os.path.join(folder, "t.db").replace("\\", "/")
            env = {**os.environ, "PLSIM_DB_URL": url}
            builder = [sys.executable, os.path.join(PROJECT_ROOT, "Data", "build_test_plant.py")]
            subprocess.run(builder, cwd=PROJECT_ROOT, env=env, check=True, capture_output=True)
            manager = launcher.ProcessManager()
            old = os.environ.get("PLSIM_DB_URL")
            os.environ["PLSIM_DB_URL"] = url
            try:
                manager.start("Runtime", launcher.plant_command("test_plant", True, "", 851, 50.0, None, None,
                                                                "stopped", None))
                time.sleep(6)
                self.assertIsNotNone(manager.stop("Runtime"))
                manager.processes["Runtime"].wait(timeout=15)
            finally:
                if old is None:
                    del os.environ["PLSIM_DB_URL"]
                else:
                    os.environ["PLSIM_DB_URL"] = old
            engine = create_engine(url)
            with Session(engine) as session:
                self.assertIn("stopped", [n for n, _, _ in list_snapshots(session, "test_plant")])
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
