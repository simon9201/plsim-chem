import math

import numpy as np

from Modules.Entity.Entity import TO_PLC, FROM_PLC

# =====================================================================
# The AI's window on the plant: OperatorView.
#
# Rule: the watchdog may only know what a plant operator or a historian can know:
#   * PLC signals: sensor values and feedback (TO_PLC), and the commands the PLC sent (FROM_PLC)
#   * lab results, and only once the lab has reported them (delayed, noisy, as in the real world)
#   * the plant's drawings (which equipment exists and how it is connected), which are not in this class
#   * the work-order log: when a mechanic repaired something (a CMMS record)
# It must NOT know the hidden degradation state: leak size, fouling, blockage, fatigue, scaling, wear,
# insulation used, stuck actuators. Those are only visible through what they do to the signals.
#
# Everything the AI side reads goes through this class. It keeps no public reference to the plant, so
# there is no `view.plant.equipment[...]` to reach for. `audit_hidden_state` proves that none of the
# exposed signals read the hidden state directly (tests/test_ai_access.py runs it on a full plant).
# The truth is available separately in evaluation.py, which is only for scoring and for the GUI's
# "ground truth" line. No AI module may import it.
# =====================================================================

# Names of hidden condition variables on equipment. FAULTS lists are added to these automatically.
HIDDEN_CONDITION = ("leak_rate", "blockage", "fouling", "fatigue", "wear", "scaling", "thermal_damage",
                    "ice", "frozen", "cavitation", "cavitation_time", "freeze_thaw_cycles", "deadheading",
                    "slurry_load")
HIDDEN_MOTOR = ("insulation_used", "failed")


class OperatorView:
    _UNIT_FLOORS = {"A": 0.02, "bar": 0.001, "C": 0.05, "kW": 0.005, "L/s": 0.005,
                    "mm/s": 0.02, "mol/s": 1e-5, "%": 0.05, "V": 0.1}

    def __init__(self, plant, lab=None, lab_tags=(), noise_fraction=0.0, drift_fraction_per_hour=0.0, seed=None):
        self._table = dict(plant.signal_table())
        self._plant = plant                 # private on purpose: only time, work orders and the lab clock are used
        self._lab = lab
        self._lab_tags = list(lab_tags)
        if noise_fraction < 0 or drift_fraction_per_hour < 0:
            raise ValueError("measurement noise and drift must be non-negative")
        self._noise_fraction = float(noise_fraction)
        self._drift_fraction_per_hour = float(drift_fraction_per_hour)
        self._rng = np.random.default_rng(seed)
        self._bias = {}
        self._last_time = {}

    # ------------------------------------------------------------ what can be read
    @property
    def time(self):
        return self._plant.time

    def measured(self):
        """Measured analog feedback plus lab results that have completed their turnaround time."""
        out = {f"{t}.{n}": self._read((t, n), s) for (t, n), s in self._table.items()
               if s.direction == TO_PLC and s.data_type == "REAL"}
        out.update(self.lab_channels())
        return out

    def feedback(self):
        """All PLC feedback signals, including BOOL and INT, but never the PLC's FROM_PLC commands."""
        return {f"{t}.{n}": self._read((t, n), s) for (t, n), s in self._table.items()
                if s.direction == TO_PLC}

    def commands(self):
        """{"<tag>.<signal>": float} for what the PLC is commanding."""
        return {f"{t}.{n}": float(s.read()) for (t, n), s in self._table.items() if s.direction == FROM_PLC}

    def read(self):
        """Every PLC-visible signal (inputs, outputs and raw counts), plus lab channels when a lab is attached."""
        out = {f"{t}.{n}": self._read((t, n), s) for (t, n), s in self._table.items()}
        out.update(self.lab_channels())
        return out

    def _read(self, key, signal):
        value = float(signal.read())
        if signal.direction != TO_PLC or signal.data_type != "REAL":
            return value
        now = float(self._plant.time)
        scale = max(abs(value), self._UNIT_FLOORS.get(signal.unit, 1e-3))
        last = self._last_time.get(key, now)
        elapsed_hours = max(0.0, now - last) / 3600.0
        bias = self._bias.get(key, 0.0)
        if elapsed_hours and self._drift_fraction_per_hour:
            bias += self._rng.normal(0.0, scale * self._drift_fraction_per_hour * math.sqrt(elapsed_hours))
            self._bias[key] = bias
        self._last_time[key] = now
        noise = self._rng.normal(0.0, scale * self._noise_fraction) if self._noise_fraction else 0.0
        return value + bias + noise

    def lab_channels(self):
        """LAB.<tag>.<analyte> and LAB.<tag>.age from reported results only (see Modules/Lab/lab.py with_lab)."""
        if self._lab is None:
            return {}
        from Modules.Lab.lab import with_lab
        tags = self._lab_tags or sorted({result.tag for result in self._lab.results})
        return with_lab(lambda: {}, self._lab, tags)()

    def work_orders(self):
        """[(time, tag)] for each repair carried out so far."""
        return list(self._plant.maintenance_log)


def audit_hidden_state(plant, lab=None, lab_tags=(), step=0.5):
    """Change each hidden condition variable and see whether any channel in the view changes *instantly*
    (without stepping the plant). Returns [(equipment tag, variable, channel)]; empty means no direct leak.
    The plant is restored afterwards. Effects that appear through the physics, over time, are what the AI
    is meant to find, and are not leaks."""
    view = OperatorView(plant, lab, lab_tags)
    view.read()                              # take a first sample so cached instrument readings are fixed
    leaks = []
    for tag, eq in plant.equipment.items():
        targets = [(eq, n) for n in sorted(set(getattr(eq, "FAULTS", ())) | set(HIDDEN_CONDITION))
                   if isinstance(getattr(eq, n, None), (bool, int, float))]
        motor = getattr(eq, "motor", None)
        if motor is not None:
            targets += [(motor, n) for n in HIDDEN_MOTOR]
        for obj, name in targets:
            old = getattr(obj, name)
            before = view.read()
            setattr(obj, name, (not old) if isinstance(old, bool) else old + step)
            try:
                after = view.read()
            finally:
                setattr(obj, name, old)
            for ch, b in before.items():
                a = after[ch]
                if not (a == b or (math.isnan(a) and math.isnan(b))):
                    leaks.append((tag, name, ch))
    return leaks
