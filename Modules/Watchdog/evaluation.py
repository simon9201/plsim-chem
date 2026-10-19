from Modules.Watchdog.access import HIDDEN_CONDITION, HIDDEN_MOTOR

# =====================================================================
# Ground truth: the hidden condition of the plant. ONLY for scoring the AI and for the GUI's "ground truth"
# line. The AI itself (health, features, models, watchdog) must never import this module; the access test
# checks that. In a real plant nobody has this information, which is why the AI exists.
# =====================================================================


class GroundTruth:
    def __init__(self, plant):
        self._plant = plant

    def condition(self, tag):
        """{variable: value} for every non-zero hidden condition variable of the equipment."""
        eq = self._plant.equipment.get(tag)
        if eq is None:
            return {}
        names = set(getattr(eq, "FAULTS", ())) | set(HIDDEN_CONDITION)
        out = {n: round(float(getattr(eq, n)), 3) for n in sorted(names)
               if isinstance(getattr(eq, n, None), (bool, int, float)) and getattr(eq, n)}
        motor = getattr(eq, "motor", None)
        if motor is not None:
            out.update({n: round(float(getattr(motor, n)), 3) for n in HIDDEN_MOTOR if getattr(motor, n)})
        return out

    def unhealthy(self, tag, threshold=0.2):
        """True if any wear-type variable is above `threshold` (0..1 scale) or a leak/stuck fault is active."""
        c = self.condition(tag)
        return any(v >= threshold for k, v in c.items() if k not in ("leak_rate", "stuck")) or \
            bool(c.get("stuck")) or c.get("leak_rate", 0.0) > 0.0
