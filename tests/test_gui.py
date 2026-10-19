import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tkinter as tk

from sqlalchemy import create_engine

try:
    from Modules.GUI.app import App
except Exception:       # tkinter missing
    App = None


def _display():
    try:
        root = tk.Tk()
        root.destroy()
        return True
    except tk.TclError:
        return False


@unittest.skipUnless(App is not None and _display(), "needs tkinter and a display")
class GuiStartupWithExistingPlantTest(unittest.TestCase):
    def test_window_opens_when_the_database_already_has_a_plant(self):
        from sqlalchemy.pool import StaticPool
        from sqlalchemy.orm import Session
        from Modules.Editor.document import PlantDocument
        engine = create_engine("sqlite://", poolclass=StaticPool)
        with Session(engine) as s:
            PlantDocument.create(s, "Existing")
            s.commit()
        app = App(engine)
        try:
            self.assertEqual(app.plant_tab.doc.name, "Existing")
            self.assertEqual(app.scenario_tab.plant_name, "Existing")
        finally:
            app.close()


@unittest.skipUnless(App is not None and _display(), "needs tkinter and a display")
class GuiSmokeTest(unittest.TestCase):
    def setUp(self):
        self.app = App(create_engine("sqlite://"))

    def tearDown(self):
        self.app.close()

    def test_build_a_plant_by_clicking_then_run_a_leak_scenario(self):
        from Modules.Editor.document import PlantDocument
        plant = self.app.plant_tab
        doc = PlantDocument.create(self.app.session, "G")
        self.app.session.commit()
        plant.refresh_list()
        plant.open("G")
        self.app.update()

        plant.mode.set("add")
        for kind, x in (("source", 100), ("pipe", 300), ("sink", 500)):
            plant.kind.set(kind)
            plant.background(x, 100)
        self.assertEqual(len(plant.doc.nodes()), 3)
        tags = [n.tag for n in plant.doc.nodes()]

        plant.mode.set("connect")
        for a, b in ((tags[0], tags[1]), (tags[1], tags[2])):
            plant.clicked(a, 1)
            plant.clicked(b, 1)
        self.assertEqual(len(plant.doc.edges()), 2)
        plant.kind.set("flow_sensor")
        plant.mode.set("add")
        plant.clicked(tags[1], 1)
        self.assertEqual(len(plant.doc.probes()), 1)
        self.assertEqual(plant.doc.validate(), [])
        plant.save()

        scen = self.app.scenario_tab
        scen.set_plant("G")
        scen.clicked(tags[1], 1)
        scen.preset("leak")
        self.assertEqual(len(scen.scenario.events), 1)
        scen.name.delete(0, "end")
        scen.name.insert(0, "leaky")
        scen.save()
        self.assertEqual(scen.saved["values"][0], "leaky")

        run = self.app.run_tab
        run.dt.set("1")
        run.steps.set("100")
        run.start()
        self.assertIs(self.app.health_tab.plant, run.plant)
        self.assertIsNotNone(self.app.health_tab.monitor)
        for _ in range(3):
            run.tick()
        self.assertGreater(run.plant.time, 100)
        self.assertTrue(run.player.log or run.plant.equipment[tags[1]].leak_rate > 0)
        self.assertEqual(self.app.health_tab.monitor.health[tags[1]].repairs, 0)
        self.app.health_tab.canvas.select(tags[1])
        self.app.health_tab.repair()
        self.assertEqual(self.app.health_tab.monitor.health[tags[1]].repairs, 1)
        self.assertEqual(self.app.health_tab.monitor.health[tags[1]].status, "learning")
        run.canvas.select(tags[1])
        run.show_signals(tags[1])
        self.assertIn(tags[1], run.readout.get("1.0", "end"))

    def test_live_colours_and_plc_tab(self):
        from Modules.Editor.document import PlantDocument
        plant = self.app.plant_tab
        PlantDocument.create(self.app.session, "C")
        self.app.session.commit()
        plant.refresh_list()
        plant.open("C")
        plant.mode.set("add")
        for kind, x in (("source", 100), ("pipe", 300), ("storage_tank", 500)):
            plant.kind.set(kind)
            plant.background(x, 100)
        tags = [n.tag for n in plant.doc.nodes()]
        plant.mode.set("connect")
        for a, b in ((tags[0], tags[1]), (tags[1], tags[2])):
            plant.clicked(a, 1)
            plant.clicked(b, 1)
        plant.kind.set("flow_sensor")
        plant.mode.set("add")
        plant.clicked(tags[1], 1)
        plant.save()
        run = self.app.run_tab
        run.link_mode.set("Loopback")
        run.dt.set("0.5")
        run.steps.set("10")
        run.start()
        self.assertIsNotNone(run.cycle)
        for _ in range(8):
            run.tick()
        self.assertIn(tags[2], run.canvas.levels)
        self.assertTrue(run.canvas.edge_style)
        self.assertTrue(self.app.plc_tab.table.get_children())
        self.assertIn("Loopback", self.app.plc_tab.status.get())
        run.canvas.is_3d.set(True)
        run.canvas._switch()
        run.update_view()
        run.canvas.redraw()

    def test_3d_view_and_elevation(self):
        from Modules.Editor.document import PlantDocument
        plant = self.app.plant_tab
        PlantDocument.create(self.app.session, "H")
        self.app.session.commit()
        plant.refresh_list()
        plant.open("H")
        plant.mode.set("add")
        for kind, x in (("storage_tank", 100), ("valve", 300), ("pump", 500), ("sink", 700)):
            plant.kind.set(kind)
            plant.background(x, 100)
        tag = plant.doc.nodes()[0].tag
        plant.mode.set("select")
        plant.clicked(tag, 1)
        plant.elevation.set("6")
        plant.set_elevation()
        self.assertEqual(plant.doc.node(tag).z, 6.0)
        plant.canvas.is_3d.set(True)
        plant.canvas._switch()
        self.app.update()
        self.assertEqual(plant.canvas._c3.world(tag)[2], 6.0)
        plant.canvas.redraw()
        plant.canvas._c3.fit()


if __name__ == "__main__":
    unittest.main()
