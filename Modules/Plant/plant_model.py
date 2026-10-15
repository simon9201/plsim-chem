import math

from Modules.Entity.Process_equipment.Junction.junction import Junction
from Modules.Entity.Process_equipment.process_equipment import ProcessEquipment
from Modules.Entity.Process_equipment.transfer import Transfer
from Modules.Entity.Process_equipment.Boundary.boundary import Source, Sink
from Modules.Entity.Process_equipment.Vessel.Vessel import Vessel
from Modules.Flow.flow import Material

# =====================================================================
# PlantModel: the container that connects everything and advances time.
#
# The network is split in two kinds of things:
#   NODES  - everything that is not a Transfer: vessels, sources, sinks, junctions.
#   EDGES  - a line from one node outlet to one node inlet, made of the
#            Transfer elements (pump, valve, pipe, ...) connected in series between them.
#
# Each step:
#   1. actuators move (valve travel, pump ramp)
#   2. the flow of every edge is solved from the pressure at its two ends:
#          pressure drop = a * Q * |Q|   (a = sum of the element losses, pumps add a head b)
#      Junction pressures are unknown, so they are found by iteration until
#      flow in = flow out at every junction.
#   3. liquid (moles per component + temperature) is moved along the edges
#   4. vessels update themselves (reactions, cooling, leaks)
#   5. instruments take their measurements
# Units: bar, L/s, g, mol, J, degrees C.
# =====================================================================

DIRECT_RESISTANCE = 0.01    # bar/(L/s)^2 for a connection with no element at all
MIN_RESISTANCE = 1e-4


class Edge:
    def __init__(self, src_node, src_port, dst_node, dst_port, elements):
        self.src, self.src_port = src_node, src_port
        self.dst, self.dst_port = dst_node, dst_port
        self.elements = elements
        self.q = 0.0                # L/s, positive = from src to dst
        self.a = self.b = 0.0
        self.blocked = False
        self.one_way = False
        self.q_pos = self.q_neg = float("inf")

    def end_resistance(self, node, port):
        return node.port_resistance(port) if isinstance(node, Junction) else 0.0

    def refresh(self):
        self.a = sum(e.loss_coefficient() for e in self.elements)
        self.a += self.end_resistance(self.src, self.src_port) + self.end_resistance(self.dst, self.dst_port)
        if not self.elements and self.a == 0.0:
            self.a = DIRECT_RESISTANCE
        self.a = max(self.a, MIN_RESISTANCE)
        self.b = sum(e.head_at_zero_flow() for e in self.elements)
        self.blocked = any(e.is_blocked() for e in self.elements)
        self.one_way = any(e.one_way for e in self.elements)

    def flow(self):
        if self.blocked:
            return 0.0
        dp = self.src.port_pressure(self.src_port) - self.dst.port_pressure(self.dst_port)
        if self.b > 0:
            x = dp + self.b
            q = math.sqrt(x / self.a) if x > 0 else 0.0
        else:
            q = math.copysign(math.sqrt(abs(dp) / self.a), dp)
        if self.one_way and q < 0:
            q = 0.0
        return min(q, self.q_pos) if q > 0 else max(q, -self.q_neg)

    def record(self):
        """Store the flow and the pressure at both sides of every element."""
        q = self.q
        quad = q * abs(q)
        p_src = self.src.port_pressure(self.src_port) - self.end_resistance(self.src, self.src_port) * quad
        p_dst = self.dst.port_pressure(self.dst_port) + self.end_resistance(self.dst, self.dst_port) * quad

        def drop(el):
            return el.loss_coefficient() * quad - el.head_at_zero_flow()

        first_blocked = next((i for i, e in enumerate(self.elements) if e.is_blocked()), None)
        stop = len(self.elements) if (q != 0 or first_blocked is None) else first_blocked
        p = p_src
        for el in self.elements[:stop]:
            el.p_in, el.p_out = p, p - drop(el)
            p = el.p_out
        if stop < len(self.elements):
            # Stopped line: pressure upstream of the first blocked element follows the source,
            # everything from there on follows the destination.
            blocked_el = self.elements[stop]
            p_block_in = p
            p = p_dst
            for el in reversed(self.elements[stop:]):
                el.p_out, el.p_in = p, p + drop(el)
                p = el.p_in
            blocked_el.p_in = p_block_in
        for el in self.elements:
            el.flow = q


class PlantModel:
    def __init__(self, name="plant"):
        self.name = name
        self.time = 0.0
        self.equipment = {}
        self.instruments = {}
        self._links = []        # (from tag, from port, to tag, to port)
        self._edges = []
        self._dirty = True

    # ------------------------------------------------------------ building
    def add_equipment(self, eq):
        if eq.name in self.equipment or eq.name in self.instruments:
            raise ValueError(f"Duplicate tag {eq.name}")
        self.equipment[eq.name] = eq
        self._dirty = True
        return eq

    def add_instrument(self, inst):
        if inst.name in self.equipment or inst.name in self.instruments:
            raise ValueError(f"Duplicate tag {inst.name}")
        self.instruments[inst.name] = inst
        return inst

    def connect(self, from_tag, from_port, to_tag, to_port):
        src, dst = self.equipment[from_tag], self.equipment[to_tag]
        if from_port not in getattr(src, "outlets", []):
            raise ValueError(f"{from_tag} has no outlet '{from_port}'")
        if to_port not in getattr(dst, "inlets", []):
            raise ValueError(f"{to_tag} has no inlet '{to_port}'")
        for f, fp, t, tp in self._links:
            if (f, fp) == (from_tag, from_port) or (t, tp) == (to_tag, to_port):
                raise ValueError(f"Port already connected: {from_tag}.{from_port} -> {to_tag}.{to_port}")
        self._links.append((from_tag, from_port, to_tag, to_port))
        self._dirty = True

    def _build_edges(self):
        """Find the lines between nodes and check that the network makes sense."""
        out_map = {(f, fp): (t, tp) for f, fp, t, tp in self._links}
        in_map = {(t, tp) for _, _, t, tp in self._links}
        transfers = [eq for eq in self.equipment.values() if isinstance(eq, Transfer)]
        for tr in transfers:
            if (tr.name, tr.inlets[0]) not in in_map:
                raise ValueError(f"{tr.name}: inlet is not connected")
            if (tr.name, tr.outlets[0]) not in out_map:
                raise ValueError(f"{tr.name}: outlet is not connected")

        edges, used = [], set()
        for tag, node in self.equipment.items():
            if not isinstance(node, ProcessEquipment) or isinstance(node, Transfer):
                continue
            for port in node.outlets:
                if (tag, port) not in out_map:
                    continue
                elements, (cur, cur_port) = [], out_map[(tag, port)]
                while isinstance(self.equipment[cur], Transfer):
                    tr = self.equipment[cur]
                    elements.append(tr)
                    used.add(cur)
                    cur, cur_port = out_map[(cur, tr.outlets[0])]
                edges.append(Edge(node, port, self.equipment[cur], cur_port, elements))
        loose = [t.name for t in transfers if t.name not in used]
        if loose:
            raise ValueError(f"Pipe/valve/pump not connected between two vessels or junctions: {loose}")
        self._edges = edges
        self._node_edges = {}
        for e in edges:
            self._node_edges.setdefault(e.src.name, []).append((e, "src"))
            self._node_edges.setdefault(e.dst.name, []).append((e, "dst"))
        self._dirty = False

    # ------------------------------------------------------------ solving
    def _refresh_edges(self, dt):
        for e in self._edges:
            e.refresh()
            n_src = len(self._node_edges[e.src.name])
            n_dst = len(self._node_edges[e.dst.name])
            e.q_pos = min(self._out_cap(e.src, e.src_port, n_src, dt), self._in_cap(e.dst, e.dst_port, n_dst, dt))
            e.q_neg = min(self._out_cap(e.dst, e.dst_port, n_dst, dt), self._in_cap(e.src, e.src_port, n_src, dt))

    @staticmethod
    def _out_cap(node, port, n, dt):
        return node.max_outflow(dt) / n if node.can_supply(port) else 0.0

    @staticmethod
    def _in_cap(node, port, n, dt):
        return node.max_inflow(dt) / n if node.can_accept(port) else 0.0

    def _net_inflow(self, junction):
        total = 0.0
        for e, role in self._node_edges.get(junction.name, []):
            total += e.flow() if role == "dst" else -e.flow()
        return total

    def _solve_junctions(self):
        junctions = [eq for eq in self.equipment.values() if isinstance(eq, Junction)]
        for _ in range(200):
            change = 0.0
            for j in junctions:
                old = j.pressure
                lo, hi = -10.0, 500.0
                j.pressure = lo
                if self._net_inflow(j) <= 0:
                    j.pressure = old
                    continue
                j.pressure = hi
                if self._net_inflow(j) > 1e-12:
                    continue
                for _ in range(60):
                    j.pressure = 0.5 * (lo + hi)
                    if self._net_inflow(j) > 0:
                        lo = j.pressure
                    else:
                        hi = j.pressure
                j.pressure = 0.5 * (lo + hi)
                change = max(change, abs(j.pressure - old))
            if change < 1e-10:
                break
        for e in self._edges:
            e.q = e.flow()

    # ------------------------------------------------------------ material
    def _junction_mix(self, junction, cache, active):
        if junction.name in cache:
            return cache[junction.name]
        if junction.name in active:     # a loop of junctions: use what it held last step
            return junction.last_mix or Material({}, 25.0)
        active.add(junction.name)
        mix = Material({}, 25.0)
        for e, role in self._node_edges.get(junction.name, []):
            if role == "dst" and e.q > 0:
                node, port, rate = e.src, e.src_port, e.q
            elif role == "src" and e.q < 0:
                node, port, rate = e.dst, e.dst_port, -e.q
            else:
                continue
            mix.add(self._supplied(node, port, cache, active).scaled_to_volume(rate))
        active.discard(junction.name)
        cache[junction.name] = mix
        if mix.volume > 0:
            junction.last_mix = mix.copy()
        return mix

    def _supplied(self, node, port, cache, active):
        if isinstance(node, Junction):
            return self._junction_mix(node, cache, active)
        return node.stream(port)

    def _move_material(self, dt):
        cache, moves = {}, []
        for e in self._edges:
            if abs(e.q) * dt < 1e-12:
                continue
            if e.q > 0:
                sup, sup_port, acc = e.src, e.src_port, e.dst
            else:
                sup, sup_port, acc = e.dst, e.dst_port, e.src
            moved = self._supplied(sup, sup_port, cache, set()).scaled_to_volume(abs(e.q) * dt)
            moves.append((sup, acc, moved))
        for sup, _, moved in moves:
            if not isinstance(sup, Junction):
                sup.draw(moved)
        for _, acc, moved in moves:
            if not isinstance(acc, Junction):
                acc.receive(moved)

    # ------------------------------------------------------------ time stepping
    def step(self, dt):
        if self._dirty:
            self._build_edges()
        for eq in self.equipment.values():
            if hasattr(eq, "advance"):
                eq.advance(dt)
        self._refresh_edges(dt)
        self._solve_junctions()
        for e in self._edges:
            e.record()
            starved = e.q == 0 and e.src.is_empty
            for el in e.elements:
                el.after_step(dt, starved)
        for node, attached in self._node_edges.items():
            eq = self.equipment[node]
            if isinstance(eq, (Source, Sink)):
                eq.flow = sum(abs(e.q) for e, _ in attached)
            elif not isinstance(eq, Junction):
                eq.flow = sum(e.q if r == "dst" else -e.q for e, r in attached)
        self._move_material(dt)
        for eq in self.equipment.values():
            if isinstance(eq, Vessel):
                eq.update(dt)
        self.time += dt
        self.sample_instruments()

    def sample_instruments(self):
        for inst in self.instruments.values():
            inst.sample()

    # ------------------------------------------------------------ PLC interface
    def entities(self):
        return list(self.equipment.values()) + list(self.instruments.values())

    def signal_table(self):
        """{(entity tag, signal name): Signal} for everything the PLC can see or do."""
        return {(ent.name, s.name): s for ent in self.entities() for s in ent.signals()}

    def apply_failsafe(self):
        for ent in self.entities():
            ent.apply_failsafe()

    # ------------------------------------------------------------ snapshots
    def get_state(self):
        return {"time": float(self.time),
                "equipment": {tag: eq.get_state() for tag, eq in self.equipment.items()}}

    def set_state(self, state):
        unknown = [t for t in state["equipment"] if t not in self.equipment]
        if unknown:
            raise ValueError(f"Snapshot has equipment that is not in this plant: {unknown}")
        for tag, eq_state in state["equipment"].items():
            if eq_state:
                self.equipment[tag].set_state(eq_state)
        self.time = state["time"]
        self.sample_instruments()
