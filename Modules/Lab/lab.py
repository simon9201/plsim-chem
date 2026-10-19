import random
from dataclasses import dataclass, field

from Modules.Flow import chemistry
from Modules.Flow.flow import Material

# =====================================================================
# Laboratory: simulated off-line analysis.
#
# take_sample(tag) physically removes a small volume of liquid from a vessel (or catches a stream from a
# Source, to check an incoming lot). The result is NOT available at once: it appears after `turnaround`
# seconds of plant time, measured with noise and a detection limit. So the plant (and the AI watchdog)
# sees lab data that is old, sparse and imperfect, like a real QC lab.
#
# Analytes: wt.<component> (mass %), density (g/L), temperature (C, at sampling).
# What the lab reports is the composition when the sample was taken, not when the result arrives.
# The lab queue is not part of PlantModel snapshots: restoring a snapshot leaves pending samples as they are.
# =====================================================================


@dataclass
class LabResult:
    sample_id: int
    tag: str
    taken_at: float
    ready_at: float
    values: dict = field(default_factory=dict)
    lot: str = ""
    out_of_spec: list = field(default_factory=list)


class Laboratory:
    def __init__(self, plant, turnaround=900.0, seed=None, rel_error=0.02, detection_limit_wt=0.01,
                 sample_volume=0.05):
        self.plant = plant
        self.turnaround = turnaround            # s of plant time from sampling to result
        self.rel_error = rel_error              # relative (1 sigma) measurement noise
        self.detection_limit_wt = detection_limit_wt    # mass % below which a component reads 0
        self.sample_volume = sample_volume      # L removed from the vessel per sample
        self.specs = {}                         # {tag: {analyte: (low, high)}}, None = no limit on that side
        self.pending = []
        self.results = []                       # every finished LabResult, oldest first
        self._rng = random.Random(seed)
        self._next_id = 1

    def take_sample(self, tag):
        """Draw a sample now. Returns the sample id. Raises if the equipment cannot be sampled."""
        eq = self.plant.equipment.get(tag)
        if eq is None:
            raise KeyError(f"Unknown equipment '{tag}'")
        if hasattr(eq, "stream") and hasattr(eq, "lot"):                    # a Source: incoming lot QC
            sample = eq.stream("out").scaled_to_volume(self.sample_volume)
            lot = eq.lot
        elif hasattr(eq, "contents") and isinstance(eq.contents, Material):   # a vessel: remove real liquid
            if eq.contents.volume < self.sample_volume:
                raise ValueError(f"{tag} has too little liquid to sample")
            sample = eq.contents.remove_volume(self.sample_volume)
            lot = ""
        else:
            raise ValueError(f"{tag} cannot be sampled (only vessels and sources)")
        now = self.plant.time
        result = LabResult(self._next_id, tag, now, now + self.turnaround, self._analyse(sample), lot)
        for analyte, (low, high) in self.specs.get(tag, {}).items():
            v = result.values.get(analyte)
            if v is not None and ((low is not None and v < low) or (high is not None and v > high)):
                result.out_of_spec.append(analyte)
        self._next_id += 1
        self.pending.append(result)
        return result.sample_id

    def _noisy(self, value):
        return max(0.0, value * (1.0 + self._rng.gauss(0.0, self.rel_error)))

    def _analyse(self, sample):
        values = {}
        for comp, w in sample.mass_fractions().items():
            wt = self._noisy(100.0 * w)
            values[f"wt.{comp}"] = wt if wt >= self.detection_limit_wt else 0.0
        values["density"] = self._noisy(sample.density)
        values["temperature"] = sample.temperature + self._rng.gauss(0.0, 0.1)
        return values

    def update(self):
        """Release the results that are due. Call once per plant step (PlantModel does this)."""
        if not self.pending:
            return
        now = self.plant.time
        due = [r for r in self.pending if r.ready_at <= now]
        if due:
            self.pending = [r for r in self.pending if r.ready_at > now]
            self.results.extend(sorted(due, key=lambda r: r.ready_at))

    def latest(self, tag):
        for r in reversed(self.results):
            if r.tag == tag:
                return r
        return None


def with_lab(sampler, lab, tags):
    """Wrap a {channel: float} sampler so it also reports the latest lab result of each tag:
    LAB.<tag>.<analyte> (held until the next result) and LAB.<tag>.age (s since the sample was taken, or the
    plant time when there is no result yet). The channel list is fixed (all known components), so it works as a
    model input. Same pattern as residuals.with_residuals."""
    tags = list(tags)

    def sample():
        out = dict(sampler())
        now = lab.plant.time
        analytes = [f"wt.{c}" for c in sorted(chemistry.COMPONENTS)] + ["density", "temperature"]
        for tag in tags:
            r = lab.latest(tag)
            for a in analytes:
                out[f"LAB.{tag}.{a}"] = float(r.values.get(a, 0.0)) if r else 0.0
            out[f"LAB.{tag}.age"] = float(now - r.taken_at) if r else float(now)
        return out

    return sample
