from collections import defaultdict
from dataclasses import dataclass

from Modules.Entity.Process_equipment.transfer import Transfer

# =====================================================================
# PlantModel: the running plant, built from equipment, connections and
# instruments. It has no database code; Data/loader.py builds it from rows.
#
# Terms: a "node" is equipment where material accumulates or enters/leaves
# the plant (vessel, source, sink). A "line" is the chain of transfer
# elements (pump, valve, pipe) between two nodes.
# =====================================================================


@dataclass
class _Line:
    src: object
    src_port: str
    dst: object
    dst_port: str
    transfers: list


class PlantModel:
    def __init__(self, name):
        self.name = name
        self.equipment = {}     # tag -> equipment
        self.instruments = {}   # tag -> instrument
        self.time = 0.0         # s
        self._links = {}        # (tag, outlet port) -> (tag, inlet port)
        self._lines = None

    def add_equipment(self, eq):
        if eq.name in self.equipment:
            raise ValueError(f"Duplicate equipment tag {eq.name}")
        if isinstance(eq, Transfer) and (len(eq.inlets) != 1 or len(eq.outlets) != 1):
            raise ValueError(f"{eq.name}: pumps, valves and pipes need exactly one inlet and one outlet")
        self.equipment[eq.name] = eq
        self._lines = None
        return eq

    def add_instrument(self, instrument):
        if instrument.name in self.instruments or instrument.name in self.equipment:
            raise ValueError(f"Duplicate tag {instrument.name}")
        self.instruments[instrument.name] = instrument
        return instrument

    def connect(self, from_tag, from_port, to_tag, to_port):
        src, dst = self.equipment[from_tag], self.equipment[to_tag]
        if from_port not in src.outlets:
            raise ValueError(f"{from_tag} has no outlet '{from_port}'")
        if to_port not in dst.inlets:
            raise ValueError(f"{to_tag} has no inlet '{to_port}'")
        if (from_tag, from_port) in self._links:
            raise ValueError(f"{from_tag}.{from_port} is already connected")
        if any(target == (to_tag, to_port) for target in self._links.values()):
            raise ValueError(f"{to_tag}.{to_port} is already connected")
        self._links[(from_tag, from_port)] = (to_tag, to_port)
        self._lines = None

    def _build_lines(self):
        lines = []
        for tag, eq in self.equipment.items():
            if isinstance(eq, Transfer):
                continue
            for port in eq.outlets:
                key, chain = (tag, port), []
                while key in self._links:
                    next_tag, next_port = self._links[key]
                    nxt = self.equipment[next_tag]
                    if isinstance(nxt, Transfer):
                        if nxt in chain:
                            raise ValueError(f"Loop of pumps/valves/pipes at {next_tag}")
                        chain.append(nxt)
                        key = (next_tag, nxt.outlets[0])
                        continue
                    if not chain:
                        raise ValueError(f"{tag}.{port} is connected straight to {next_tag}; "
                                         "put a pipe, valve or pump between them")
                    lines.append(_Line(eq, port, nxt, next_port, chain))
                    break
        return lines

    def step(self, dt):
        """Advance the plant by dt seconds."""
        if self._lines is None:
            self._lines = self._build_lines()
        lines = self._lines
        nodes = [eq for eq in self.equipment.values() if not isinstance(eq, Transfer)]
        for eq in self.equipment.values():
            eq.advance(dt)
            if isinstance(eq, Transfer):
                eq.flow = 0.0

        # 1. What each line would like to carry: the most restrictive element decides
        wanted = [min(t.flow_limit() for t in line.transfers) for line in lines]

        # 2. Scale down where a node cannot supply or accept that much
        out_total, in_total = defaultdict(float), defaultdict(float)
        for line, q in zip(lines, wanted):
            out_total[line.src.name] += q
            in_total[line.dst.name] += q

        def scale(limit, total):
            return limit / total if total > limit else 1.0

        flows = []
        for line, q in zip(lines, wanted):
            factor = min(scale(line.src.max_outflow(dt), out_total[line.src.name]),
                         scale(line.dst.max_inflow(dt), in_total[line.dst.name]))
            flows.append(q * factor)

        # 3. Streams are taken from the state at the start of the step, before anything is updated
        inflows, outflow = defaultdict(list), defaultdict(float)
        for line, q in zip(lines, flows):
            starved = line.src.max_outflow(dt) <= 1e-9
            for t in line.transfers:
                t.observe(dt, starved)
            if q <= 0:
                continue
            material = line.src.outlet_material(line.src_port, q)
            for t in line.transfers:
                t.flow = q
                t.temperature = material.temperature
            inflows[line.dst.name].append(material)
            outflow[line.src.name] += q

        # 4. Update every node
        for node in nodes:
            node.update(dt, inflows[node.name], outflow[node.name])
        self.time += dt
        for inst in self.instruments.values():
            inst.sample()

    def read_instruments(self):
        """Latest measured values, by instrument tag."""
        return {tag: inst.value for tag, inst in self.instruments.items()}

    def signal_table(self):
        """All PLC signals of the plant: {(entity tag, signal name): Signal}."""
        table = {}
        for tag, entity in {**self.equipment, **self.instruments}.items():
            for signal in entity.signals():
                table[(tag, signal.name)] = signal
        return table

    def apply_failsafe(self):
        """Put every actuator in its safe state (PLC connection lost)."""
        for eq in self.equipment.values():
            eq.apply_failsafe()
