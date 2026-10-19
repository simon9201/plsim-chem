import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from Data.Model.model import Base
from Data.chemistry import seed_chemistry
from Data.loader import load_plant
from Modules.Editor.document import METERS_PER_UNIT, PlantDocument
from Modules.GUI.projection import Camera
from tests import test_flow


class ElevationTests(unittest.TestCase):
    def _flow(self, sink_z=0.0, pump_z=0.0):
        p, pump = test_flow.FlowTests._pump_plant(self)
        p.equipment["K"].location = (0.0, 0.0, sink_z)
        pump.location = (0.0, 0.0, pump_z)
        test_flow.run(p, 20)
        return p, pump

    def test_pumping_uphill_is_harder_than_downhill(self):
        down = self._flow(sink_z=-5.0)[1].flow
        level = self._flow(sink_z=0.0)[1].flow
        up = self._flow(sink_z=8.0)[1].flow
        self.assertGreater(down, level)
        self.assertGreater(level, up)

    def test_edge_static_head_is_rho_g_dz(self):
        p, _ = self._flow(sink_z=5.0)
        total = sum(e.static for e in p._edges)
        self.assertAlmostEqual(total, 1000 * 9.80665 * 5.0 / 1e5, delta=0.03)

    def test_element_statics_add_up_to_the_edge_static(self):
        p, _ = self._flow(sink_z=3.0, pump_z=1.0)
        for e in p._edges:
            self.assertAlmostEqual(sum(e.statics.values()), e.static, places=9)

    def test_pump_above_the_source_has_more_cavitation(self):
        low = self._flow(pump_z=0.0)[1]
        high = self._flow(pump_z=9.5)[1]
        self.assertGreater(high.cavitation, low.cavitation)


class CameraTests(unittest.TestCase):
    def test_ground_round_trip(self):
        cam = Camera(yaw_deg=40, pitch_deg=50, scale=9, origin=(300, 200), centre=(5, 6, 1))
        for z in (0.0, 2.5):
            sx, sy, _ = cam.project(7.0, 3.0, z)
            x, y = cam.to_ground(sx, sy, z)
            self.assertAlmostEqual(x, 7.0, places=6)
            self.assertAlmostEqual(y, 3.0, places=6)

    def test_higher_points_are_drawn_higher(self):
        cam = Camera()
        self.assertLess(cam.project(0, 0, 5)[1], cam.project(0, 0, 0)[1])

    def test_pitch_is_clamped(self):
        cam = Camera()
        cam.rotate(0, 10)
        self.assertLessEqual(cam.pitch, math.radians(90))
        cam.rotate(0, -10)
        self.assertGreater(cam.pitch, 0)


class EditorElevationTests(unittest.TestCase):
    def test_z_round_trips_and_reaches_the_equipment(self):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        with Session(engine) as s:
            seed_chemistry(s)
            doc = PlantDocument.create(s, "E")
            doc.add_node("source", 100, 200, z=4.0)
            doc.move_node(doc.nodes()[0].tag, 120, 200)    # z stays when not given
            doc.save()
            tag = doc.nodes()[0].tag
            self.assertEqual(doc.node(tag).z, 4.0)
            plant = load_plant(s, "E")
            eq = plant.equipment[tag]
            self.assertAlmostEqual(eq.location[0], 120 * METERS_PER_UNIT)
            self.assertAlmostEqual(eq.location[2], 4.0)


if __name__ == "__main__":
    unittest.main()
