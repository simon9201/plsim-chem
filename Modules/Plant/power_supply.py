import math
import random

# =====================================================================
# PowerSupply: the electrical network feeding every motor in the plant (one shared supply).
#
#   voltage_factor = 1 + offset + wander - (depth of any dip active now)      (1.0 = nominal)
#
#   offset  steady sag (negative) or swell (positive), e.g. -0.1 = weak grid at 10 % under
#   wander  random fluctuation (Ornstein-Uhlenbeck: correlated noise, std = `noise`, memory `noise_tau`)
#   events  scheduled dips: (start s, duration s, depth), plant time. depth 0.3 = 30 % sag, 1.0 = blackout
#
# The defaults are a perfect supply (factor 1.0 always), so nothing changes unless you ask for it.
# Motors read `voltage_factor` (Modules/Entity/Process_equipment/Motor/motor.py) and heat, derate,
# trip or age accordingly. The fluctuations are seeded, so a scenario can be repeated exactly.
# =====================================================================


class PowerSupply:
    def __init__(self, nominal_v=400.0, noise=0.0, noise_tau=30.0, seed=0):
        self.nominal_v = nominal_v
        self.noise = noise
        self.noise_tau = noise_tau
        self.offset = 0.0
        self.events = []
        self.seed = seed
        self._rng = random.Random(seed)
        self._wander = 0.0
        self.voltage_factor = 1.0

    @property
    def voltage(self):
        return self.nominal_v * self.voltage_factor

    def add_event(self, start, duration, depth):
        """Schedule a voltage dip at plant time `start` (s) lasting `duration` (s)."""
        self.events.append((float(start), float(duration), float(depth)))

    def clear_events(self):
        self.events = []

    def update(self, t, dt):
        if self.noise > 0 and self.noise_tau > 0:
            a = math.exp(-dt / self.noise_tau)
            self._wander = self._wander * a + self.noise * math.sqrt(1.0 - a * a) * self._rng.gauss(0.0, 1.0)
        dip = sum(depth for start, duration, depth in self.events if start <= t < start + duration)
        self.voltage_factor = max(0.0, 1.0 + self.offset + self._wander - dip)

    def get_state(self):
        return {"nominal_v": float(self.nominal_v), "noise": float(self.noise), "noise_tau": float(self.noise_tau),
                "offset": float(self.offset), "events": [list(e) for e in self.events], "seed": int(self.seed),
                "wander": float(self._wander), "voltage_factor": float(self.voltage_factor)}

    def set_state(self, state):
        self.nominal_v = state.get("nominal_v", self.nominal_v)
        self.noise = state.get("noise", 0.0)
        self.noise_tau = state.get("noise_tau", 30.0)
        self.offset = state.get("offset", 0.0)
        self.events = [tuple(e) for e in state.get("events", [])]
        self.seed = state.get("seed", 0)
        self._wander = state.get("wander", 0.0)
        self.voltage_factor = state.get("voltage_factor", 1.0)
        # The random stream is re-seeded on restore: the statistics are kept, the exact sequence is not.
        self._rng = random.Random(self.seed + int(self._wander * 1e6))
