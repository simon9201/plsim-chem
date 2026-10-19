import math

from Modules.Entity.Process_equipment.Junction.junction import Junction
from Modules.Entity.Process_equipment.process_equipment import ProcessEquipment, G
from Modules.Entity.Process_equipment.transfer import Transfer
from Modules.Entity.Process_equipment.Boundary.boundary import Source, Sink
from Modules.Entity.Process_equipment.Vessel.Vessel import Vessel
from Modules.Entity.Process_equipment.Pump.Pump import Pump
from Modules.Plant.environment import Environment
from Modules.Plant.gas_network import GasNetwork
from Modules.Plant.power_supply import PowerSupply
from Modules.Flow.flow import Material
from Modules.Lab.lab import Laboratory

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
#          pressure drop = a * Q * |Q| + static head   (a = sum of the element losses, pumps add a head b;
#          static head = density * g * rise in elevation along the line, so pumping uphill is harder and a
#          downhill line helps. Elevations are equipment.location[2], set from the editor)
#      Junction pressures are unknown, so they are found by iteration until
#      flow in = flow out at every junction.
#   3. liquid (moles per component + temperature) is moved along the edges
#   3b. gas moves through its own network (Modules/Plant/gas_network.py)
#   4. vessels and gas volumes run their own physics (leaks, precipitation, freezing, reactions, flashing)
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
        self.leaked = 0.0           # L/s lost along the line (set by record)
        self.static = 0.0           # bar of static head from src to dst (negative = downhill)
        self.statics = {}           # id(element) -> bar of static head across that element

    def end_resistance(self, node, port):
        return node.port_resistance(port) if isinstance(node, Junction) else 0.0

    def refresh(self):
        self.a = sum(e.loss_coefficient() for e in self.elements)
        self.a += self.end_resistance(self.src, self.src_port) + self.end_resistance(self.dst, self.dst_port)
        if not self.elements and self.a == 0.0:
            self.a = DIRECT_RESISTANCE
        self.a = max(self.a, MIN_RESISTANCE)
        self.b = sum(e.head_at_zero_flow() for e in self.elements)
        self.blocked = any(not e.status or e.is_blocked() for e in self.elements)
        self.one_way = any(e.one_way for e in self.elements)
        self._refresh_static()

    def _refresh_static(self):
        """Elevation change along the line: the points are src, each element, dst. Element i carries the climb
        from itself to the next point, so the pieces add up to z_dst - z_src."""
        points = [self.src, *self.elements, self.dst]
        z = [p.location[2] for p in points]
        if all(v == z[0] for v in z):
            self.static, self.statics = 0.0, {}
            return
        k = self._density() * G / 1e5
        self.static = k * (z[-1] - z[0])
        self.statics = {id(el): k * (z[i + 2] - z[i + 1]) for i, el in enumerate(self.elements)}
        if self.elements:       # whatever the first element sits above/below the source belongs to the first element
            first = self.elements[0]
            self.statics[id(first)] += k * (z[1] - z[0])

    def _density(self):
        """g/L of the liquid in the line, for the static head."""
        for node, port in ((self.src, self.src_port), (self.dst, self.dst_port)):
            if isinstance(node, Junction):
                if node.last_mix is not None and node.last_mix.volume > 0:
                    return node.last_mix.density
                continue
            if not hasattr(node, "stream"):     # a Sink holds nothing to take a density from
                continue
            d =  node.stream(port).density
            if d > 0:
                return d
        return 1000.0

    def flow(self):
        if self.blocked:
            return 0.0
        dp = self.src.port_pressure(self.src_port) - self.dst.port_pressure(self.dst_port) - self.static
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
            return el.loss_coefficient() * quad - el.head_at_zero_flow() + self.statics.get(id(el), 0.0)

        first_blocked = next((i for i, e in enumerate(self.elements) if not e.status or e.is_blocked()), None)
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
        # A leak removes liquid on the way: elements (and the node at the far end) see less flow past it.
        leaked = 0.0
        for el in (self.elements if q >= 0 else reversed(self.elements)):
            own = getattr(el, "effective_leak", el.leak_rate)
            el.flow = math.copysign(max(0.0, abs(q) - leaked - 0.5 * own), q) if q else 0.0
            leaked += own
        self.leaked = leaked

    @property
    def q_far(self):
        """Flow that arrives at the receiving end (q minus everything that leaked out on the way)."""
        if self.q == 0.0:
            return 0.0
        return self.q * max(0.0, 1.0 - self.leaked / abs(self.q))


class PlantModel:
    def __init__(self, name="plant"):
        self.name = name
        self.time = 0.0
        self.equipment = {}
        self.instruments = {}
        self._links = []        # (from tag, from port, to tag, to port)
        self._edges = []
        self._dirty = True
        # Slow processes (weather exchange, fouling, wear) run every slow_interval seconds of plant time.
        # slow_time_scale speeds them up (and the weather cycles) relative to the hydraulics.
        self.environment = Environment()
        self.power = PowerSupply()      # electrical network shared by all motors (sags, noise, dips)
        self.slow_interval = 10.0
        self.slow_time_scale = 1.0
        self.slow_time = 0.0
        self._slow_acc = 0.0
        self.maintenance_log = []       # (plant time, tag) for every repair; the health view reads it to re-baseline
        self._gas = GasNetwork(self)    # the gas side: GasVolume/Compressor/... (Modules/Plant/gas_network.py)
        self.lab = Laboratory(self)     # simulated off-line analysis (Modules/Lab/lab.py)

    @property
    def ambient_temperature(self):
        return self.environment.temperature(self.slow_time)

    def add_equipment(self, eq):
        if eq.name in self.equipment or eq.name in self.instruments:
            raise ValueError(f"Duplicate tag {eq.name}")
        self.equipment[eq.name] = eq
        if hasattr(eq, "motor"):
            eq.motor.supply = self.power
        self._dirty = True
        return eq

    def add_instrument(self, inst):
        if inst.name in self.equipment or inst.name in self.instruments:
            raise ValueError(f"Duplicate tag {inst.name}")
        self.instruments[inst.name] = inst
        return inst

    def connect(self, from_tag, from_port, to_tag, to_port):
        src, dst = self.equipment[from_tag], self.equipment[to_tag]
        if from_port not in list(getattr(src, "outlets", [])) + list(getattr(src, "gas_outlets", [])):
            raise ValueError(f"{from_tag} has no outlet '{from_port}'")
        if to_port not in list(getattr(dst, "inlets", [])) + list(getattr(dst, "gas_inlets", [])):
            raise ValueError(f"{to_tag} has no inlet '{to_port}'")
        if src.is_gas_port(from_port) != dst.is_gas_port(to_port):
            raise ValueError(f"Cannot connect a gas port to a liquid port: {from_tag}.{from_port} -> {to_tag}.{to_port}")
        for f, fp, t, tp in self._links:
            if (f, fp) == (from_tag, from_port) or (t, tp) == (to_tag, to_port):
                raise ValueError(f"Port already connected: {from_tag}.{from_port} -> {to_tag}.{to_port}")
        self._links.append((from_tag, from_port, to_tag, to_port))
        self._dirty = True

    def _build_edges(self):
        """Find the lines between nodes and check that the network makes sense."""
        out_map = {(f, fp): (t, tp) for f, fp, t, tp in self._links}
        in_map = {(t, tp) for _, _, t, tp in self._links}
        transfers = [eq for eq in self.equipment.values() if isinstance(eq, Transfer) and eq.domain == "liquid"]
        for tr in transfers:
            if (tr.name, tr.inlets[0]) not in in_map:
                raise ValueError(f"{tr.name}: inlet is not connected")
            if (tr.name, tr.outlets[0]) not in out_map:
                raise ValueError(f"{tr.name}: outlet is not connected")

        edges, used = [], set()
        for tag, node in self.equipment.items():
            if not isinstance(node, ProcessEquipment) or isinstance(node, Transfer) or node.domain == "gas":
                continue
            for port in node.outlets:
                if (tag, port) not in out_map:
                    continue
                elements, (cur, cur_port) = [], out_map[(tag, port)]
                path = set()
                while isinstance(self.equipment[cur], Transfer):
                    tr = self.equipment[cur]
                    if tr.name in path:
                        raise ValueError(f"Transfer-only cycle detected while tracing from {tag}.{port}: {tr.name}")
                    path.add(tr.name)
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
        self._gas.build()
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
        return node.max_outflow_at(port, dt) / n if node.status and node.can_supply(port) else 0.0

    @staticmethod
    def _in_cap(node, port, n, dt):
        return node.max_inflow_at(port, dt) / n if node.status and node.can_accept(port) else 0.0

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
                sup, sup_port, acc, acc_port = e.src, e.src_port, e.dst, e.dst_port
            else:
                sup, sup_port, acc, acc_port = e.dst, e.dst_port, e.src, e.src_port
            moved = self._supplied(sup, sup_port, cache, set()).scaled_to_volume(abs(e.q) * dt)
            for el in (e.elements if e.q > 0 else reversed(e.elements)):
                moved = el.condition_liquid(moved, abs(e.q), dt)
            leaked = sum(getattr(el, "effective_leak", el.leak_rate) for el in e.elements)
            received = moved.scaled(max(0.0, 1.0 - leaked / abs(e.q))) if leaked > 0 else moved
            moves.append((sup, sup_port, acc, acc_port, moved, received))
        for sup, sup_port, _, _, moved, _ in moves:
            if not isinstance(sup, Junction):
                sup.draw_at(sup_port, moved)
        for _, _, acc, acc_port, _, received in moves:
            if not isinstance(acc, Junction):
                acc.receive_at(acc_port, received)

    # ------------------------------------------------------------ time stepping
    def step(self, dt):
        if not math.isfinite(dt) or dt <= 0:
            raise ValueError("Simulation step dt must be a finite positive number")
        if not math.isfinite(self.slow_interval) or self.slow_interval <= 0:
            raise ValueError("slow_interval must be a finite positive number")
        if not math.isfinite(self.slow_time_scale) or self.slow_time_scale < 0:
            raise ValueError("slow_time_scale must be a finite non-negative number")
        if self._dirty:
            self._build_edges()
        self.power.update(self.time, dt)
        for eq in self.equipment.values():
            if hasattr(eq, "advance"):
                eq.advance(dt)
        self._refresh_edges(dt)
        self._solve_junctions()
        for e in self._edges:
            e.record()
            starved = e.q == 0 and e.src.is_empty
            fluid = self._supplied(e.src, e.src_port, {}, set()) if e.elements else None
            for el in e.elements:
                el.after_step(dt, starved, fluid)
        for node, attached in self._node_edges.items():
            eq = self.equipment[node]
            if isinstance(eq, (Source, Sink)):
                eq.flow = sum(abs(e.q_far if (r == "dst") == (e.q >= 0) else e.q) for e, r in attached)
            elif not isinstance(eq, Junction):
                eq.flow = sum((e.q_far if e.q >= 0 else e.q) if r == "dst" else -(e.q if e.q >= 0 else e.q_far)
                              for e, r in attached)
        self._move_material(dt)
        self._gas.step(dt)
        for eq in self.equipment.values():
            if hasattr(eq, "run_step"):     # vessels (leak, solids, freezing, reactions, flash) and gas volumes
                eq.run_step(dt)
        self._slow_acc += dt
        while self._slow_acc >= self.slow_interval:
            self._slow_acc -= self.slow_interval
            slow_dt = self.slow_interval * self.slow_time_scale
            self.slow_time += slow_dt
            ambient = self.ambient_temperature
            for eq in self.equipment.values():
                eq.slow_update(slow_dt, ambient)
        self.time += dt
        self.lab.update()
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

    # ------------------------------------------------------------ faults
    def set_fault(self, tag, name, value):
        """Inject (or clear, with 0/False) a fault on a piece of equipment, e.g. set_fault("TK-101", "leak_rate", 0.5).
        Each class lists the faults it supports in FAULTS."""
        eq = self.equipment[tag]
        faults = getattr(eq, "FAULTS", ())
        if name not in faults:
            raise ValueError(f"{tag} ({type(eq).__name__}) does not support fault '{name}'. Supported: {list(faults)}")
        if name == "stuck":
            if not isinstance(value, bool) and value not in (0, 1):
                raise ValueError(f"{tag}.{name} must be a boolean")
            value = bool(value)
        else:
            try:
                value = float(value)
            except (TypeError, ValueError) as err:
                raise ValueError(f"{tag}.{name} must be a finite number") from err
            if not math.isfinite(value):
                raise ValueError(f"{tag}.{name} must be a finite number")
            if name in ("blockage", "fouling", "wear", "fatigue") and not 0.0 <= value <= 1.0:
                raise ValueError(f"{tag}.{name} must be between 0 and 1")
            if name == "leak_rate" and value < 0.0:
                raise ValueError(f"{tag}.{name} cannot be negative")
        setattr(eq, name, value)

    # ------------------------------------------------------------ maintenance
    def repair(self, tag):
        """Maintenance on one piece of equipment: wear, fouling, ice, blockage, stuck actuators, leaks and
        settled solids are reset to like-new. Commands, trips (cleared by the PLC's reset) and the
        material inside are left alone. This is not "replace": the equipment keeps its identity and state."""
        self.equipment[tag].repair()
        self.maintenance_log.append((self.time, tag))

    def repair_all(self):
        for tag in self.equipment:
            self.repair(tag)

    # ------------------------------------------------------------ snapshots
    def get_state(self):
        equipment = {}
        for tag, eq in self.equipment.items():
            state = eq.get_state()
            if getattr(eq, "leak_rate", 0.0):
                state = {**state, "leak_rate": float(eq.leak_rate)}
            equipment[tag] = state
        return {"time": float(self.time), "slow_time": float(self.slow_time),
                "slow_acc": float(self._slow_acc), "slow_interval": float(self.slow_interval),
                "slow_time_scale": float(self.slow_time_scale),
                "environment": self.environment.get_state(), "power": self.power.get_state(), "equipment": equipment}

    def set_state(self, state):
        unknown = [t for t in state["equipment"] if t not in self.equipment]
        if unknown:
            raise ValueError(f"Snapshot has equipment that is not in this plant: {unknown}")
        for tag, eq_state in state["equipment"].items():
            eq = self.equipment[tag]
            if isinstance(eq, ProcessEquipment):
                eq.leak_rate = float(eq_state.get("leak_rate", 0.0))
            if eq_state:
                eq.set_state(eq_state)
        self.time = state["time"]
        self.slow_time = state.get("slow_time", 0.0)
        self._slow_acc = state.get("slow_acc", 0.0)
        self.slow_interval = state.get("slow_interval", self.slow_interval)
        self.slow_time_scale = state.get("slow_time_scale", self.slow_time_scale)
        self.environment.set_state(state.get("environment", {}))
        self.power.set_state(state.get("power", {}))
        self.sample_instruments()
