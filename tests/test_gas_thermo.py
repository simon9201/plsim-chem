import os
import sys
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from Data.Model.model import Base, Plant, add_equipment, add_instrument, connect
from Data.chemistry import seed_chemistry
from Data.loader import load_plant
from Modules.Entity.Process_equipment.Boundary.boundary import Source, Sink
from Modules.Entity.Process_equipment.Gas.gas import (Compressor, GasCheckValve, GasPipe, GasSink, GasSource,
                                                      GasValve, GasVolume)
from Modules.Entity.Process_equipment.Pipe.Pipe import Pipe
from Modules.Entity.Process_equipment.Pump.Pump import Pump
from Modules.Entity.Process_equipment.Valve.Valve import Valve
from Modules.Entity.Process_equipment.Vessel.FlashDrum.flash_drum import FlashDrum
from Modules.Entity.Process_equipment.Vessel.StorageTank.storage_tank import StorageTank
from Modules.Flow import thermo
from Modules.Flow.chemistry import (COMPONENTS, DEFAULT_REACTIONS, Component, set_chemistry, use_default_chemistry)
from Modules.Flow.flow import Gas, Material, gas_pressure_factor
from Modules.Plant.environment import Environment
from Modules.Plant.plant_model import PlantModel

DT = 0.1


def run(plant, seconds, dt=DT):
    for _ in range(round(seconds / dt)):
        plant.step(dt)


def binary_chemistry():
    """light (boils 60 C) and heavy (boils 120 C), Clausius-Clapeyron vapor pressures, plus N2 and a salt."""
    set_chemistry(
        [Component("light", 50.0, 700.0, 100.0, 0.0, {"boiling_point_c": 60.0}),
         Component("heavy", 100.0, 900.0, 150.0, 0.0, {"boiling_point_c": 120.0}),
         Component("water", 18.015, 997.0, 75.3, -285830.0, {"freezing_point_c": 0.0, "boiling_point_c": 100.0,
                                                                  "antoine_a": 4.6543, "antoine_b": 1435.264,
                                                                  "antoine_c": -64.848, "heat_of_vaporization": 40660.0}),
         Component("N2", 28.0, 808.0, 57.0, 0.0, {"noncondensable": True}),
         Component("salt", 58.0, 2000.0, 50.0, 0.0, {"solubility_25c": 1.0, "solubility_slope": 0.05})],
        [])


class ThermoTests(unittest.TestCase):
    def setUp(self):
        binary_chemistry()

    def tearDown(self):
        use_default_chemistry()

    def test_water_antoine_matches_normal_boiling_point(self):
        self.assertAlmostEqual(thermo.psat("water", 100.0), 1.013, delta=0.02)
        self.assertAlmostEqual(thermo.psat("water", 25.0), 0.0317, delta=0.003)
        self.assertAlmostEqual(thermo.bubble_temperature({"water": 1.0}, 1.01325), 100.0, delta=0.5)

    def test_clausius_clapeyron_fallback_hits_boiling_point(self):
        self.assertAlmostEqual(thermo.psat("light", 60.0), thermo.P_ATM_BAR, places=6)
        self.assertEqual(thermo.psat("salt", 100.0), 0.0)
        self.assertFalse(thermo.is_volatile("salt"))

    def test_bubble_and_dew_points_bracket_the_two_phase_region(self):
        z = {"light": 0.5, "heavy": 0.5}
        t_bub, t_dew = thermo.bubble_temperature(z, 1.0), thermo.dew_temperature(z, 1.0)
        self.assertLess(60.0, t_bub)
        self.assertLess(t_bub, t_dew)
        self.assertLess(t_dew, 120.0)
        self.assertAlmostEqual(thermo.bubble_pressure(z, t_bub), 1.0, places=3)
        self.assertAlmostEqual(thermo.dew_pressure(z, t_dew), 1.0, places=3)

    def test_flash_satisfies_mass_balance_and_equilibrium(self):
        z = {"light": 0.5, "heavy": 0.5}
        t = 0.5 * (thermo.bubble_temperature(z, 1.0) + thermo.dew_temperature(z, 1.0))
        beta, x, y = thermo.flash_tp(z, t, 1.0)
        self.assertGreater(beta, 0.0)
        self.assertLess(beta, 1.0)
        for name in z:
            self.assertAlmostEqual(beta * y[name] + (1 - beta) * x[name], z[name], places=6)
            self.assertAlmostEqual(y[name], thermo.k_values(z, t, 1.0)[name] * x[name], places=6)
        self.assertGreater(y["light"], x["light"])      # the light component concentrates in the vapor
        self.assertEqual(thermo.flash_tp(z, 20.0, 1.0)[0], 0.0)
        self.assertEqual(thermo.flash_tp(z, 300.0, 1.0)[0], 1.0)

    def test_noncondensable_stays_in_the_vapor(self):
        beta, x, y = thermo.flash_tp({"water": 0.9, "N2": 0.1}, 25.0, 1.01325)
        self.assertGreater(y["N2"], 0.9)
        self.assertAlmostEqual(x.get("N2", 0.0), 0.0, places=4)

    def test_adiabatic_flash_recovers_temperature(self):
        z = {"light": 0.5, "heavy": 0.5}
        t = 0.5 * (thermo.bubble_temperature(z, 1.0) + thermo.dew_temperature(z, 1.0))
        beta, x, y = thermo.flash_tp(z, t, 1.0)
        n = 10.0
        h = thermo.mixture_enthalpy({k: n * (1 - beta) * v for k, v in x.items()}, t, "liquid") + \
            thermo.mixture_enthalpy({k: n * beta * v for k, v in y.items()}, t, "vapor")
        t_back, beta_back, _, _ = thermo.flash_ph({k: n * v for k, v in z.items()}, h, 1.0)
        self.assertAlmostEqual(t_back, t, places=3)
        self.assertAlmostEqual(beta_back, beta, places=3)

    def test_activity_model_hook(self):
        z = {"light": 0.5, "heavy": 0.5}
        ideal = thermo.bubble_pressure(z, 80.0)
        thermo.set_activity_model(lambda x, t: {k: 2.0 for k in x})
        try:
            self.assertAlmostEqual(thermo.bubble_pressure(z, 80.0), 2.0 * ideal, places=6)
        finally:
            thermo.set_activity_model()
        self.assertAlmostEqual(thermo.bubble_pressure(z, 80.0), ideal, places=9)

    def test_material_vapor_pressure_uses_thermo(self):
        m = Material.from_fractions({"water": 1.0}, 10.0, 100.0)
        self.assertAlmostEqual(m.vapor_pressure(), thermo.psat("water", 100.0), places=9)


class GasPlantTests(unittest.TestCase):
    def setUp(self):
        use_default_chemistry()

    def test_gas_obeys_ideal_gas_law(self):
        g = Gas.at_pressure({"N2": 1.0}, 3.0, 100.0, 40.0)
        self.assertAlmostEqual(g.pressure(100.0), 3.0, places=9)
        g.temperature = 313.15 + 100.0 - 40.0 - 273.15 + 40.0     # +100 K
        self.assertAlmostEqual(g.pressure(100.0), 3.0 * (140.0 + 273.15) / (40.0 + 273.15) * 313.15 / 413.15 * 413.15 / 313.15 *
                               (g.temperature + 273.15) / (140.0 + 273.15), places=6)

    def _fill_plant(self, supply=6.0):
        p = PlantModel()
        p.add_equipment(GasSource("GS", supply_pressure=supply))
        p.add_equipment(GasPipe("GP", inlets=["in"], outlets=["out"]))
        p.add_equipment(GasVolume("V", volume=500.0, initial_pressure=1.0))
        p.connect("GS", "out", "GP", "in")
        p.connect("GP", "out", "V", "in")
        return p

    def test_receiver_fills_to_supply_pressure_without_overshoot(self):
        p = self._fill_plant(6.0)
        v = p.equipment["V"]
        peak = 0.0
        for _ in range(3000):
            p.step(DT)
            peak = max(peak, v.pressure)
        self.assertLessEqual(peak, 6.0 + 1e-6)
        self.assertAlmostEqual(v.pressure, 6.0, delta=0.05)
        moles_in = p.equipment["GS"].supplied.total_moles
        self.assertAlmostEqual(moles_in, v.gas.total_moles - Gas.at_pressure({"N2": 1}, 1.0, 500.0).total_moles, delta=1.0)

    def test_gas_valve_blocks_and_drains_to_sink(self):
        p = PlantModel()
        p.add_equipment(GasVolume("V", volume=200.0, initial_pressure=5.0))
        p.add_equipment(GasValve("XV", inlets=["in"], outlets=["out"]))
        p.add_equipment(GasSink("GK", sink_pressure=1.013))
        p.connect("V", "out", "XV", "in")
        p.connect("XV", "out", "GK", "in")
        run(p, 5)
        self.assertAlmostEqual(p.equipment["V"].pressure, 5.0, places=6)   # closed valve: nothing moves
        p.equipment["XV"].command = 1.0
        run(p, 120)
        self.assertAlmostEqual(p.equipment["V"].pressure, 1.013, delta=0.05)
        self.assertGreater(p.equipment["GK"].received.total_moles, 0.0)

    def test_gas_flows_through_check_valve_one_way(self):
        p = PlantModel()
        p.add_equipment(GasVolume("LOW", volume=200.0, initial_pressure=1.0))
        p.add_equipment(GasCheckValve("CV", inlets=["in"], outlets=["out"]))
        p.add_equipment(GasVolume("HIGH", volume=200.0, initial_pressure=4.0))
        p.connect("LOW", "out", "CV", "in")
        p.connect("CV", "out", "HIGH", "in")
        run(p, 20)
        self.assertAlmostEqual(p.equipment["HIGH"].pressure, 4.0, places=6)     # would flow backwards without it
        self.assertAlmostEqual(p.equipment["LOW"].pressure, 1.0, places=6)

    def test_gas_pipe_blockage_fault_and_repair(self):
        p = self._fill_plant()
        run(p, 0.5)
        free = p.equipment["GP"].flow
        p.set_fault("GP", "blockage", 0.6)
        p.step(DT)
        self.assertLess(p.equipment["GP"].flow, free)
        p.repair("GP")
        self.assertEqual(p.equipment["GP"].blockage, 0.0)

    def test_gas_leak_and_relief_valve(self):
        p = self._fill_plant(10.0)
        v = p.equipment["V"]
        v.relief_pressure = 3.0
        run(p, 300)
        self.assertLessEqual(v.pressure, 3.0 + 1e-6)
        self.assertTrue(v.relieving)
        self.assertGreater(v.vented.total_moles, 0.0)
        p.set_fault("V", "leak_rate", 5.0)
        p.set_fault("GP", "blockage", 1.0)
        before = v.gas.total_moles
        run(p, 5)
        self.assertLess(v.gas.total_moles, before - 20.0)
        p.repair("V")
        self.assertEqual(v.leak_rate, 0.0)

    def test_ambient_temperature_changes_gas_pressure(self):
        p = PlantModel()
        p.environment = Environment(mean_c=-20.0)
        p.slow_time_scale = 100.0
        v = p.add_equipment(GasVolume("V", volume=100.0, initial_pressure=3.0, temperature=25.0, ambient_ua=20.0))
        run(p, 600)
        self.assertLess(v.temperature, 0.0)
        self.assertLess(v.pressure, 3.0 * 0.95)

    def _compressor_plant(self, **compressor):
        p = PlantModel()
        p.add_equipment(GasSource("GS", supply_pressure=1.013))
        p.add_equipment(Compressor("K", inlets=["in"], outlets=["out"], **compressor))
        p.add_equipment(GasVolume("RCV", volume=500.0, initial_pressure=1.013))
        p.add_equipment(GasValve("XV", inlets=["in"], outlets=["out"], initial_position=1.0))
        p.add_equipment(GasSink("GK", sink_pressure=1.013))
        p.connect("GS", "out", "K", "in")
        p.connect("K", "out", "RCV", "in")
        p.connect("RCV", "out", "XV", "in")
        p.connect("XV", "out", "GK", "in")
        return p

    def test_compressor_builds_pressure_heats_gas_and_respects_shutoff(self):
        p = self._compressor_plant(max_flow=10.0, max_ratio=4.0)
        p.equipment["XV"].command = 0.0
        p.equipment["XV"].position = 0.0
        k, rcv = p.equipment["K"], p.equipment["RCV"]
        run(p, 5)
        self.assertEqual(rcv.pressure, 1.013)           # stopped compressor blocks the line
        k.run_cmd = True
        peak = 0.0
        for _ in range(900):
            p.step(DT)
            peak = max(peak, rcv.pressure)
            k.tripped = False
            k.surge_time = 0.0
        self.assertGreater(rcv.pressure, 3.0)
        self.assertLessEqual(peak, 1.013 * 4.0 + 0.01)
        self.assertGreater(k.discharge_temp, 80.0)

    def test_compressor_flow_against_demand_and_wear_reduces_capacity(self):
        p = self._compressor_plant(max_flow=10.0, max_ratio=4.0)
        k = p.equipment["K"]
        k.run_cmd = True
        run(p, 120)
        self.assertGreater(k.flow, 1.0)
        self.assertTrue(k.running)
        new_capacity = k.capacity()
        p.set_fault("K", "wear", 0.8)
        self.assertLess(k.capacity(), new_capacity)
        p.repair("K")
        self.assertEqual(k.wear, 0.0)

    def test_compressor_surges_and_trips_on_closed_discharge(self):
        p = self._compressor_plant(surge_trip=60.0, max_discharge_temp=1000.0)
        p.equipment["XV"].command = 0.0
        p.equipment["XV"].position = 0.0
        k = p.equipment["K"]
        k.run_cmd = True
        p.slow_time_scale = 500.0
        run(p, 45)
        self.assertTrue(k.surge)
        self.assertGreater(k.wear, 0.0)
        run(p, 40)
        self.assertTrue(k.tripped)
        k.run_cmd = False
        k._reset(True)
        self.assertFalse(k.tripped)

    def test_gas_and_liquid_ports_cannot_be_mixed(self):
        p = PlantModel()
        p.add_equipment(GasSource("GS"))
        p.add_equipment(Sink("K"))
        with self.assertRaises(ValueError):
            p.connect("GS", "out", "K", "in")

    def test_unconnected_gas_element_is_rejected(self):
        p = PlantModel()
        p.add_equipment(GasPipe("GP", inlets=["in"], outlets=["out"]))
        with self.assertRaises(ValueError):
            p.step(DT)

    def test_gas_state_roundtrip(self):
        p = self._compressor_plant()
        p.equipment["K"].run_cmd = True
        run(p, 30)
        state = p.get_state()
        q = self._compressor_plant()
        q.set_state(state)
        run(p, 20)
        run(q, 20)
        self.assertAlmostEqual(p.equipment["RCV"].pressure, q.equipment["RCV"].pressure, places=6)
        self.assertAlmostEqual(p.equipment["K"].discharge_temp, q.equipment["K"].discharge_temp, places=6)


class FlashDrumTests(unittest.TestCase):
    def setUp(self):
        binary_chemistry()

    def tearDown(self):
        use_default_chemistry()

    def _drum(self, **kw):
        options = dict(max_capacity=100.0, height=2.0, initial_volume=50.0, composition={"water": 1.0},
                       temperature=25.0, headspace_pressure=1.013)
        options.update(kw)
        return FlashDrum("D", inlets=["in", "vapor_in"], outlets=["out", "vapor_out"], **options)

    def test_ports_are_split_between_networks(self):
        d = self._drum()
        self.assertEqual(d.inlets, ["in"])
        self.assertEqual(d.outlets, ["out"])
        self.assertEqual(d.gas_inlets, ["vapor_in"])
        self.assertEqual(d.gas_outlets, ["vapor_out"])

    def test_closed_drum_pressure_is_inert_gas_plus_vapor_pressure(self):
        p = PlantModel()
        d = p.add_equipment(self._drum())
        inert = d.gas.moles["N2"] * gas_pressure_factor(25.0) / d.gas_volume_l()
        run(p, 60)
        self.assertAlmostEqual(d.headspace_pressure, inert + thermo.psat("water", 25.0), delta=0.003)

    def test_heating_boils_and_pressure_rises_then_cooling_condenses(self):
        p = PlantModel()
        d = p.add_equipment(self._drum(initial_volume=5.0, heater_power=20e3, cooling_ua=500.0, coolant_temp=10.0))
        d.heater_command = 100.0
        run(p, 80)
        self.assertGreater(d.temperature, 60.0)
        hot_pressure = d.headspace_pressure
        self.assertGreater(hot_pressure, 1.5)
        water_vapor_hot = d.gas.moles["water"]
        d.heater_command, d.cooling_command = 0.0, 100.0
        run(p, 600)
        self.assertLess(d.temperature, 40.0)
        self.assertLess(d.gas.moles["water"], 0.1 * water_vapor_hot)
        self.assertLess(d.headspace_pressure, hot_pressure)

    def test_vented_drum_boils_at_the_boiling_point_and_loses_liquid(self):
        p = PlantModel()
        d = p.add_equipment(self._drum(initial_volume=10.0, heater_power=50e3))
        p.add_equipment(GasValve("XV", inlets=["in"], outlets=["out"], initial_position=1.0))
        p.add_equipment(GasSink("GK", sink_pressure=1.013))
        p.connect("D", "vapor_out", "XV", "in")
        p.connect("XV", "out", "GK", "in")
        d.heater_command = 100.0
        volume_before = d.volume
        run(p, 250)
        self.assertGreater(d.temperature, 98.0)
        self.assertLess(d.temperature, 106.0)     # slightly above 100 C: the vent valve holds the drum a little above 1 atm
        self.assertLess(d.volume, volume_before)
        self.assertGreater(p.equipment["GK"].received.moles.get("water", 0.0), 0.0)

    def test_drum_condenses_incoming_vapor(self):
        p = PlantModel()
        d = p.add_equipment(self._drum(initial_volume=5.0, composition={"water": 1.0}, cooling_ua=2000.0, coolant_temp=10.0))
        d.cooling_command = 100.0
        p.add_equipment(GasSource("GS", composition={"water": 1.0}, temperature=105.0, supply_pressure=1.3))
        p.add_equipment(GasPipe("GP", inlets=["in"], outlets=["out"]))
        p.connect("GS", "out", "GP", "in")
        p.connect("GP", "out", "D", "vapor_in")
        before = d.volume
        run(p, 120)
        self.assertGreater(d.volume, before)

    def test_relief_valve_limits_drum_pressure(self):
        p = PlantModel()
        d = p.add_equipment(self._drum(initial_volume=5.0, heater_power=50e3, relief_pressure=2.0))
        d.heater_command = 100.0
        run(p, 120)
        self.assertLessEqual(d.headspace_pressure, 2.0 + 1e-6)
        self.assertTrue(d.relieving)
        self.assertGreater(d.vented.total_moles, 0.0)

    def test_pump_cavitates_when_drum_is_near_its_boiling_point(self):
        p = PlantModel()
        d = p.add_equipment(StorageTank("T", inlets=["in"], outlets=["out"], max_capacity=100.0, height=2.0,
                                        initial_volume=40.0, composition={"water": 1.0}, temperature=99.5))
        p.add_equipment(Valve("XV", inlets=["in"], outlets=["out"], rated_dp=0.02, initial_position=1.0))
        p.add_equipment(Pump("P", inlets=["in"], outlets=["out"], npsh_required=0.3))
        p.add_equipment(Sink("K"))
        p.connect("T", "out", "XV", "in")
        p.connect("XV", "out", "P", "in")
        p.connect("P", "out", "K", "in")
        p.equipment["P"].run_cmd = True
        run(p, 10)
        self.assertGreater(p.equipment["P"].cavitation, 0.0)

    def test_drum_state_roundtrip(self):
        p = PlantModel()
        d = p.add_equipment(self._drum(heater_power=20e3))
        d.heater_command = 100.0
        run(p, 30)
        q = PlantModel()
        e = q.add_equipment(self._drum(heater_power=20e3))
        q.set_state(p.get_state())
        run(p, 20)
        run(q, 20)
        self.assertAlmostEqual(d.temperature, e.temperature, places=6)
        self.assertAlmostEqual(d.headspace_pressure, e.headspace_pressure, places=6)


class SolidsAndFreezingTests(unittest.TestCase):
    def setUp(self):
        binary_chemistry()

    def tearDown(self):
        use_default_chemistry()

    def _tank(self, salt_fraction=0.02, temperature=25.0):
        t = StorageTank("T", inlets=["in"], outlets=["out"], max_capacity=200.0, height=2.0, initial_volume=100.0,
                        composition={"water": 1.0 - salt_fraction, "salt": salt_fraction}, temperature=temperature)
        p = PlantModel()
        p.add_equipment(t)
        return p, t

    def test_oversaturated_solution_precipitates_and_conserves_moles(self):
        p, t = self._tank()
        total = t.contents.moles["salt"]
        self.assertGreater(t.concentration("salt"), 1.0)
        run(p, 300)
        self.assertGreater(t.solids["salt"], 0.0)
        self.assertAlmostEqual(t.solids["salt"] + t.contents.moles["salt"], total, places=6)
        self.assertAlmostEqual(t.concentration("salt"), 1.0, delta=0.05)
        self.assertGreater(t.solids_volume, 0.0)

    def test_cooling_precipitates_more_and_heating_redissolves(self):
        p, t = self._tank(salt_fraction=0.017, temperature=60.0)      # under-saturated at 60 C (solubility 2.75)
        run(p, 60)
        self.assertNotIn("salt", t.solids)
        t.temperature = 10.0
        run(p, 300)
        cold_solids = t.solids["salt"]
        self.assertGreater(cold_solids, 0.0)
        t.temperature = 60.0
        run(p, 300)
        self.assertLess(t.solids.get("salt", 0.0), cold_solids)

    def test_solids_take_up_volume_and_can_plug_the_outlet(self):
        p, t = self._tank()
        free = t.max_inflow(1.0)
        t.solids = {"salt": 1000.0}
        self.assertLess(t.max_inflow(1.0), free)
        self.assertFalse(t.plugged)
        t.solids = {"salt": 5000.0}
        self.assertTrue(t.plugged)
        self.assertFalse(t.can_supply("out"))

    def test_repair_cleans_settled_solids(self):
        p, t = self._tank()
        run(p, 120)
        self.assertTrue(t.solids)
        p.repair("T")
        self.assertEqual(t.solids, {})

    def test_vessel_freezes_blocks_flow_and_thaws(self):
        p, t = self._tank(salt_fraction=0.0, temperature=5.0)
        t.temperature = -2.0
        p.step(DT)
        self.assertTrue(t.frozen)
        self.assertFalse(t.can_supply("out"))
        self.assertFalse(t.can_accept("in"))
        t.temperature = 0.5
        p.step(DT)
        self.assertTrue(t.frozen)           # hysteresis: not thawed until above the freezing point + margin
        t.temperature = 3.0
        p.step(DT)
        self.assertFalse(t.frozen)
        self.assertTrue(t.can_supply("out"))

    def test_cold_weather_freezes_an_outdoor_tank(self):
        p, t = self._tank(salt_fraction=0.0, temperature=5.0)
        t.ambient_ua = 100.0
        p.environment = Environment(mean_c=-25.0)
        p.slow_time_scale = 200.0
        run(p, 600)
        self.assertTrue(t.frozen)

    def test_fresh_solids_need_time_to_dissolve_and_a_stopped_stirrer_settles_them(self):
        p, t = self._tank(salt_fraction=0.0)
        t.add_solids("salt", 50.0)
        self.assertGreater(t.stream("out").moles["salt"], 49.0)         # leaves with the flow while suspended
        run(p, 30)
        self.assertGreater(t.slurry["salt"], 5.0)                       # not dissolved yet
        run(p, 600)
        self.assertNotIn("salt", t.slurry)
        self.assertAlmostEqual(t.contents.moles["salt"] + t.solids.get("salt", 0.0), 50.0, places=6)
        p2, t2 = self._tank(salt_fraction=0.0)
        t2.mix_rate = 0.0
        t2.add_solids("salt", 50.0)
        run(p2, 600)
        self.assertLess(t2.slurry.get("salt", 0.0), 0.1)                     # without stirring it settles instead

    def test_slurry_clogs_a_pipe_and_the_restriction_damages_it(self):
        pipe = Pipe("P")
        pipe.flow = 1.0
        slurry = Material.from_fractions({"water": 0.97, "salt": 0.03}, 100.0, 25.0)    # far above saturation
        clean = Material.from_fractions({"water": 1.0}, 100.0, 25.0)
        pipe.after_step(1.0, False, clean)
        self.assertEqual(pipe.fouling, 0.0)
        for _ in range(600):
            pipe.after_step(1.0, False, slurry)
        self.assertGreater(pipe.slurry_load, 0.5)
        self.assertGreater(pipe.fouling, 0.5)
        self.assertGreater(pipe.fatigue, 0.0)

    def test_solids_and_frozen_flag_survive_snapshot(self):
        p, t = self._tank()
        run(p, 60)
        t.temperature = -5.0
        t.contents.moles["salt"] = 0.0
        p.step(DT)
        state = p.get_state()
        q, u = self._tank()
        q.set_state(state)
        self.assertEqual(u.solids, t.solids)
        self.assertTrue(u.frozen)


class RepairTests(unittest.TestCase):
    def setUp(self):
        use_default_chemistry()

    def test_repair_resets_every_kind_of_deterioration(self):
        p = PlantModel()
        pipe = p.add_equipment(Pipe("PL", inlets=["in"], outlets=["out"]))
        pump = p.add_equipment(Pump("P", inlets=["in"], outlets=["out"]))
        valve = p.add_equipment(Valve("XV", inlets=["in"], outlets=["out"]))
        pipe.fouling, pipe.ice, pipe.blockage, pipe.leak_rate = 0.5, 0.4, 0.3, 1.0
        pump.wear, pump.cavitation_time, pump.leak_rate = 0.9, 5.0, 0.2
        valve.stuck = True
        p.repair("PL")
        p.repair("P")
        p.repair("XV")
        self.assertEqual((pipe.fouling, pipe.ice, pipe.blockage, pipe.leak_rate), (0.0, 0.0, 0.0, 0.0))
        self.assertEqual((pump.wear, pump.cavitation_time, pump.leak_rate), (0.0, 0.0, 0.0))
        self.assertFalse(valve.stuck)

    def test_repair_keeps_operator_state_and_trips(self):
        p = PlantModel()
        pump = p.add_equipment(Pump("P", inlets=["in"], outlets=["out"]))
        pump.run_cmd, pump.tripped, pump.wear = True, True, 0.5
        p.repair("P")
        self.assertTrue(pump.run_cmd)
        self.assertTrue(pump.tripped)       # only the PLC's ResetCmd clears a trip
        self.assertEqual(pump.wear, 0.0)

    def test_repair_all(self):
        p = PlantModel()
        a = p.add_equipment(Pump("A", inlets=["in"], outlets=["out"]))
        b = p.add_equipment(Pump("B", inlets=["in"], outlets=["out"]))
        a.wear = b.wear = 0.7
        p.repair_all()
        self.assertEqual((a.wear, b.wear), (0.0, 0.0))


class LoaderTests(unittest.TestCase):
    def tearDown(self):
        use_default_chemistry()

    def test_gas_and_flash_equipment_load_from_the_database(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as s:
            seed_chemistry(s)
            plant = Plant(name="gas_plant")
            s.add(plant)
            s.flush()
            drum = add_equipment(plant, "D", "flash_drum",
                                 {"max_capacity": 100.0, "initial_volume": 20.0, "composition": {"water": 1.0},
                                  "heater_power": 10e3, "relief_pressure": 3.0},
                                 inlets=("in", "vapor_in"), outlets=("out", "vapor_out"))
            xv = add_equipment(plant, "XV", "gas_valve", {"initial_position": 1.0}, ("in",), ("out",))
            comp = add_equipment(plant, "K", "compressor", {"max_ratio": 3.0}, ("in",), ("out",))
            gs = add_equipment(plant, "GS", "gas_source", {"supply_pressure": 1.5}, (), ("out",))
            rcv = add_equipment(plant, "RCV", "gas_volume", {"volume": 300.0}, ("in",), ("out",))
            gk = add_equipment(plant, "GK", "gas_sink", {}, ("in",), ())
            connect(plant, drum, "vapor_out", xv, "in")
            connect(plant, xv, "out", rcv, "in")
            connect(plant, gs, "out", comp, "in")
            connect(plant, comp, "out", drum, "vapor_in")
            connect(plant, rcv, "out", gk.ports[0].equipment, "in")
            add_instrument(plant, "PT1", "pressure_sensor", rcv, {})
            add_instrument(plant, "FT1", "flow_sensor", comp, {"range_max": 50.0})
            s.commit()
            model = load_plant(s, "gas_plant")
            model.equipment["K"].run_cmd = True
            run(model, 20)
            self.assertIn(("K", "Surge"), model.signal_table())
            self.assertIn(("D", "HeaterCmd"), model.signal_table())
            self.assertGreater(model.instruments["PT1"].value, 0.0)


if __name__ == "__main__":
    unittest.main()
