import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from Data.Model.model import Base
from Data.chemistry import seed_chemistry
from Data.loader import load_plant
from Modules.Editor.document import PlantDocument
from Modules.Entity.Process_equipment.Boundary.boundary import Sink, Source
from Modules.Entity.Process_equipment.Drying.drying import Dryer, Evaporator
from Modules.Entity.Process_equipment.HeatExchanger.heat_exchanger import HeatExchanger
from Modules.Entity.Process_equipment.Vessel.Column.column import DistillationColumn
from Modules.Flow import thermo
from Modules.Plant.plant_model import PlantModel
from tests.test_flow import open_valve

# Aspen-style NRTL binary parameters for ethanol(1)-water(2)
ETHANOL_WATER = {("ethanol", "water"): (-0.8009, 246.18, 3.4578, -586.08, 0.3)}


class NrtlTests(unittest.TestCase):
    def tearDown(self):
        thermo.set_activity_model()

    def test_ethanol_is_non_ideal_in_water(self):
        thermo.set_activity_model(thermo.nrtl_model(ETHANOL_WATER))
        gamma = thermo.activity_coefficients({"ethanol": 0.1, "water": 0.9}, 78.0)
        self.assertGreater(gamma["ethanol"], 1.5)
        self.assertAlmostEqual(gamma["water"], 1.0, delta=0.1)
        # pure component: gamma -> 1
        pure = thermo.activity_coefficients({"ethanol": 1.0, "water": 0.0}, 78.0)
        self.assertAlmostEqual(pure["ethanol"], 1.0, places=6)

    def test_nrtl_raises_the_bubble_pressure_above_raoult(self):
        x = {"ethanol": 0.3, "water": 0.7}
        ideal = thermo.bubble_pressure(x, 78.0)
        thermo.set_activity_model(thermo.nrtl_model(ETHANOL_WATER))
        self.assertGreater(thermo.bubble_pressure(x, 78.0), ideal)

    def test_unlisted_pairs_stay_ideal(self):
        thermo.set_activity_model(thermo.nrtl_model({}))
        gamma = thermo.activity_coefficients({"ethanol": 0.5, "water": 0.5}, 60.0)
        self.assertAlmostEqual(gamma["ethanol"], 1.0)


def column_plant(**options):
    p = PlantModel()
    p.add_equipment(Source("F", composition={"water": 0.8, "ethanol": 0.2}, temperature=60, supply_pressure=2.0))
    p.add_equipment(open_valve("FV", rated_dp=0.02))
    col = p.add_equipment(DistillationColumn(
        "C", inlets=["feed"], outlets=["bottoms", "distillate"], max_capacity=600, height=8, initial_volume=300,
        composition={"water": 0.8, "ethanol": 0.2}, temperature=60, heater_power=60e3, **options))
    for tag in ("B", "D"):
        p.add_equipment(open_valve(tag + "V", rated_dp=0.02))
        p.add_equipment(Sink(tag))
    p.connect("F", "out", "FV", "in")
    p.connect("FV", "out", "C", "feed")
    p.connect("C", "bottoms", "BV", "in")
    p.connect("BV", "out", "B", "in")
    p.connect("C", "distillate", "DV", "in")
    p.connect("DV", "out", "D", "in")
    p.equipment["BV"].command = p.equipment["BV"].position = 0.3
    p.equipment["DV"].command = p.equipment["DV"].position = 0.2
    col.heater_command = 100.0
    return p, col


def run(p, seconds, dt=1.0):
    for _ in range(round(seconds / dt)):
        p.step(dt)


class ColumnTests(unittest.TestCase):
    def setUp(self):
        thermo.set_activity_model(thermo.nrtl_model(ETHANOL_WATER))

    def tearDown(self):
        thermo.set_activity_model()

    def test_column_enriches_the_light_component_at_the_top(self):
        p, col = column_plant()
        run(p, 600)
        self.assertGreater(col.boilup, 0.5)
        self.assertGreater(col.top_composition["ethanol"], 0.5)
        self.assertLess(col.contents.mole_fractions().get("ethanol", 0.0), 0.2)
        self.assertGreater(col.drum_composition.get("ethanol", 0.0) + p.equipment["D"].received.mole_fractions().get("ethanol", 0.0), 0.4)
        self.assertLess(col.top_temperature, col.contents.temperature)      # light key boils colder

    def test_more_reflux_gives_a_purer_top(self):
        results = []
        for reflux in (10.0, 90.0):
            p, col = column_plant()
            col.reflux_command = reflux
            run(p, 500)
            results.append(col.top_composition["ethanol"])
        self.assertGreater(results[1], results[0])

    def test_no_heat_no_separation(self):
        p, col = column_plant()
        col.heater_command = 0.0
        run(p, 100)
        self.assertEqual(col.boilup, 0.0)
        self.assertAlmostEqual(col.top_composition["ethanol"], 0.2, delta=0.02)

    def test_flooding_and_fouling_hurt_the_separation_and_repair_restores_it(self):
        p, col = column_plant(max_vapor=1.0)
        run(p, 30)
        self.assertTrue(col.flooding)
        self.assertGreater(col.headspace_pressure - col.column_pressure, col.n_trays * col.tray_dp)
        col.heater_command = 40.0
        run(p, 5)
        self.assertFalse(col.flooding)
        clean = col.efficiency
        p.set_fault("C", "fouling", 0.8)
        self.assertLess(col.stage_efficiency, clean)
        col.repair()
        self.assertEqual(col.fouling, 0.0)
        self.assertAlmostEqual(col.stage_efficiency, clean)

    def test_state_round_trip(self):
        p, col = column_plant()
        run(p, 120)
        state = col.get_state()
        other = column_plant()[1]
        other.set_state(state)
        self.assertEqual(other.get_state(), state)

    def test_material_is_conserved_without_feed(self):
        p, col = column_plant()
        p.equipment["FV"].command = p.equipment["FV"].position = 0.0
        start = _moles(col)
        run(p, 300)
        out = sum(sum(p.equipment[t].received.moles.values()) for t in ("B", "D"))
        self.assertGreater(out, 0.0)
        self.assertAlmostEqual(_moles(col) + out, start, delta=1e-6 * start)


def _moles(col):
    return (sum(col.contents.moles.values()) + sum(sum(t.moles.values()) for t in col.trays)
            + sum(col.drum.moles.values()))


class HeatExchangerTests(unittest.TestCase):
    def _plant(self, opening=1.0, **options):
        p = PlantModel()
        p.add_equipment(Source("S", composition={"water": 1.0}, temperature=20.0, supply_pressure=2.0))
        hx = p.add_equipment(HeatExchanger("HX", **options))
        p.add_equipment(open_valve("V", rated_dp=0.02))
        p.add_equipment(Sink("K"))
        p.connect("S", "out", "HX", "in")
        p.connect("HX", "out", "V", "in")
        p.connect("V", "out", "K", "in")
        p.equipment["V"].command = p.equipment["V"].position = opening
        return p, hx

    def test_heats_the_stream_between_inlet_and_utility_temperature(self):
        p, hx = self._plant()
        run(p, 10)
        self.assertGreater(hx.outlet_temp, 21.0)
        self.assertLess(hx.outlet_temp, hx.utility_temp)
        self.assertGreater(hx.duty, 0.0)

    def test_more_flow_means_less_temperature_rise(self):
        p1, hx1 = self._plant(opening=0.3)
        p2, hx2 = self._plant(opening=1.0)
        run(p1, 10)
        run(p2, 10)
        self.assertGreater(hx1.outlet_temp, hx2.outlet_temp)

    def test_duty_command_zero_stops_heating(self):
        p, hx = self._plant()
        hx.duty_command = 0.0
        run(p, 10)
        self.assertAlmostEqual(hx.outlet_temp, 20.0, places=3)

    def test_scaling_cuts_duty_and_repair_cleans_it(self):
        p, hx = self._plant()
        run(p, 5)
        clean = hx.outlet_temp
        p.set_fault("HX", "scaling", 0.8)
        run(p, 5)
        self.assertLess(hx.outlet_temp, clean)
        hx.repair()
        self.assertEqual(hx.scaling, 0.0)

    def test_cooling_works_the_other_way(self):
        p, hx = self._plant(utility_temp=5.0)
        run(p, 10)
        self.assertLess(hx.outlet_temp, 20.0)
        self.assertLess(hx.duty, 0.0)


class DryingTests(unittest.TestCase):
    def _dryer(self, **options):
        return Dryer("DR", inlets=["in1"], outlets=["out1", "vapor_out"], max_capacity=500.0, height=2.0,
                     initial_volume=100.0, composition={"water": 0.9, "C": 0.1}, temperature=25.0, **options)

    def test_hot_air_dries_the_product(self):
        d = self._dryer()
        start = d.moisture_percent
        d.air_command = 100.0
        for _ in range(900):
            d.run_step(1.0)
        self.assertLess(d.moisture_percent, start)
        self.assertGreater(sum(d.vented.moles.values()), 0.0)

    def test_no_air_no_drying(self):
        d = self._dryer()
        for _ in range(100):
            d.run_step(1.0)
        self.assertAlmostEqual(d.temperature, 25.0, delta=0.5)

    def test_overheating_damages_the_product_and_clean_resets(self):
        d = self._dryer(max_product_temp=40.0)
        d.air_command = 100.0
        for _ in range(900):
            d.run_step(1.0)
        self.assertGreater(d.thermal_damage, 0.0)
        d.clean()
        self.assertEqual(d.thermal_damage, 0.0)

    def test_evaporator_boils_solvent_and_scaling_reduces_it(self):
        rates = []
        for scaling in (0.0, 0.8):
            e = Evaporator("EV", inlets=["in1"], outlets=["out1", "vapor_out"], max_capacity=1000.0, height=3.0,
                           initial_volume=400.0, composition={"water": 1.0}, temperature=95.0, heater_power=200e3)
            e.scaling = scaling
            e.heater_command = 100.0
            for _ in range(120):
                e.run_step(1.0)
            rates.append(e.evaporation_rate)
        self.assertGreater(rates[0], 0.0)
        self.assertLess(rates[1], rates[0])


class CatalogTests(unittest.TestCase):
    def test_new_units_build_from_the_editor_and_step(self):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        with Session(engine) as s:
            seed_chemistry(s)
            doc = PlantDocument.create(s, "U")
            src = doc.add_node("source", 0, 0, params={"composition": {"water": 0.8, "ethanol": 0.2}})
            hx = doc.add_node("heat_exchanger", 100, 0)
            col = doc.add_node("distillation_column", 200, 0, z=1.0)
            bot = doc.add_node("sink", 300, 0)
            top = doc.add_node("sink", 300, 100)
            doc.connect_auto(src, hx)
            doc.connect_auto(hx, col)
            doc.connect_auto(col, bot)
            doc.connect_auto(col, top)
            self.assertEqual(doc.validate(), [])
            doc.save()
            plant = load_plant(s, "U")
            self.assertIsInstance(plant.equipment[col], DistillationColumn)
            self.assertIsInstance(plant.equipment[hx], HeatExchanger)
            self.assertEqual(plant.equipment[col].location[2], 1.0)


if __name__ == "__main__":
    unittest.main()
