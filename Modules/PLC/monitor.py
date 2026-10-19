import math
import time

from Modules.Entity.Entity import FROM_PLC, TO_PLC
from Modules.PLC.bridge import LinkError, PlcBridge

# =====================================================================
# PLC layer view (no GUI): what the PLC sees and does, and where it goes wrong.
#
#   PlcMonitor - watches every mapped tag: current value, how long since it changed, and a list of problems
#                that really occur on a plant: a tag that points at nothing, a frozen sensor, a NaN, a command the
#                equipment does not follow (pump told to run but not running, valve told to open but still closed),
#                an equipment fault flag, and a lost link.
#   PlcCycle   - one PLC scan against the plant: read commands, step, write feedback, with fail-safe when the
#                link is lost (the same logic as Runtimes/run_plant.py), and the monitor updated each scan.
# Deliberately shallow: it flags what an operator would notice on a PLC screen, it does not model the PLC program.
# =====================================================================

FLATLINE_S = 120.0      # a measurement that moved before, then stays exactly constant this long, is suspect
FOLLOW_S = 10.0         # a command must be followed by its feedback within this time

# (command signal, commanded value, feedback signal, value the feedback should reach, what it means when it doesn't)
FOLLOW_PAIRS = (
    ("RunCmd", True, "RunningFb", True, "commanded to run but not running (tripped, interlocked or no power)"),
    ("OpenCmd", True, "OpenFb", True, "commanded open but not fully open (stuck or blocked)"),
    ("OpenCmd", False, "ClosedFb", True, "commanded closed but not closed (stuck)"),
)


class PlcMonitor:
    def __init__(self, plant, tags):
        table = plant.signal_table()
        self.tags = list(tags)
        self.signals = {t.plc_symbol: table.get((t.entity_tag, t.signal)) for t in self.tags}
        self.values = {}
        self.changed_at = {}            # symbol -> sim time of the last change
        self.moved = set()              # symbols that have changed at least once
        self._since = {}                # (entity, command, fb) -> sim time the mismatch began
        self.link_ok = True
        self.link_error = ""
        self.time = 0.0

    def update(self, t):
        self.time = t
        for symbol, sig in self.signals.items():
            if sig is None:
                continue
            try:
                value = sig.read()
            except Exception:       # an unreadable signal is shown as missing instead of stopping the display
                value = None
            if symbol in self.values and value != self.values[symbol]:
                self.changed_at[symbol] = t
                self.moved.add(symbol)
            self.changed_at.setdefault(symbol, t)
            self.values[symbol] = value
        self._track_commands(t)

    def _track_commands(self, t):
        """Note when a command first stops being followed by its feedback (see FOLLOW_PAIRS)."""
        by_entity = {}
        for tag in self.tags:
            by_entity.setdefault(tag.entity_tag, {})[tag.signal] = tag.plc_symbol
        for entity, symbols in by_entity.items():
            for cmd, cmd_value, fb, fb_value, _ in FOLLOW_PAIRS:
                if cmd not in symbols or fb not in symbols:
                    continue
                c, f = self.values.get(symbols[cmd]), self.values.get(symbols[fb])
                key = (entity, cmd, fb)
                if c is not None and f is not None and bool(c) == cmd_value and bool(f) != fb_value:
                    self._since.setdefault(key, t)
                else:
                    self._since.pop(key, None)

    def rows(self):
        out = []
        for tag in self.tags:
            sig = self.signals[tag.plc_symbol]
            out.append({"symbol": tag.plc_symbol, "entity": tag.entity_tag, "signal": tag.signal,
                        "direction": tag.direction, "type": tag.data_type, "unit": sig.unit if sig else "",
                        "value": self.values.get(tag.plc_symbol),
                        "age": self.time - self.changed_at.get(tag.plc_symbol, self.time)})
        return out

    def problems(self):
        """[(severity, entity tag, text)], severity 'error' or 'warn'."""
        found = []
        if not self.link_ok:
            found.append(("error", "link", f"PLC link lost, plant on fail-safe commands ({self.link_error})"))
        by_entity = {}
        for tag in self.tags:
            sig = self.signals[tag.plc_symbol]
            if sig is None:
                found.append(("error", tag.entity_tag, f"{tag.plc_symbol} maps to '{tag.signal}', which does not exist"))
                continue
            by_entity.setdefault(tag.entity_tag, {})[tag.signal] = tag.plc_symbol
            value = self.values.get(tag.plc_symbol)
            if isinstance(value, float) and not math.isfinite(value):
                found.append(("error", tag.entity_tag, f"{tag.signal} is not a number"))
            elif (tag.direction == TO_PLC and sig.data_type == "REAL" and tag.plc_symbol in self.moved
                  and self.time - self.changed_at[tag.plc_symbol] > FLATLINE_S):
                found.append(("warn", tag.entity_tag, f"{tag.signal} frozen for {self.time - self.changed_at[tag.plc_symbol]:.0f} s (sensor stuck or link stale?)"))
            if tag.signal == "Fault" and value:
                found.append(("error", tag.entity_tag, "equipment reports a fault"))
        for entity, symbols in by_entity.items():
            for cmd, cmd_value, fb, fb_value, meaning in FOLLOW_PAIRS:
                began = self._since.get((entity, cmd, fb))
                if began is not None and self.time - began >= FOLLOW_S:
                    found.append(("error", entity, meaning))
        return found

    def problem_entities(self):
        return {e for _, e, _ in self.problems() if e != "link"}


class PlcCycle:
    """Drive a plant from a PLC link, one scan per call."""

    def __init__(self, plant, tags, link):
        self.plant, self.link = plant, link
        self.bridge = PlcBridge(plant, tags, link)
        self.monitor = PlcMonitor(plant, tags)
        self.connected = False
        self.scans = 0
        self.scan_ms = 0.0

    def step(self, dt):
        started = time.perf_counter()
        if not self.connected:
            try:
                self.link.connect()
                self.connected = True
                self.monitor.link_ok, self.monitor.link_error = True, ""
            except LinkError as err:
                self._lost(err, dt)
                return
        try:
            self.bridge.read_commands()
            self.plant.step(dt)
            self.bridge.write_feedback()
        except LinkError as err:
            self.connected = False
            self._lost(err, dt, step=False)
            return
        self.scans += 1
        self.scan_ms = 0.9 * self.scan_ms + 0.1 * (time.perf_counter() - started) * 1000.0
        self.monitor.update(self.plant.time)

    def _lost(self, err, dt, step=True):
        self.monitor.link_ok, self.monitor.link_error = False, str(err)
        self.plant.apply_failsafe()
        if step:
            self.plant.step(dt)     # the plant keeps running on its fail-safe commands
        self.monitor.update(self.plant.time)

    def set_command(self, symbol, value):
        """Play the PLC by hand on a loopback link (memory is the PLC's variable space)."""
        memory = getattr(self.link, "memory", None)
        if memory is None:
            raise ValueError("only a loopback link can be written by hand")
        memory[symbol] = value

    def close(self):
        self.link.close()
