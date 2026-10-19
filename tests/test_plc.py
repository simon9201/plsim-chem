import os
import sys
import types
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from Data.Model.model import Base
from Data.build_test_plant import PLANT_NAME, build_test_plant
from Data.loader import load_plant
from Data.tags import export_gvl, load_tags, sync_tags
from Modules.PLC.bridge import AdsLink, LinkError, LoopbackLink, PlcBridge
from Modules.PLC.monitor import PlcCycle

DT = 0.1


def make_plant():
    """The test plant in a throw-away in-memory database, wired to a loopback link."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        build_test_plant(session)
        plant = load_plant(session, PLANT_NAME)
        sync_tags(session, PLANT_NAME, plant)
        tags = load_tags(session, PLANT_NAME)
    link = LoopbackLink()
    return plant, tags, link, PlcBridge(plant, tags, link)


def cycle(plant, bridge, seconds):
    for _ in range(round(seconds / DT)):
        bridge.read_commands()
        plant.step(DT)
        bridge.write_feedback()


def sym(tag, signal):
    return f"GVL_Sim.{tag}_{signal}"


class PlcLoopTests(unittest.TestCase):
    def test_commands_drive_equipment_and_feedback_returns(self):
        plant, tags, link, bridge = make_plant()
        mem = link.memory
        cycle(plant, bridge, 5)
        self.assertFalse(mem[sym("P101", "RunningFb")])
        self.assertTrue(mem[sym("XV111", "ClosedFb")])

        mem[sym("XV111", "OpenCmd")] = True
        mem[sym("P101", "RunCmd")] = True
        cycle(plant, bridge, 1)
        self.assertFalse(mem[sym("XV111", "OpenFb")])       # valve is still travelling
        cycle(plant, bridge, 10)
        self.assertTrue(mem[sym("XV111", "OpenFb")])
        self.assertTrue(mem[sym("P101", "RunningFb")])
        self.assertAlmostEqual(plant.equipment["P101"].flow, 2.0, delta=0.3)
        self.assertAlmostEqual(mem[sym("FT101", "Value")], 2.0, delta=0.6)      # the sensor has random noise
        self.assertGreater(plant.equipment["R201"].volume, 200.0)

    def test_raw_matches_value(self):
        plant, tags, link, bridge = make_plant()
        cycle(plant, bridge, 1)
        value, raw = link.memory[sym("LT101", "Value")], link.memory[sym("LT101", "Raw")]
        self.assertAlmostEqual(raw / 32767 * 100, value, delta=0.01)
        self.assertAlmostEqual(plant.equipment["TK101"].level_percent, 60.0, delta=0.5)

    def test_analog_commands(self):
        plant, tags, link, bridge = make_plant()
        mem = link.memory
        mem[sym("P103", "RunCmd")] = True
        mem[sym("P103", "SpeedSP")] = 50.0
        mem[sym("XV113", "OpenCmd")] = True
        cycle(plant, bridge, 15)
        self.assertAlmostEqual(mem[sym("P103", "Speed")], 50.0, delta=0.1)
        self.assertAlmostEqual(plant.equipment["P103"].flow, 1.43, delta=0.1)

    def test_dry_run_trips_pump_until_reset(self):
        plant, tags, link, bridge = make_plant()
        mem = link.memory
        tank = plant.equipment["TK101"]
        tank.contents.moles = {k: 0.0 for k in tank.contents.moles}
        mem[sym("P101", "RunCmd")] = True
        cycle(plant, bridge, 5)
        self.assertTrue(mem[sym("P101", "Fault")])
        self.assertFalse(mem[sym("P101", "RunningFb")])

        mem[sym("P101", "RunCmd")] = False
        cycle(plant, bridge, 1)
        tank.contents.moles["A"] = 5000.0
        mem[sym("P101", "ResetCmd")] = True
        cycle(plant, bridge, 1)
        mem[sym("P101", "ResetCmd")] = False
        self.assertFalse(mem[sym("P101", "Fault")])

    def test_failsafe(self):
        plant, tags, link, bridge = make_plant()
        mem = link.memory
        mem[sym("P101", "RunCmd")] = True
        mem[sym("XV101", "OpenCmd")] = True
        cycle(plant, bridge, 10)
        plant.apply_failsafe()
        for _ in range(200):    # no PLC reads: the commands stay at their fail-safe values
            plant.step(DT)
            bridge.write_feedback()
        self.assertFalse(mem[sym("P101", "RunningFb")])
        self.assertTrue(mem[sym("XV101", "ClosedFb")])
        self.assertTrue(mem[sym("XV210", "OpenFb")])    # cooling water fails open

    def test_cooling_valve_controls_temperature(self):
        plant, tags, link, bridge = make_plant()
        mem = link.memory
        for t in ("XV111", "XV112", "XV113"):
            mem[sym(t, "OpenCmd")] = True
        for t in ("P101", "P102", "P103"):
            mem[sym(t, "RunCmd")] = True
        mem[sym("P103", "SpeedSP")] = 20.0
        mem[sym("XV210", "PositionSP")] = 0.0
        cycle(plant, bridge, 600)
        hot = mem[sym("TT201", "Value")]
        mem[sym("XV210", "PositionSP")] = 100.0
        cycle(plant, bridge, 600)
        self.assertGreater(hot, 60.0)
        self.assertLess(mem[sym("TT201", "Value")], hot - 20.0)

    def test_bad_tag_table_is_rejected(self):
        plant, tags, link, _ = make_plant()
        bad = tags[:1] + [type(tags[0])("P101", "NoSuchSignal", "from_plc", "BOOL", "GVL_Sim.x")]
        with self.assertRaises(ValueError):
            PlcBridge(plant, bad, link)

    def test_gvl_export(self):
        plant, tags, link, bridge = make_plant()
        text = export_gvl(tags)
        self.assertIn("P101_RunCmd : BOOL;", text)
        self.assertIn("LT101_Raw : INT;", text)
        self.assertEqual(text.count(" : "), len(tags))


class FakeConnection:
    memory = {}
    state = 5   # ADSSTATE_RUN

    def __init__(self, ams, port): pass
    def open(self): pass
    def close(self): pass
    def read_state(self): return (self.state, 0)
    def read_list_by_name(self, names):
        return {n: self.memory.get(n, "symbol not found") for n in names}
    def write_list_by_name(self, values):
        self.memory.update(values)
        return {n: "no error" for n in values}


class AdsLinkTests(unittest.TestCase):
    def setUp(self):
        fake = types.ModuleType("pyads")
        fake.Connection, fake.ADSError, fake.ADSSTATE_RUN = FakeConnection, type("ADSError", (Exception,), {}), 5
        self._saved = sys.modules.get("pyads")
        sys.modules["pyads"] = fake
        FakeConnection.memory, FakeConnection.state = {}, 5

    def tearDown(self):
        if self._saved is not None:
            sys.modules["pyads"] = self._saved
        else:
            del sys.modules["pyads"]

    def test_roundtrip(self):
        link = AdsLink()
        link.connect()
        link.write({"GVL_Sim.a": 1.5})
        self.assertEqual(link.read(["GVL_Sim.a"]), {"GVL_Sim.a": 1.5})

    def test_missing_symbol_is_a_link_error(self):
        link = AdsLink()
        link.connect()
        with self.assertRaises(LinkError):
            link.read(["GVL_Sim.missing"])

    def test_plc_not_running(self):
        FakeConnection.state = 6    # STOP
        with self.assertRaises(LinkError):
            AdsLink().connect()

    def test_not_connected(self):
        with self.assertRaises(LinkError):
            AdsLink().read(["x"])


class PlcMonitorTests(unittest.TestCase):
    def setUp(self):
        self.plant, self.tags, self.link, _ = make_plant()
        self.cyc = PlcCycle(self.plant, self.tags, self.link)

    def run_for(self, seconds):
        for _ in range(round(seconds / DT)):
            self.cyc.step(DT)

    def texts(self):
        return [t for _, _, t in self.cyc.monitor.problems()]

    def test_quiet_plant_has_no_problems_and_rows_show_values(self):
        self.run_for(5)
        self.assertEqual(self.cyc.monitor.problems(), [])
        row = next(r for r in self.cyc.monitor.rows() if r["symbol"] == sym("TK101", "Value") or r["signal"] == "Value")
        self.assertIsNotNone(row["value"])
        self.assertGreater(self.cyc.scans, 40)

    def test_pump_commanded_but_not_running_is_flagged_after_a_delay(self):
        self.link.memory[sym("XV111", "OpenCmd")] = True
        self.link.memory[sym("P101", "RunCmd")] = True
        self.plant.equipment["P101"].tripped = True
        self.run_for(5)
        self.assertFalse(any("not running" in t for t in self.texts()))
        self.run_for(10)
        self.assertTrue(any("not running" in t for t in self.texts()))
        self.assertIn("P101", self.cyc.monitor.problem_entities())

    def test_dead_link_puts_plant_on_failsafe_and_is_reported(self):
        self.run_for(2)

        class Dead(LoopbackLink):
            def connect(self):
                raise LinkError("no route")

        self.cyc.link = self.cyc.bridge.link = Dead()
        self.cyc.connected = False
        self.run_for(1)
        self.assertTrue(any("link lost" in t for t in self.texts()))
        self.assertGreater(self.plant.time, 2.0)        # still simulating on fail-safe

    def test_frozen_sensor_and_broken_mapping(self):
        self.link.memory[sym("XV111", "OpenCmd")] = True
        self.link.memory[sym("P101", "RunCmd")] = True
        self.run_for(20)
        probe = self.plant.instruments["FT101"]
        self.assertTrue(probe)
        mon = self.cyc.monitor
        symbol = sym("FT101", "Value")
        mon.moved.add(symbol)
        mon.changed_at[symbol] = mon.time - 500.0
        self.assertTrue(any("frozen" in t for t in self.texts()))
        mon.signals[sym("P101", "RunCmd")] = None
        self.assertTrue(any("does not exist" in t for t in self.texts()))

    def test_manual_command_on_loopback(self):
        self.cyc.set_command(sym("P101", "RunCmd"), True)
        self.assertTrue(self.link.memory[sym("P101", "RunCmd")])


if __name__ == "__main__":
    unittest.main()
