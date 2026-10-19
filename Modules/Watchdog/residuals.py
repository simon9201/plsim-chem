import numpy as np

from Modules.Watchdog.model import AnomalyDetector

# =====================================================================
# Residuals: signals derived from the PHYSICS of the plant, not from any single sensor.
#
# A leak does not show up on one sensor; it shows up as a mismatch between two:
#   balance     flow sensor upstream minus flow sensor downstream on the same unbranched line.
#               Healthy: ~0 (sensor noise only). A leak between them: positive and growing.
#   resistance  differential pressure divided by flow squared across one element.
#               Healthy: constant. Blockage, fouling or ice: it climbs, long before flow stops.
#
# Residuals are added as extra channels ("RES.<a>-<b>.balance", "RES.<dp>.resistance") so the recorder,
# the feature builder and the models treat them like any other tag. discover_residuals() finds them from
# the plant layout (the editor's Node / Edge / Probe lists), so a new plant needs no hand-written list.
#
#   specs   = discover_residuals(doc.nodes(), doc.edges(), doc.probes())
#   sampler = with_residuals(plant_sampler(plant), specs)      # or wrap a live link sampler
# =====================================================================

# Equipment a line passes straight through: flow in equals flow out except for leaks.
LINE_TYPES = {"pump", "valve", "check_valve", "pipe", "elbow", "gas_pipe", "gas_valve", "gas_check_valve", "compressor"}
FLOW_SENSORS = {"flow_sensor"}
DP_SENSORS = {"differential_pressure_sensor"}
MIN_FLOW = 1e-3         # below this the resistance ratio is meaningless


class ResidualSpec:
    def __init__(self, name, kind, a, b=None):
        self.name, self.kind, self.a, self.b = name, kind, a, b     # a, b are channel names

    def compute(self, values):
        if self.kind == "balance":
            return values[self.a] - values[self.b]
        flow = np.maximum(np.abs(values[self.b]), MIN_FLOW)         # resistance: a = dp channel, b = flow channel
        return values[self.a] / (flow * flow)

    def __repr__(self):
        return f"ResidualSpec({self.name!r}, {self.kind!r})"


def discover_residuals(nodes, edges, probes):
    """Find the leak (balance) and blockage (resistance) residuals a plant layout supports."""
    types = {n.tag: n.type for n in nodes}
    flow_probe = {}                 # equipment tag -> first flow sensor tag on it
    dp_probe = {}
    for pr in probes:
        if pr.type in FLOW_SENSORS:
            flow_probe.setdefault(pr.target, pr.tag)
        elif pr.type in DP_SENSORS:
            dp_probe.setdefault(pr.target, pr.tag)

    downstream = {}
    for e in edges:
        downstream.setdefault(e.from_tag, []).append(e.to_tag)

    specs = []
    for start, sensor in flow_probe.items():
        node, seen = start, {start}
        while len(downstream.get(node, ())) == 1:       # follow an unbranched line
            node = downstream[node][0]
            if node in seen:
                break
            seen.add(node)
            if node in flow_probe:
                other = flow_probe[node]
                specs.append(ResidualSpec(f"RES.{sensor}-{other}.balance", "balance",
                                          f"{sensor}.Value", f"{other}.Value"))
                break
            if types.get(node) not in LINE_TYPES:       # a tank or junction stores / splits flow: no balance
                break
    for equipment, dp in dp_probe.items():
        if equipment in flow_probe:
            specs.append(ResidualSpec(f"RES.{dp}.resistance", "resistance", f"{dp}.Value", f"{flow_probe[equipment]}.Value"))
    return specs


def with_residuals(sampler, specs):
    """Wrap a sampler so it also returns the residual channels (specs whose inputs are missing are skipped)."""
    def sample():
        values = sampler()
        for spec in specs:
            if spec.a in values and spec.b in values:
                values[spec.name] = float(spec.compute(values))
        return values
    return sample


def add_residuals(times, matrix, names, specs):
    """Append residual channels to a recording (times, matrix, names). Returns the extended triple."""
    index = {n: i for i, n in enumerate(names)}
    extra, extra_names = [], []
    for spec in specs:
        if spec.a in index and spec.b in index:
            extra.append(spec.compute({spec.a: matrix[:, index[spec.a]], spec.b: matrix[:, index[spec.b]]}))
            extra_names.append(spec.name)
    if not extra:
        return times, matrix, list(names)
    return times, np.column_stack([matrix] + extra), list(names) + extra_names


class ResidualMonitor:
    """Learns the normal spread of each residual and flags a residual that leaves it (leak / blockage detection).
    It needs only healthy data, so it works before any failure has been recorded."""

    def __init__(self, names, smoothing=12):
        self.names = list(names)
        self.smoothing = smoothing      # samples averaged before comparing, which suppresses sensor noise
        self.detector = AnomalyDetector()
        self._recent = []

    def _smooth(self, matrix):
        k = self.smoothing
        if len(matrix) < k:
            return matrix.mean(axis=0, keepdims=True)
        kernel = np.ones(k) / k
        return np.column_stack([np.convolve(matrix[:, j], kernel, mode="valid") for j in range(matrix.shape[1])])

    def fit(self, times, matrix, names):
        cols = [list(names).index(n) for n in self.names]
        self.detector.fit(self._smooth(matrix[:, cols]))
        return self

    def update(self, values):
        """Feed one reading dict; returns (score, worst residual name) once enough samples were seen. Score > ~5 = alarm."""
        self._recent.append([values[n] for n in self.names])
        self._recent = self._recent[-self.smoothing:]
        if len(self._recent) < self.smoothing:
            return None
        row = np.mean(self._recent, axis=0, keepdims=True)
        z = np.abs((row - self.detector.mean) / self.detector.std)[0]
        worst = int(np.argmax(z))
        return float(z[worst]), self.names[worst]
