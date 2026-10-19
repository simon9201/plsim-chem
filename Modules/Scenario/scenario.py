import random
from dataclasses import asdict, dataclass, field

from Modules.Entity.Entity import FROM_PLC
from Modules.Flow.batch import make_lot
from Modules.Flow.flow import Material

# =====================================================================
# Scenarios: a timeline of things that happen to a plant (a leak, a lightning storm, a snow day).
#
# A Scenario is only data (a list of Events, JSON friendly). A ScenarioPlayer applies it to a running
# PlantModel: call player.update() once per simulation step (or once per GUI tick). Nothing in the
# physics knows about scenarios; the player only uses the public hooks (set_fault, power, environment).
#
# Event.action     what it does                                     uses
#   fault          plant.set_fault(target, name, value)              target, name, value
#   param          set a numeric attribute (degradation tuning)      target, name, value
#   supply_dip     voltage dip on the shared supply (negative = surge)  value = depth (0.3 = -30 %), duration
#   supply_offset  steady sag/surge of the supply                    value
#   supply_noise   random wander of the supply                       value (std, fraction)
#   ambient_offset shift the outdoor temperature (snow, heat wave)   value (K)
#   repair         plant.repair(target)                              target
#   feed_lot       a Source delivers a different lot (weaker assay,   target (Source), value = assay,
#                  impurities); reverts after `duration`             data {"impurities": {"I": 0.02}, "lot": "L7"}
#   charge         add a batch to a vessel                           target (vessel),
#                                                                    data {"composition": {...}, "volume": L, "temperature": C}
#   sample         send a lab sample of a vessel/Source to the lab   target
#   command        write a PLC command signal (RunCmd, OpenCmd,       target, name = signal, value
#                  HeaterCmd...) when no PLC is connected
#
# Event.time is plant seconds from the start of the scenario. With duration > 0 the change is undone after
# that long (a snow day ends); with ramp > 0 a fault/param moves gradually to its value (a leak that grows).
# =====================================================================

ACTIONS = ("fault", "param", "supply_dip", "supply_offset", "supply_noise", "ambient_offset", "repair",
           "feed_lot", "charge", "sample", "command")
NEEDS_TARGET = ("fault", "param", "repair", "feed_lot", "charge", "sample", "command")


@dataclass
class Event:
    time: float
    action: str
    target: str = ""
    name: str = ""
    value: float = 0.0
    duration: float = 0.0
    ramp: float = 0.0
    label: str = ""
    data: dict = field(default_factory=dict)    # extra settings for feed_lot / charge (see the table above)

    def describe(self):
        what = self.label or " ".join(str(x) for x in (self.action, self.target, self.name) if x)
        return f"t+{self.time:g}s  {what} = {self.value:g}" + (f" for {self.duration:g}s" if self.duration else "")

    def validate(self):
        if self.action not in ACTIONS:
            raise ValueError(f"Unknown action '{self.action}'. Known: {list(ACTIONS)}")
        if self.action in NEEDS_TARGET and not self.target:
            raise ValueError(f"Action '{self.action}' needs a target")
        if self.action in ("fault", "param", "command") and not self.name:
            raise ValueError(f"Action '{self.action}' needs a name")
        if self.time < 0 or self.duration < 0 or self.ramp < 0:
            raise ValueError("time, duration and ramp cannot be negative")
        if self.action == "charge" and not (self.data.get("composition") and self.data.get("volume", 0) > 0):
            raise ValueError("charge needs data: composition and a positive volume")
        if self.action == "supply_dip" and self.duration <= 0:
            raise ValueError("A supply dip needs a duration")


@dataclass
class Scenario:
    name: str
    description: str = ""
    events: list = field(default_factory=list)

    def add(self, event):
        event.validate()
        self.events.append(event)
        self.events.sort(key=lambda e: e.time)
        return event

    def to_dict(self):
        return {"name": self.name, "description": self.description, "events": [asdict(e) for e in self.events]}

    @classmethod
    def from_dict(cls, data):
        s = cls(data["name"], data.get("description", ""))
        for e in data.get("events", []):
            s.add(Event(**e))
        return s


def _write(plant, event, value):
    if event.action == "fault":
        plant.set_fault(event.target, event.name, value)
        return
    eq = plant.equipment[event.target]
    current = getattr(eq, event.name, None)
    if isinstance(current, bool) or not isinstance(current, (int, float)):
        raise ValueError(f"{event.target}.{event.name} is not a numeric attribute")
    setattr(eq, event.name, float(value))


def _read(plant, event):
    return float(getattr(plant.equipment[event.target], event.name, 0.0))


class ScenarioPlayer:
    def __init__(self, plant, scenario, start_time=None):
        self.plant = plant
        self.scenario = scenario
        self.t0 = plant.time if start_time is None else start_time
        self._pending = sorted(scenario.events, key=lambda e: e.time)
        self._active = []       # [event, start time, previous value, undo callable]
        self.log = []           # (plant time, text) of everything that happened

    @property
    def finished(self):
        return not self._pending and not self._active

    def update(self):
        t = self.plant.time
        rel = t - self.t0
        while self._pending and self._pending[0].time <= rel:
            self._start(self._pending.pop(0), t)
        for item in list(self._active):
            event, started, previous, undo = item
            elapsed = t - started
            if event.ramp > 0:
                _write(self.plant, event, previous + (event.value - previous) * min(1.0, elapsed / event.ramp))
            if event.duration > 0 and elapsed >= event.duration:
                if undo:
                    undo()
                self._active.remove(item)
                self.log.append((t, f"end: {event.describe()}"))
            elif event.duration == 0 and elapsed >= event.ramp:
                self._active.remove(item)       # a finished permanent ramp

    def _start(self, event, t):
        plant, undo, previous = self.plant, None, 0.0
        self.log.append((t, f"start: {event.describe()}"))
        if event.action in ("fault", "param"):
            previous = _read(plant, event)
            _write(plant, event, previous if event.ramp > 0 else event.value)
            undo = (lambda e=event, p=previous: _write(plant, e, p))
        elif event.action == "supply_dip":
            plant.power.add_event(t, event.duration, event.value)
            return
        elif event.action in ("supply_offset", "supply_noise"):
            attr = "offset" if event.action == "supply_offset" else "noise"
            previous = getattr(plant.power, attr)
            setattr(plant.power, attr, event.value)
            undo = (lambda a=attr, p=previous: setattr(plant.power, a, p))
        elif event.action == "ambient_offset":
            previous = plant.environment.offset
            plant.environment.offset = previous + event.value
            undo = (lambda p=previous: setattr(plant.environment, "offset", p))
        elif event.action == "repair":
            plant.repair(event.target)
            return
        elif event.action == "feed_lot":
            source = plant.equipment[event.target]
            old_composition, old_lot = dict(source.composition), source.lot
            source.set_lot(make_lot(old_composition, event.value or 1.0, event.data.get("impurities")),
                           event.data.get("lot", event.label or "lot"))
            undo = (lambda s=source, c=old_composition, l=old_lot: s.set_lot(c, l))
        elif event.action == "charge":
            d = event.data
            plant.equipment[event.target].receive(Material.from_fractions(
                d["composition"], d["volume"], d.get("temperature", plant.ambient_temperature)))
            return
        elif event.action == "sample":
            plant.lab.take_sample(event.target)
            return
        elif event.action == "command":
            # Plays the PLC's part when none is connected (a linked PLC overwrites this every cycle).
            signal = plant.signal_table().get((event.target, event.name))
            if signal is None or signal.direction != FROM_PLC:
                raise ValueError(f"{event.target}.{event.name} is not a command signal")
            signal.write(event.value)
            return
        if event.duration > 0 or event.ramp > 0:
            self._active.append([event, t, previous, undo])


# ---------------------------------------------------------------- presets
# Each preset returns a Scenario. Times and durations are plant seconds; pass a shorter `duration`
# when running with a large slow_time_scale.

def leak(target, rate=0.2, start=60.0, grow_over=0.0):
    s = Scenario(f"Leak on {target}", f"{target} starts leaking {rate} L/s")
    s.add(Event(start, "fault", target, "leak_rate", rate, ramp=grow_over, label=f"leak on {target}"))
    return s


def lightning_day(duration=86400.0, strikes=6, seed=0, start=0.0):
    """Grid disturbances: a few short deep dips and surges plus a noisy supply while the storm lasts."""
    rng = random.Random(seed)
    s = Scenario("Lightning day", f"{strikes} grid disturbances over {duration:g} s")
    s.add(Event(start, "supply_noise", value=0.02, duration=duration, label="storm: noisy supply"))
    for _ in range(strikes):
        at = start + rng.uniform(0.0, duration)
        depth = rng.choice((-0.12, 0.08, 0.15, 0.25))
        s.add(Event(at, "supply_dip", value=depth, duration=rng.uniform(0.2, 3.0), label="lightning strike"))
    return s


def snow_day(duration=86400.0, drop=-12.0, start=0.0):
    s = Scenario("Snow day", "Cold front: outdoor temperature falls and stays low")
    s.add(Event(start, "ambient_offset", value=drop, duration=duration, label="snow / cold front"))
    return s


def heat_wave(duration=86400.0, rise=10.0, start=0.0):
    s = Scenario("Heat wave", "Outdoor temperature rises")
    s.add(Event(start, "ambient_offset", value=rise, duration=duration, label="heat wave"))
    return s


def brownout(depth=0.17, duration=1800.0, start=0.0):
    s = Scenario("Brownout", f"Supply sags {depth:.0%} for {duration:g} s")
    s.add(Event(start, "supply_offset", value=-depth, duration=duration, label="brownout"))
    return s


PRESETS = {"leak": leak, "lightning_day": lightning_day, "snow_day": snow_day,
           "heat_wave": heat_wave, "brownout": brownout}
