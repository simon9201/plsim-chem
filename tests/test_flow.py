import os
import sys
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from Data.Model.model import Base, ChemComponent, Plant, add_equipment, add_instrument, connect
from Data.build_test_plant import PLANT_NAME, build_test_plant
from Data.chemistry import seed_chemistry
from Data.loader import load_plant
from Data.snapshots import list_snapshots, load_snapshot, save_snapshot
from Modules.Flow.chemistry import (COMPONENTS, DEFAULT_COMPONENTS, DEFAULT_REACTIONS, REACTIONS, Component, Reaction,
                                    set_chemistry, use_default_chemistry)
from Modules.Flow.flow import Material
from Modules.Entity.Process_equipment.Junction.junction import Tee
from Modules.Entity.Process_equipment.Pipe.Pipe import Elbow, Pipe
from Modules.Entity.Process_equipment.Pump.Pump import Pump
from Modules.Entity.Process_equipment.Valve.Valve import CheckValve, Valve
from Modules.Entity.Process_equipment.Vessel.StorageTank.storage_tank import StorageTank
from Modules.Entity.Process_equipment.Vessel.Reactor.CSTR.CSTR import ContinuousStirredTankReactor
from Modules.Entity.Process_equipment.Boundary.boundary import Sink, Source
from Modules.Plant.plant_model import PlantModel
from Modules.Plant.environment import Environment

DT = 0.1


def run(plant, seconds):
    for _ in range(round(seconds / DT)):
        plant.step(DT)


def tank(name, volume, composition=None, **kw):
    return StorageTank(name, inlets=["in1"], outlets=["out1"], max_capacity=1000.0,
                       initial_volume=volume, composition=composition or {"water": 1.0}, **kw)


def open_valve(name, **kw):
    return Valve(name, inlets=["in"], outlets=["out"], initial_position=1.0, **kw)


class FlowTests(unittest.TestCase):
    def tearDown(self):
        use_default_chemistry()

    def test_gravity_equalizes_and_conserves_volume(self):
        p = PlantModel()
        a, b = p.add_equipment(tank("A", 800)), p.add_equipment(tank("B", 100))
        p.add_equipment(open_valve("V"))
        p.connect("A", "out1", "V", "in")
        p.connect("V", "out", "B", "in1")
        total = a.volume + b.volume
        run(p, 600)
        self.assertAlmostEqual(a.volume + b.volume, total, places=6)
        self.assertGreater(b.volume, 100)
        self.assertLess(abs(a.pressure - b.headspace_pressure) - abs(b.hydrostatic_pressure), 0.5)

    def test_source_valve_sink_matches_formula(self):
        p = PlantModel()
        p.add_equipment(Source("S", composition={"water": 1.0}, supply_pressure=2.0))
        p.add_equipment(open_valve("V", rated_dp=0.5, max_flow=4.0))
        p.add_equipment(Sink("K"))
        p.connect("S", "out", "V", "in")
        p.connect("V", "out", "K", "in")
        p.step(DT)
        self.assertAlmostEqual(p.equipment["V"].flow, 4.0 * ((2.0 - 1.013) / 0.5) ** 0.5, places=3)

    def test_closed_valve_shows_full_differential_pressure(self):
        p = PlantModel()
        p.add_equipment(Source("S", composition={"water": 1.0}, supply_pressure=2.0))
        p.add_equipment(Valve("V", inlets=["in"], outlets=["out"]))
        p.add_equipment(Sink("K"))
        p.connect("S", "out", "V", "in")
        p.connect("V", "out", "K", "in")
        p.step(DT)
        self.assertEqual(p.equipment["V"].flow, 0.0)
        self.assertAlmostEqual(p.equipment["V"].differential_pressure, 2.0 - 1.013, places=6)

    def test_tee_merges_flows_and_mixes_composition(self):
        p = PlantModel()
        p.add_equipment(Source("SA", composition={"A": 1.0}, supply_pressure=2.0, temperature=20.0))
        p.add_equipment(Source("SB", composition={"B": 1.0}, supply_pressure=2.0, temperature=60.0))
        p.add_equipment(Tee("T", inlets=["in1", "in2"], outlets=["out1"]))
        p.add_equipment(open_valve("V1"))
        p.add_equipment(open_valve("V2"))
        p.add_equipment(Sink("K"))
        p.connect("SA", "out", "V1", "in")
        p.connect("V1", "out", "T", "in1")
        p.connect("SB", "out", "V2", "in")
        p.connect("V2", "out", "T", "in2")
        p.connect("T", "out1", "K", "in")
        run(p, 5)
        self.assertAlmostEqual(p.equipment["V1"].flow + p.equipment["V2"].flow, p.equipment["K"].flow, places=6)
        got = p.equipment["K"].received
        self.assertGreater(got.moles["A"], 0)
        self.assertGreater(got.moles["B"], 0)
        self.assertTrue(20.0 < got.temperature < 60.0)

    def _backflow_plant(self, element):
        # A high-pressure source pushes into the tee, which is connected back to tank A through `element`.
        p = PlantModel()
        p.add_equipment(tank("A", 100))
        p.add_equipment(Source("S", composition={"water": 1.0}, supply_pressure=3.0))
        p.add_equipment(open_valve("V1"))
        p.add_equipment(open_valve("V2"))
        p.add_equipment(Tee("T", inlets=["in1", "in2"], outlets=["out1"]))
        p.add_equipment(element)
        p.add_equipment(Sink("K"))
        p.connect("A", "out1", element.name, "in")
        p.connect(element.name, "out", "T", "in1")
        p.connect("S", "out", "V1", "in")
        p.connect("V1", "out", "T", "in2")
        p.connect("T", "out1", "V2", "in")
        p.connect("V2", "out", "K", "in")
        return p

    def test_check_valve_blocks_reverse_flow(self):
        p = self._backflow_plant(CheckValve("E", inlets=["in"], outlets=["out"]))
        run(p, 20)
        self.assertAlmostEqual(p.equipment["A"].volume, 100, places=3)

    def test_plain_valve_lets_liquid_flow_backwards(self):
        p = self._backflow_plant(open_valve("E"))
        run(p, 20)
        self.assertGreater(p.equipment["A"].volume, 105)
    def test_elbow_loses_more_than_straight_pipe(self):
        self.assertGreater(Elbow("E", length=5.0).loss_coefficient(), Pipe("P", length=5.0).loss_coefficient())

    def test_reaction_enthalpy_from_formation(self):
        self.assertAlmostEqual(REACTIONS["A_B_to_C"].enthalpy(), -140e3)

    def test_unconnected_transfer_is_rejected(self):
        p = PlantModel()
        p.add_equipment(Valve("V", inlets=["in"], outlets=["out"]))
        with self.assertRaises(ValueError):
            p.step(DT)

    def test_cstr_conserves_atoms_and_releases_heat(self):
        p = PlantModel()
        r = p.add_equipment(ContinuousStirredTankReactor(
            "R", inlets=["in1"], outlets=["out1"], max_capacity=1000.0, initial_volume=100.0,
            composition={"A": 0.1, "B": 0.1, "water": 0.8}, temperature=25.0, reactions=["A_B_to_C"]))
        before = dict(r.contents.moles)
        run(p, 100)
        m = r.contents.moles
        self.assertAlmostEqual(m["A"], m["B"], places=6)
        self.assertAlmostEqual(m["A"] + m["C"], before["A"], places=6)
        self.assertAlmostEqual(m["water"], before["water"], places=6)
        self.assertGreater(m["C"], 0)
        self.assertGreater(r.temperature, 25.0)

    def test_custom_reaction_and_component_attributes(self):
        d = Component("D", 120.0, 1100.0, 150.0, -400e3, {"hazard": "corrosive", "boiling_point_c": 150.0})
        rx = Reaction("2A_to_D", {"A": -2, "D": 1}, k_ref=0.05, activation_energy=40e3)
        set_chemistry(list(DEFAULT_COMPONENTS) + [d], list(DEFAULT_REACTIONS) + [rx])
        self.assertAlmostEqual(rx.enthalpy(), -400e3 + 200e3)
        self.assertEqual(rx.orders, {"A": 2})
        p = PlantModel()
        r = p.add_equipment(ContinuousStirredTankReactor(
            "R", inlets=["in1"], outlets=["out1"], initial_volume=100.0,
            composition={"A": 0.2, "water": 0.8}, reactions=["2A_to_D"]))
        a0 = r.contents.moles["A"]
        run(p, 50)
        self.assertAlmostEqual(r.contents.moles["A"] + 2 * r.contents.moles["D"], a0, places=6)
        self.assertTrue(100.0 < r.contents.attribute_average("boiling_point_c") < 150.0)

    def _pump_plant(self, temperature=25.0):
        p = PlantModel()
        p.add_equipment(tank("A", 500, temperature=temperature))
        p.add_equipment(open_valve("SV", rated_dp=0.02))
        pump = p.add_equipment(Pump("P", inlets=["in"], outlets=["out"]))
        p.add_equipment(Sink("K"))
        p.connect("A", "out1", "SV", "in")
        p.connect("SV", "out", "P", "in")
        p.connect("P", "out", "K", "in")
        pump.run_cmd = True
        return p, pump

    def test_pump_does_not_cavitate_with_good_suction(self):
        p, pump = self._pump_plant()
        run(p, 20)
        self.assertEqual(pump.cavitation, 0.0)
        self.assertFalse(pump.tripped)

    def test_throttled_suction_cavitates_and_trips_pump(self):
        p, pump = self._pump_plant()
        run(p, 5)
        healthy = pump.flow
        p.equipment["SV"].command = p.equipment["SV"].position = 0.05
        run(p, 3)
        self.assertGreater(pump.cavitation, 0.0)
        self.assertLess(pump.flow, healthy)
        run(p, 60)
        self.assertTrue(pump.tripped)

    def test_hot_liquid_cavitates(self):
        p, pump = self._pump_plant(temperature=99.0)
        run(p, 2)
        self.assertGreater(pump.cavitation, 0.0)

    def test_leak_on_any_equipment(self):
        p = PlantModel()
        p.add_equipment(tank("A", 500))
        pipe = p.add_equipment(Pipe("L", inlets=["in"], outlets=["out"]))
        b = p.add_equipment(tank("B", 0))
        p.connect("A", "out1", "L", "in")
        p.connect("L", "out", "B", "in1")
        p.set_fault("L", "leak_rate", 0.5)
        total = p.equipment["A"].volume
        run(p, 30)
        self.assertLess(p.equipment["A"].volume + b.volume, total - 1.0)
        p.set_fault("A", "leak_rate", 1.0)
        before = p.equipment["A"].volume
        run(p, 5)
        self.assertLess(p.equipment["A"].volume, before - 4.0)
        with self.assertRaises(ValueError):
            p.set_fault("A", "blockage", 0.5)
        self.assertEqual(pipe.leak_rate, 0.5)

    def test_faults_reject_out_of_range_and_non_finite_values(self):
        p = PlantModel()
        p.add_equipment(tank("A", 500))
        p.add_equipment(Pipe("L", inlets=["in"], outlets=["out"]))
        for name, value in (("leak_rate", -1), ("blockage", 1.1), ("blockage", float("nan"))):
            with self.assertRaises(ValueError):
                p.set_fault("L", name, value)
        with self.assertRaises(ValueError):
            p.step(0)
        with self.assertRaises(ValueError):
            p.step(float("inf"))
        p.slow_interval = 0
        with self.assertRaises(ValueError):
            p.step(DT)
        p.slow_interval = 10.0
        p.slow_time_scale = -1
        with self.assertRaises(ValueError):
            p.step(DT)

    def test_status_disables_transfer_and_node_flow(self):
        p = PlantModel()
        p.add_equipment(Source("S", composition={"water": 1.0}, supply_pressure=2.0))
        valve = p.add_equipment(open_valve("V"))
        p.add_equipment(Sink("K"))
        p.connect("S", "out", "V", "in")
        p.connect("V", "out", "K", "in")
        valve.status = 0
        p.step(DT)
        self.assertEqual(valve.flow, 0.0)
        valve.status = 1
        p.step(DT)
        self.assertGreater(valve.flow, 0.0)

    def test_disabled_pump_stops_and_reports_not_running(self):
        p, pump = self._pump_plant()
        pump.status = 0
        run(p, 3)
        self.assertFalse(pump.running)
        self.assertEqual(pump.flow, 0.0)
        pump.status = 1
        run(p, 3)
        self.assertTrue(pump.running)

    def test_environment_cycles(self):
        env = Environment(mean_c=10, daily_amp=5, annual_amp=10, start_day=180, start_hour=0)
        day = [env.temperature(h * 3600) for h in range(24)]
        self.assertAlmostEqual(max(day) - min(day), 10.0, delta=0.2)
        self.assertEqual(day.index(max(day)), 16)
        summer, winter = env.temperature(0), env.temperature(182 * 86400)
        self.assertGreater(summer - winter, 15)
        env.offset = -15
        self.assertAlmostEqual(env.temperature(0), summer - 15)

    def test_tank_follows_ambient_slowly(self):
        p = PlantModel()
        t = p.add_equipment(tank("A", 500, ambient_ua=50.0))
        p.environment = Environment(mean_c=-10)
        p.slow_time_scale = 100.0
        run(p, 600)
        self.assertLess(t.temperature, 20.0)
        self.assertGreater(t.temperature, -10.0)

    def _line(self, composition=None):
        p = PlantModel()
        p.add_equipment(tank("A", 500, composition))
        pipe = p.add_equipment(Pipe("L", inlets=["in"], outlets=["out"]))
        p.add_equipment(tank("B", 0))
        p.connect("A", "out1", "L", "in")
        p.connect("L", "out", "B", "in1")
        return p, pipe

    def test_stagnant_pipe_freezes_in_cold_weather_and_thaws(self):
        p, pipe = self._line()
        p.step(DT)
        p.environment = Environment(mean_c=-20)
        p.slow_time_scale = 50.0
        pipe.blockage = 0.0
        p.equipment["A"].outlets = []       # nothing flows, pipe stagnates
        pipe.flow = 0.0
        pipe.flowing = False
        run(p, 4000)
        self.assertGreater(pipe.ice, 0.5)
        p.environment.mean_c = 20
        run(p, 4000)
        self.assertEqual(pipe.ice, 0.0)

    def test_oversaturated_cold_pipe_fouls_but_flowing_warm_pipe_does_not(self):
        set_chemistry(
            [Component("water", 18.015, 997.0, 75.3, -285830.0, {"freezing_point_c": 0.0}),
             Component("A", 60.0, 1000.0, 120.0, -100e3, {"solubility_25c": 1.0, "solubility_slope": 0.05}),
             Component("B", 46.0, 800.0, 110.0, -50e3), Component("C", 106.0, 900.0, 190.0, -290e3)],
            DEFAULT_REACTIONS)
        p, pipe = self._line({"water": 0.99, "A": 0.01})
        p.slow_time_scale = 20.0
        run(p, 600)
        self.assertEqual(pipe.fouling, 0.0)
        pipe.flowing = False
        p.equipment["A"].outlets = []
        p.environment = Environment(mean_c=-10)
        run(p, 3000)
        self.assertGreater(pipe.fouling, 0.0)
        p.set_fault("L", "fouling", 0)
        self.assertEqual(pipe.fouling, 0.0)

    def test_cavitation_wears_pump(self):
        p, pump = self._pump_plant(temperature=99.0)
        p.slow_time_scale = 1000.0
        head_new = pump.shutoff_head
        run(p, 600)
        self.assertGreater(pump.wear, 0.0)
        p.set_fault("P", "wear", 0.5)
        pump.tripped, pump.speed, pump.cavitation = False, 100.0, 0.0
        self.assertLess(pump.head_at_zero_flow(), head_new)

    def test_slow_clock_preserves_partial_interval_and_environment_in_snapshot(self):
        p = PlantModel()
        p.slow_interval = 10.0
        p.slow_time_scale = 3.0
        p.environment = Environment(mean_c=4.0, daily_amp=2.0, annual_amp=8.0, start_day=20)
        p.environment.offset = -5.0
        for _ in range(35):
            p.step(0.1)
        state = p.get_state()
        restored = PlantModel()
        restored.set_state(state)
        self.assertAlmostEqual(restored._slow_acc, 3.5)
        self.assertAlmostEqual(restored.slow_time, 0.0)
        self.assertEqual(restored.environment.get_state(), p.environment.get_state())
        p.step(6.5)
        restored.step(6.5)
        self.assertAlmostEqual(restored.slow_time, p.slow_time)
        self.assertAlmostEqual(restored.ambient_temperature, p.ambient_temperature)

    def test_old_chemistry_gets_water_freezing_point_without_losing_attributes(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            session.add(ChemComponent(name="water", molar_mass=18.015, density=997.0, cp=75.3,
                                      enthalpy_formation=-285830.0,
                                      attributes={"boiling_point_c": 100.0, "custom": "keep"}))
            session.commit()
            seed_chemistry(session)
            water = session.query(ChemComponent).filter_by(name="water").one()
            self.assertEqual(water.attributes["freezing_point_c"], 0.0)
            self.assertEqual(water.attributes["custom"], "keep")

    def test_cooling_remains_bounded_for_large_steps(self):
        reactor = ContinuousStirredTankReactor(
            "R", inlets=["in"], outlets=["out"], initial_volume=100.0,
            composition={"water": 1.0}, temperature=80.0,
            cooling_ua=1e6, coolant_temp=15.0, cooling_command=1.0)
        reactor.update(100.0)
        self.assertGreaterEqual(reactor.temperature, 15.0)
        self.assertLess(reactor.temperature, 15.01)
        self.assertGreater(reactor.cooling_duty, 0.0)

    def test_snapshot_roundtrip_is_deterministic(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as s:
            build_test_plant(s)
            a, b = load_plant(s, PLANT_NAME), load_plant(s, PLANT_NAME)
            a.environment = Environment(mean_c=5.0, daily_amp=3.0, annual_amp=12.0)
            a.environment.offset = -4.0
            a.slow_time_scale = 5.0
            b.environment = Environment(mean_c=99.0)
            b.slow_time_scale = 1.0
            for plant in (a, b):
                for t in ("XV111", "XV112", "XV113", "XV201"):
                    plant.equipment[t].command = 0.8
                for t in ("P101", "P102", "P103"):
                    plant.equipment[t].run_cmd = True
            run(a, 60.5)
            save_snapshot(s, PLANT_NAME, a, "mid")
            self.assertEqual(list_snapshots(s, PLANT_NAME)[0][0], "mid")
            with self.assertRaises(ValueError):
                save_snapshot(s, PLANT_NAME, a, "mid")
            run(a, 60)
            load_snapshot(s, PLANT_NAME, b, "mid")
            self.assertAlmostEqual(b.time, 60.5, places=6)
            self.assertEqual(b.environment.get_state(), a.environment.get_state())
            self.assertEqual(b.slow_time_scale, a.slow_time_scale)
            run(b, 60)
            for tag in ("R201", "TK101", "TK103"):
                self.assertAlmostEqual(a.equipment[tag].volume, b.equipment[tag].volume, places=6)
                self.assertAlmostEqual(a.equipment[tag].temperature, b.equipment[tag].temperature, places=6)
            self.assertAlmostEqual(a.equipment["R201"].concentration("C"), b.equipment["R201"].concentration("C"), places=6)

    def test_analyzer_reads_concentration(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as s:
            build_test_plant(s)
            plant = load_plant(s, PLANT_NAME)
        r = plant.equipment["R201"]
        r.contents = Material.from_fractions({"C": 0.1, "water": 0.9}, 300.0)
        plant.sample_instruments()
        self.assertAlmostEqual(plant.instruments["AT201"].value, r.concentration("C"), delta=0.1)


if __name__ == "__main__":
    unittest.main()
