from collections import deque

import numpy as np

from Modules.Watchdog.access import OperatorView
from Modules.Watchdog.residuals import LINE_TYPES

# =====================================================================
# HealthMonitor: a per-node "how worried should we be about this piece of equipment" estimate, built only
# from operator-visible data (measured feedback, reported lab results and derived residual channels).
# No GUI, no database: feed it samples, read the node risks. The Health tab only draws them.
#
# How it works, per node:
#   1. Learning: for the first `learn_samples` samples per channel (and again after maintenance) it records
#      what measurements look like when healthy (mean and spread per channel).
#   2. Watching: each channel is smoothed and compared with that baseline (a z-score). The node's score is
#      its worst channel. A short deviation gives a risk straight away ("instant").
#   3. Wear: persistent or slowly growing deviation accumulates (a CUSUM). Gradual degradation such as
#      fouling or fatigue shows up here long before any single reading is alarming. Wear decays slowly when
#      readings return to normal.
#   4. risk = max(instant, wear), 0..1. The forecast fits a line to the recent risk and extrapolates to
#      the alarm level: "about N hours until alarm".
#   5. Maintenance: maintenance(tag) clears the node's wear and relearns its baseline, so the picture
#      follows repairs. A change of operating point that is not a fault ("new normal") is handled the same
#      way with maintenance(tag, relearn_only=True), which keeps the history but starts a new baseline.
#
# Limits: the baseline is only valid for the operating point it learned. It does not know the physics, so
# it flags "this changed" and not "why"; the reasons list names the channels that moved most.
# =====================================================================

WATCH, WARNING, ALARM = 0.3, 0.6, 0.8      # risk levels for the status text and colours
Z_DEADBAND = 3.0                            # deviations below this many sigma are noise and add no risk
WEAR_SCALE = 60.0                           # accumulated excess z that corresponds to "very worn" (63 % wear)


def measurement_sampler(source):
    """Sampler over analog PLC feedback plus completed lab results; PLC commands are not used for health scoring.
    `source` is an OperatorView (a PlantModel is wrapped in one, so the AI never touches equipment directly)."""
    view = source if isinstance(source, OperatorView) else OperatorView(source)
    return view.measured


class _Baseline:
    def __init__(self):
        self.reset()

    def reset(self):
        self.n, self.mean, self.m2 = 0, 0.0, 0.0
        self.smooth = None

    def learn(self, x):                     # Welford's running mean and variance
        self.n += 1
        d = x - self.mean
        self.mean += d / self.n
        self.m2 += d * (x - self.mean)

    @property
    def std(self):
        return float(np.sqrt(self.m2 / self.n)) if self.n > 1 else 0.0


class NodeHealth:
    def __init__(self, tag):
        self.tag = tag
        self.learning = True
        self.instant = 0.0
        self.cusum = 0.0
        self.risk = 0.0
        self.worst = []                         # [(channel, z)] largest deviations, for the reasons list
        self.history = deque(maxlen=600)        # (time, risk)
        self.repairs = 0
        self.monitored = False                  # True once a measured channel belongs to this node

    @property
    def wear(self):
        return 1.0 - float(np.exp(-self.cusum / WEAR_SCALE))

    @property
    def status(self):
        if not self.monitored:
            return "unmonitored"
        if self.learning:
            return "learning"
        return "alarm" if self.risk >= ALARM else "warning" if self.risk >= WARNING else \
            "watch" if self.risk >= WATCH else "ok"


class HealthMonitor:
    def __init__(self, nodes, probes, edges=(), specs=(), learn_samples=60, smoothing=8, rel_floor=0.02, abs_floor=1e-3):
        """nodes/probes/edges as from PlantDocument (or any objects with the same attributes);
        specs from discover_residuals, so leak and blockage channels are charged to the equipment on that line."""
        self.owner = {}                         # channel prefix (entity tag) -> node tag
        self.health = {n.tag: NodeHealth(n.tag) for n in nodes}
        for n in nodes:
            self.owner[n.tag] = n.tag
        for p in probes:
            if p.target in self.health:
                self.owner[p.tag] = p.target
        self.residual_nodes = {s.name: self._line_nodes(s, nodes, probes, edges) for s in specs}
        self.learn_samples, self.alpha = learn_samples, 2.0 / (smoothing + 1)
        self.rel_floor, self.abs_floor = rel_floor, abs_floor
        self.base = {}                          # channel -> _Baseline
        self.events = []                        # (time, text): maintenance and relearn actions

    # ------------------------------------------------------------ wiring
    def _line_nodes(self, spec, nodes, probes, edges):
        """Equipment a residual is charged to: the sensors' own equipment and everything between them."""
        target = {p.tag: p.target for p in probes}
        ends = [target.get(c.split(".")[0]) for c in (spec.a, spec.b) if c]
        ends = [e for e in ends if e]
        if len(ends) < 2 or spec.kind != "balance":
            return set(ends)
        types = {n.tag: n.type for n in nodes}
        down = {}
        for e in edges:
            down.setdefault(e.from_tag, []).append(e.to_tag)
        for first, last in (ends, ends[::-1]):
            path, node = {first}, first
            while len(down.get(node, ())) == 1:
                node = down[node][0]
                path.add(node)
                if node == last:
                    return path
                if types.get(node) not in LINE_TYPES:
                    break
        return set(ends)

    def _nodes_of(self, channel):
        if channel in self.residual_nodes:
            return self.residual_nodes[channel]
        node = self.owner.get(channel.split(".")[0])
        return {node} if node else set()

    # ------------------------------------------------------------ live updates
    def update(self, values, t):
        scores = {tag: [] for tag in self.health}
        for channel, x in values.items():
            nodes = self._nodes_of(channel)
            if not nodes or not np.isfinite(x):
                continue
            for n in nodes:
                self.health[n].monitored = True
            b = self.base.setdefault(channel, _Baseline())
            if b.n < self.learn_samples:
                b.learn(x)
                continue
            b.smooth = x if b.smooth is None else b.smooth + self.alpha * (x - b.smooth)
            sigma = max(b.std, self.rel_floor * abs(b.mean), self.abs_floor)
            z = abs(b.smooth - b.mean) / sigma
            for n in nodes:
                scores[n].append((channel, z))
        for tag, h in self.health.items():
            if h.learning:
                owned = [c for c in self.base if tag in self._nodes_of(c)]
                if owned and all(self.base[c].n >= self.learn_samples for c in owned):
                    h.learning = False
                h.history.append((t, 0.0))
                continue
            ranked = sorted(scores[tag], key=lambda cz: -cz[1])
            h.worst = ranked[:3]
            top = ranked[0][1] if ranked else 0.0
            excess = max(0.0, top - Z_DEADBAND)
            h.instant = 1.0 - float(np.exp(-excess / 6.0))
            h.cusum = max(0.0, h.cusum * 0.9995 + excess)           # slow forgetting, so recovery is gradual
            h.risk = max(h.instant, h.wear)
            h.history.append((t, h.risk))

    # ------------------------------------------------------------ maintenance
    def maintenance(self, tag, t=0.0, relearn_only=False):
        """A repair (or an accepted new operating point): forget wear and learn a fresh baseline."""
        h = self.health[tag]
        h.learning, h.cusum, h.instant, h.risk, h.worst = True, 0.0, 0.0, 0.0, []
        if not relearn_only:
            h.repairs += 1
        for channel in list(self.base):
            if tag in self._nodes_of(channel):
                self.base[channel].reset()
        self.events.append((t, f"{'relearn' if relearn_only else 'repair'} {tag}"))

    # ------------------------------------------------------------ reading results
    def risks(self):
        return {tag: h.risk for tag, h in self.health.items()}

    def forecast(self, tag, horizon_points=120):
        """Seconds until the node's risk reaches the alarm level if the recent trend continues,
        0 if it is already there, None if it is not rising (or too little history)."""
        h = self.health[tag]
        if h.learning or len(h.history) < 20:
            return None
        if h.risk >= ALARM:
            return 0.0
        recent = list(h.history)[-horizon_points:]
        ts = np.array([r[0] for r in recent])
        rs = np.array([r[1] for r in recent])
        if ts[-1] - ts[0] <= 0:
            return None
        slope = np.polyfit(ts - ts[0], rs, 1)[0]
        if slope <= 1e-9:
            return None
        return float((ALARM - h.risk) / slope)

    def reasons(self, tag):
        return [f"{c} deviates {z:.1f} sigma from its healthy value" for c, z in self.health[tag].worst if z >= Z_DEADBAND]
