import csv

import numpy as np

from Modules.Watchdog.access import OperatorView

# =====================================================================
# Recording the tags a watchdog learns from.
#
# A "channel" is one PLC-visible value, named "<equipment tag>.<signal>" (for example "P101.Current").
# A sampler is any function returning {channel: float}. Two are provided:
#   plant_sampler(plant)     reads PLC feedback only from a PlantModel (offline training, tests)
#   link_sampler(link, tags) reads the PLC variables over a PlcLink (live, what a real watchdog sees)
# SignalRecorder stores (time, values) rows and saves/loads them as CSV so a run can be replayed later.
# =====================================================================


def plant_sampler(plant):
    """Sampler over PLC feedback signals only, matching the live read-only watchdog input boundary."""
    view = plant if isinstance(plant, OperatorView) else OperatorView(plant)
    return view.feedback


def link_sampler(link, tags, direction=None):
    """Sampler over PLC variables. `tags` are TagMapping rows (Data/tags.py); `direction` limits to FROM_PLC/TO_PLC."""
    chosen = [t for t in tags if direction is None or t.direction == direction]

    def sample():
        values = link.read([t.plc_symbol for t in chosen])
        return {f"{t.entity_tag}.{t.signal}": float(values[t.plc_symbol]) for t in chosen}
    return sample


class SignalRecorder:
    def __init__(self, period=1.0):
        self.period = period            # seconds of plant time between stored rows
        self.names = []
        self.times = []
        self.rows = []
        self._last = -float("inf")

    def record(self, t, values):
        """Store a row if `period` has passed since the last one. Returns True if stored."""
        if t - self._last < self.period - 1e-9:
            return False
        if not self.names:
            self.names = sorted(values)
        self.times.append(float(t))
        self.rows.append([float(values[n]) for n in self.names])
        self._last = t
        return True

    def arrays(self):
        """(times, matrix[rows, channels], names)"""
        return np.array(self.times), np.array(self.rows, dtype=float).reshape(len(self.rows), len(self.names)), list(self.names)

    def save_csv(self, path):
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["time"] + self.names)
            for t, row in zip(self.times, self.rows):
                writer.writerow([t] + row)


def load_csv(path):
    """(times, matrix, names) from a file written by SignalRecorder.save_csv"""
    with open(path, newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        data = [[float(x) for x in row] for row in reader]
    arr = np.array(data, dtype=float).reshape(len(data), len(header))
    return arr[:, 0], arr[:, 1:], header[1:]
