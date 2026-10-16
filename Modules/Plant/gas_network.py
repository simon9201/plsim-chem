import math

from Modules.Entity.Process_equipment.Gas.gas import GasTransfer

# =====================================================================
# GasNetwork: the gas side of the plant. PlantModel owns one and calls step() every step.
#
# Gas nodes (GasVolume, GasSource, GasSink, FlashDrum vapor ports) are joined by edges: chains of
# GasTransfer elements (pipe, valve, compressor ...), exactly like the liquid network.
# Unlike liquid, gas volumes store pressure, so there are no unknown junction pressures to iterate:
# every edge is evaluated explicitly from the pressures of its two end nodes,
#       P_up^2 - P_down^2 = R * Q^2          (R = sum over the elements, bar^2/(mol/s)^2)
# and the pressures then change as the moles move (dynamic, like a real receiver filling).
# For stability the flow in a step is capped to what would equalize the two ends (shared between the
# lines on a node), and to the gas that is actually there. A compressor edge is solved against the
# compressor characteristic instead (see Compressor), capped so a volume is never pumped above shutoff.
# =====================================================================


class GasEdge:
    def __init__(self, src, src_port, dst, dst_port, elements):
        self.src, self.src_port = src, src_port
        self.dst, self.dst_port = dst, dst_port
        self.elements = elements
        self.q = 0.0                # mol/s, positive = from src to dst
        self.blocked = False
        self.one_way = False
        self.compressor = None
        self.r_up = self.r_down = 0.0   # resistance before / after the compressor (all of it when there is none)

    def refresh(self):
        self.blocked = any(not e.status or e.is_blocked() for e in self.elements)
        self.one_way = any(e.one_way for e in self.elements)
        self.compressor = next((e for e in self.elements if e.is_compressor), None)
        before = True
        self.r_up = self.r_down = 0.0
        for e in self.elements:
            if e is self.compressor:
                before = False
            elif before:
                self.r_up += e.gas_resistance()
            else:
                self.r_down += e.gas_resistance()

    def _suction_pressure(self, p_src, q):
        return math.sqrt(max(p_src ** 2 - self.r_up * q * q, 0.0))

    def raw_flow(self):
        """Flow from the end pressures alone (before the step caps)."""
        if self.blocked:
            return 0.0
        p1 = self.src.gas_pressure(self.src_port)
        p2 = self.dst.gas_pressure(self.dst_port)
        if self.compressor is None:
            r = max(self.r_up, 1e-9)
            q = math.copysign(math.sqrt(abs(p1 * p1 - p2 * p2) / r), p1 - p2)
            return 0.0 if (self.one_way and q < 0) else q

        c = self.compressor
        cap = c.capacity()
        if cap <= 0:
            return 0.0
        q_hi = cap if self.r_up <= 0 else min(cap, p1 / math.sqrt(self.r_up))

        def margin(q):     # what the compressor can deliver minus what the downstream side needs
            return self._suction_pressure(p1, q) * c.ratio_at(q) - math.sqrt(p2 ** 2 + self.r_down * q * q)

        if margin(0.0) <= 0:
            return 0.0
        if margin(q_hi) >= 0:
            return q_hi
        lo, hi = 0.0, q_hi
        for _ in range(60):
            mid = 0.5 * (lo + hi)
            if margin(mid) > 0:
                lo = mid
            else:
                hi = mid
        return 0.5 * (lo + hi)

    def record(self):
        """Store the flow and the pressure at both sides of every element."""
        q = self.q
        p_src = self.src.gas_pressure(self.src_port)
        p_dst = self.dst.gas_pressure(self.dst_port)
        quad = q * abs(q)

        def step_forward(p, el):
            if el.is_compressor:
                return p * (el.ratio_at(abs(q)) if q > 0 and el.running else 1.0)
            return math.sqrt(max(p * p - el.gas_resistance() * quad, 0.0))

        stop = len(self.elements)
        if q == 0:
            stop = next((i for i, e in enumerate(self.elements) if not e.status or e.is_blocked()), stop)
        p = p_src
        for el in self.elements[:stop]:
            el.p_in = p
            p = step_forward(p, el)
            el.p_out = p
        if stop < len(self.elements):
            # Stopped line: upstream of the first blocked element follows the source, the rest follows the destination.
            p_block_in = p
            p = p_dst
            for el in reversed(self.elements[stop:]):
                el.p_in = el.p_out = p
            self.elements[stop].p_in = p_block_in
        for el in self.elements:
            el.flow = q


class GasNetwork:
    def __init__(self, plant):
        self.plant = plant
        self.edges = []
        self.nodes = {}
        self._node_edges = {}

    # ------------------------------------------------------------ building
    def build(self):
        eqs = self.plant.equipment
        links = self.plant._links
        out_map = {(f, fp): (t, tp) for f, fp, t, tp in links}
        in_map = {(t, tp) for _, _, t, tp in links}
        transfers = [e for e in eqs.values() if isinstance(e, GasTransfer)]
        for tr in transfers:
            if (tr.name, tr.inlets[0]) not in in_map:
                raise ValueError(f"{tr.name}: inlet is not connected")
            if (tr.name, tr.outlets[0]) not in out_map:
                raise ValueError(f"{tr.name}: outlet is not connected")

        self.nodes = {t: e for t, e in eqs.items()
                      if not isinstance(e, GasTransfer)
                      and (getattr(e, "domain", None) == "gas" or getattr(e, "gas_inlets", None) or getattr(e, "gas_outlets", None))}
        edges, used = [], set()
        for tag, node in self.nodes.items():
            ports = node.outlets if node.domain == "gas" else node.gas_outlets
            for port in ports:
                if (tag, port) not in out_map:
                    continue
                elements, (cur, cur_port) = [], out_map[(tag, port)]
                path = set()
                while isinstance(eqs[cur], GasTransfer):
                    tr = eqs[cur]
                    if tr.name in path:
                        raise ValueError(f"Gas transfer-only cycle detected while tracing from {tag}.{port}: {tr.name}")
                    path.add(tr.name)
                    elements.append(tr)
                    used.add(cur)
                    cur, cur_port = out_map[(cur, tr.outlets[0])]
                if cur not in self.nodes:
                    raise ValueError(f"{tag}.{port}: gas line ends at {cur}, which has no gas port")
                if sum(1 for e in elements if e.is_compressor) > 1:
                    raise ValueError(f"{tag}.{port}: more than one compressor in series; put a GasVolume between them")
                edges.append(GasEdge(node, port, eqs[cur], cur_port, elements))
        loose = [t.name for t in transfers if t.name not in used]
        if loose:
            raise ValueError(f"Gas pipe/valve/compressor not connected between two gas nodes: {loose}")
        self.edges = edges
        self._node_edges = {}
        for e in edges:
            self._node_edges[e.src.name] = self._node_edges.get(e.src.name, 0) + 1
            self._node_edges[e.dst.name] = self._node_edges.get(e.dst.name, 0) + 1

    # ------------------------------------------------------------ solving
    def _cap(self, sup, sup_port, acc, acc_port, edge, dt):
        """Largest flow (mol/s, magnitude) from sup to acc that keeps the step stable."""
        share = max(self._node_edges[sup.name], self._node_edges[acc.name])
        if not (sup.status and sup.can_supply_gas(sup_port) and acc.status and acc.can_accept_gas(acc_port)):
            return 0.0
        inventory = sup.gas_inventory()
        limit = 0.9 * inventory / (self._node_edges[sup.name] * dt) if math.isfinite(inventory) else math.inf
        p_sup, p_acc = sup.gas_pressure(sup_port), acc.gas_pressure(acc_port)
        c_sup, c_acc = sup.gas_buffer(), acc.gas_buffer()
        if edge.compressor is not None:
            reach = p_sup * edge.compressor.shutoff_ratio()
            room = (reach - p_acc) * c_acc / share / dt if math.isfinite(c_acc) else math.inf
        else:
            series = 1.0 / (1.0 / c_sup + 1.0 / c_acc) if (math.isfinite(c_sup) or math.isfinite(c_acc)) else math.inf
            room = (p_sup - p_acc) * series / share / dt
        return max(0.0, min(limit, room))

    def step(self, dt):
        for e in self.edges:
            e.refresh()
            q = e.raw_flow()
            if q > 0:
                q = min(q, self._cap(e.src, e.src_port, e.dst, e.dst_port, e, dt))
            elif q < 0:
                q = -min(-q, self._cap(e.dst, e.dst_port, e.src, e.src_port, e, dt))
            e.q = q
            e.record()

        for e in self.edges:
            starved = e.q == 0 and e.src.gas_inventory() <= 1e-9
            fluid = e.src.gas_stream(e.src_port) if e.elements else None
            for el in e.elements:
                el.after_step(dt, starved, fluid)

        moves = []
        for e in self.edges:
            if abs(e.q) * dt < 1e-15:
                continue
            if e.q > 0:
                sup, sup_port, acc, acc_port, elements = e.src, e.src_port, e.dst, e.dst_port, e.elements
            else:
                sup, sup_port, acc, acc_port, elements = e.dst, e.dst_port, e.src, e.src_port, list(reversed(e.elements))
            rate = abs(e.q)
            drawn = sup.gas_stream(sup_port).scaled_to_moles(rate * dt)
            moved = drawn.copy()
            for el in elements:
                el.condition(moved, rate)
            leaked = sum(el.leak_rate for el in e.elements)
            received = moved.scaled(max(0.0, 1.0 - leaked / rate)) if leaked > 0 else moved
            moves.append((sup, drawn, acc, received))
        for sup, drawn, _, _ in moves:
            sup.gas_draw(drawn)
        for _, _, acc, received in moves:
            acc.gas_receive(received)

        for tag, node in self.nodes.items():
            if node.domain == "gas":
                node.flow = 0.0
        for e in self.edges:
            if e.src.domain == "gas":
                e.src.flow -= e.q
            if e.dst.domain == "gas":
                e.dst.flow += e.q
