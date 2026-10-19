import math

# =====================================================================
# Time profiles for injected faults (simulation side only: the AI never imports this).
#
# A Profile maps plant time to a severity in 0..peak. Real degradation is not one clean ramp, so every
# fault draws a random shape, onset, speed and size:
#
#   ramp      steady creep (fouling, wear)
#   exp       slow at first, then accelerating (runaway, crack growth)
#   step      sudden change after a short transition (a valve slammed, a line plugged)
#   sawtooth  creeps, is partly cleared, creeps again (operator cleaning, pigging, back-flushing)
#   bursts    sporadic episodes that come and go and tend to get worse (loose connection, intermittent
#             cavitation, a valve that sticks now and then)
#
# Because the onset, shape and size differ per episode, the AI cannot learn "the fault starts at minute X".
# =====================================================================

KINDS = ("ramp", "exp", "step", "sawtooth", "bursts")


class Profile:
    def __init__(self, kind, onset, peak, duration, rng, horizon):
        if kind not in KINDS:
            raise ValueError(f"Unknown profile kind '{kind}'")
        self.kind, self.onset, self.peak, self.duration = kind, float(onset), float(peak), max(float(duration), 1.0)
        self.curve = rng.uniform(2.0, 5.0)
        self.transition = rng.uniform(5.0, 60.0)
        self.bursts = []
        if kind == "bursts":
            t, n = self.onset, 0
            while t < horizon:
                t += rng.uniform(20.0, 600.0)                       # quiet gap
                length = rng.uniform(20.0, 400.0)
                level = min(1.0, rng.uniform(0.25, 0.6) + 0.12 * n)      # later bursts tend to be worse
                self.bursts.append((t, t + length, self.peak * level))
                t += length
                n += 1

    def __call__(self, t):
        u = t - self.onset
        if u < 0.0:
            return 0.0
        if self.kind == "step":
            return self.peak * min(1.0, u / self.transition)
        if self.kind == "ramp":
            return self.peak * min(1.0, u / self.duration)
        if self.kind == "exp":
            x = min(1.0, u / self.duration)
            return self.peak * math.expm1(self.curve * x) / math.expm1(self.curve)
        if self.kind == "sawtooth":
            return self.peak * ((u / self.duration) % 1.0)
        for start, end, level in self.bursts:
            if start <= t < end:
                edge = min(1.0, (t - start) / 15.0, (end - t) / 15.0)
                return level * edge
        return 0.0


def random_profile(rng, horizon, peak_range, kinds=KINDS, onset_range=(0.08, 0.65), duration_range=(300.0, 3600.0)):
    """A randomly shaped profile somewhere inside the episode."""
    return Profile(rng.choice(kinds), rng.uniform(*onset_range) * horizon, rng.uniform(*peak_range),
                   rng.uniform(*duration_range), rng, horizon)
