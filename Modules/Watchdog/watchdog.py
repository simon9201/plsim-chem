from collections import deque
from dataclasses import dataclass, field

import numpy as np

from Modules.Watchdog.features import window_features

# =====================================================================
# Watchdog: read-only live inference. Feed it PLC feedback; it keeps a sliding window, asks the trained
# RiskModel for a risk score within the horizon, and applies alarm/clear hysteresis with consecutive-sample
# persistence so a single noisy score does not chatter.
#
#   watchdog.update(t, {"P101.Current": 3.1, ...}) -> Report once the window is full, else None
#
# It only reads and reports. It never writes to the PLC: acting on an alarm is the PLC program's
# (or the operator's) decision.
# =====================================================================


@dataclass
class Report:
    time: float
    risk: float                 # 0..1 model score
    alarm: bool
    anomaly: float = 0.0        # z-score of the window against healthy data (0 if no detector)
    reasons: list = field(default_factory=list)     # [(feature, contribution)] driving the risk


def alarm_transitions(scores, alarm_threshold, clear_threshold=0.5, alarm_samples=2, clear_samples=3):
    """Apply the same persistence and hysteresis policy to a sequence that the live Watchdog uses."""
    if alarm_samples < 1 or clear_samples < 1:
        raise ValueError("alarm and clear persistence must be at least one sample")
    alarm = False
    above, below = 0, 0
    active, onsets = [], []
    for i, score in enumerate(scores):
        if alarm:
            below = below + 1 if score < clear_threshold else 0
            if below >= clear_samples:
                alarm = False
                below = above = 0
        else:
            above = above + 1 if score >= alarm_threshold else 0
            if above >= alarm_samples:
                alarm = True
                onsets.append(i)
                above = below = 0
        active.append(alarm)
    return np.asarray(active, dtype=bool), np.asarray(onsets, dtype=int)


class Watchdog:
    def __init__(self, model, anomaly=None, alarm_threshold=0.8, clear_threshold=0.5, alarm_samples=2,
                 clear_samples=3):
        self.model = model
        self.anomaly = anomaly
        self.alarm_threshold = alarm_threshold
        self.clear_threshold = clear_threshold
        if alarm_samples < 1 or clear_samples < 1:
            raise ValueError("alarm and clear persistence must be at least one sample")
        if not np.isfinite(alarm_threshold) or not np.isfinite(clear_threshold) or clear_threshold < 0:
            raise ValueError("alarm thresholds must be finite and the clear threshold non-negative")
        if clear_threshold >= alarm_threshold:
            raise ValueError("clear threshold must be below the alarm threshold")
        self.alarm_samples = alarm_samples
        self.clear_samples = clear_samples
        self._above_count = 0
        self._below_count = 0
        self.alarm = False
        self._rows = deque(maxlen=model.window)
        self._last_t = -float("inf")
        self.history = []

    def update(self, t, values):
        """Add one reading (dict channel -> value). Samples closer than the model's period are skipped."""
        if not np.isfinite(t):
            raise ValueError("Watchdog sample time must be finite")
        if t < self._last_t:
            raise ValueError("Watchdog sample time moved backwards")
        if t - self._last_t < self.model.period - 1e-9:
            return None
        try:
            row = [float(values[c]) for c in self.model.channels]
        except KeyError as err:
            raise KeyError(f"Watchdog input is missing channel {err}") from None
        if not np.isfinite(row).all():
            raise ValueError("Watchdog input contains a non-finite feedback value")
        self._last_t = t
        self._rows.append(row)
        if len(self._rows) < self.model.window:
            return None
        x = window_features(np.array(self._rows))
        risk = float(self.model.predict_proba(x)[0])
        if not np.isfinite(risk) or not 0.0 <= risk <= 1.0:
            raise ValueError(f"RiskModel returned an invalid risk score: {risk}")
        if self.alarm:
            self._below_count = self._below_count + 1 if risk < self.clear_threshold else 0
            if self._below_count >= self.clear_samples:
                self.alarm = False
                self._above_count = self._below_count = 0
        else:
            self._above_count = self._above_count + 1 if risk >= self.alarm_threshold else 0
            if self._above_count >= self.alarm_samples:
                self.alarm = True
                self._above_count = self._below_count = 0
        anomaly = float(self.anomaly.score(x)[0]) if self.anomaly is not None else 0.0
        report = Report(t, risk, self.alarm, anomaly, self.model.explain(x) if self.alarm else [])
        self.history.append(report)
        return report
