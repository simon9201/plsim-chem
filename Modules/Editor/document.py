from dataclasses import dataclass

from sqlalchemy import select

from Data.Model.model import IN, OUT, Base, Connection, Instrument, Plant, add_equipment, add_instrument
from Data.chemistry import seed_chemistry
from Modules.Editor.catalog import EQUIPMENT, INSTRUMENTS, port_is_gas

# =====================================================================
# PlantDocument: edit a stored plant. This is the whole "editor" as far as the database is concerned:
# nodes (equipment with a position), ports, connections between ports, and instruments on nodes.
# There is no GUI code here, so any front end (Tk, a web page, a script) can drive it, and it can be tested.
#
# Changes are flushed to the session but only written when you call save(); revert() throws them away.
# The position of a node is stored in its params as "_ui": {"x":.., "y":.., "z":..}; the loader ignores keys that
# start with an underscore. x and y are canvas units (METERS_PER_UNIT metres each), z is the elevation in metres;
# the loader turns them into equipment.location, and the elevation drives the static head in the hydraulics.
# =====================================================================


METERS_PER_UNIT = 0.05      # 100 canvas units = 5 m


@dataclass
class Node:
    tag: str
    type: str
    x: float
    y: float
    params: dict
    inlets: list
    outlets: list
    z: float = 0.0              # elevation in metres (3D editor); x and y are canvas units, see METERS_PER_UNIT


@dataclass
class Edge:
    from_tag: str
    from_port: str
    to_tag: str
    to_port: str


@dataclass
class Probe:
    tag: str
    type: str
    target: str
    params: dict


class PlantDocument:
    def __init__(self, session, plant_name):
        Base.metadata.create_all(session.get_bind())
        self.session = session
        self.plant = session.scalars(select(Plant).where(Plant.name == plant_name)).one()

    @classmethod
    def create(cls, session, name, description=""):
        Base.metadata.create_all(session.get_bind())
        if session.scalars(select(Plant).where(Plant.name == name)).first() is not None:
            raise ValueError(f"A plant called '{name}' already exists")
        seed_chemistry(session)             # components are needed to load any plant
        session.add(Plant(name=name, description=description))
        session.flush()
        return cls(session, name)

    @staticmethod
    def list_plants(session):
        Base.metadata.create_all(session.get_bind())
        return [p.name for p in session.scalars(select(Plant).order_by(Plant.name))]

    @property
    def name(self):
        return self.plant.name

    # ------------------------------------------------------------ reading
    def _row(self, tag):
        row = next((e for e in self.plant.equipment if e.tag_name == tag), None)
        if row is None:
            raise KeyError(f"No equipment '{tag}'")
        return row

    @staticmethod
    def _node(row):
        ui = row.params.get("_ui", {})
        return Node(row.tag_name, row.type, float(ui.get("x", 0.0)), float(ui.get("y", 0.0)),
                    {k: v for k, v in row.params.items() if not k.startswith("_")},
                    [p.name for p in row.ports if p.direction == IN],
                    [p.name for p in row.ports if p.direction == OUT], float(ui.get("z", 0.0)))

    def nodes(self):
        return [self._node(r) for r in sorted(self.plant.equipment, key=lambda r: r.id)]

    def node(self, tag):
        return self._node(self._row(tag))

    def edges(self):
        return [Edge(c.from_port.equipment.tag_name, c.from_port.name,
                     c.to_port.equipment.tag_name, c.to_port.name) for c in self.plant.connections]

    def probes(self):
        return [Probe(i.tag_name, i.type, i.target.tag_name,
                      {k: v for k, v in i.params.items() if not k.startswith("_")}) for i in self.plant.instruments]

    def free_ports(self, tag, direction):
        used = {(e.from_tag, e.from_port) for e in self.edges()} | {(e.to_tag, e.to_port) for e in self.edges()}
        want = IN if direction == "in" else OUT
        return [p.name for p in self._row(tag).ports if p.direction == want and (tag, p.name) not in used]

    # ------------------------------------------------------------ nodes
    def _next_tag(self, prefix):
        taken = {e.tag_name for e in self.plant.equipment} | {i.tag_name for i in self.plant.instruments}
        n = 101
        while f"{prefix}{n}" in taken:
            n += 1
        return f"{prefix}{n}"

    def add_node(self, type, x=0.0, y=0.0, tag=None, params=None, z=0.0):
        info = EQUIPMENT.get(type)
        if info is None:
            raise ValueError(f"Unknown equipment type '{type}'. Known: {sorted(EQUIPMENT)}")
        tag = tag or self._next_tag(info.prefix)
        if any(e.tag_name == tag for e in self.plant.equipment) or any(i.tag_name == tag for i in self.plant.instruments):
            raise ValueError(f"Tag '{tag}' is already used")
        merged = {**info.params, **(params or {}), "_ui": {"x": float(x), "y": float(y), "z": float(z)}}
        add_equipment(self.plant, tag, type, merged, info.inlets, info.outlets)
        self.session.flush()
        return tag

    def needs_layout(self):
        """True when nodes were never placed (several share one spot, e.g. a plant built by a script)."""
        spots = [(n.x, n.y) for n in self.nodes()]
        return len(spots) > 1 and len(set(spots)) < len(spots)

    def auto_layout(self, col_gap=150.0, row_gap=90.0):
        """Place nodes left to right by flow: sources in the first column, each node one column after its
        furthest upstream neighbour. Rows are ordered to keep connected nodes level. Elevation (z) is kept."""
        nodes = self.nodes()
        tags = [n.tag for n in nodes]
        preds = {t: [] for t in tags}
        for e in self.edges():
            preds[e.to_tag].append(e.from_tag)
        depth = {t: 0 for t in tags}
        for _ in range(len(tags)):          # longest-path relaxation; the pass limit also tolerates loops
            changed = False
            for t in tags:
                for p in preds[t]:
                    if depth[p] + 1 > depth[t] and depth[p] + 1 < len(tags):
                        depth[t], changed = depth[p] + 1, True
            if not changed:
                break
        columns = {}
        for t in tags:
            columns.setdefault(depth[t], []).append(t)
        row = {}
        for d in sorted(columns):
            members = columns[d]
            def key(t):
                above = [row[p] for p in preds[t] if p in row]
                return sum(above) / len(above) if above else float("inf")
            members.sort(key=lambda t: (key(t), tags.index(t)))
            taken = []
            for t in members:               # nearest free row to the upstream average, never two nodes in one spot
                want = round(key(t)) if key(t) != float("inf") else (max(taken) + 1 if taken else 0)
                while want in taken:
                    want += 1
                taken.append(want)
                row[t] = want
        for t in tags:
            self.move_node(t, 80.0 + depth[t] * col_gap, 60.0 + row[t] * row_gap)

    def move_node(self, tag, x, y, z=None):
        """Move a node. z (elevation, m) is kept when not given."""
        row = self._row(tag)
        old = row.params.get("_ui", {})
        new_z = float(old.get("z", 0.0) if z is None else z)
        row.params = {**row.params, "_ui": {"x": float(x), "y": float(y), "z": new_z}}   # reassign: JSON columns do not track mutation

    def set_params(self, tag, params):
        row = self._row(tag)
        keep = {k: v for k, v in row.params.items() if k.startswith("_")}
        row.params = {**keep, **params}

    def add_port(self, tag, direction, name=None):
        row = self._row(tag)
        base = "in" if direction == "in" else "out"
        taken = {p.name for p in row.ports}
        if name is None:
            n = 1
            while f"{base}{n}" in taken:
                n += 1
            name = f"{base}{n}"
        if name in taken:
            raise ValueError(f"{tag} already has a port '{name}'")
        from Data.Model.model import Port
        row.ports.append(Port(name=name, direction=IN if direction == "in" else OUT))
        self.session.flush()
        return name

    def delete_node(self, tag):
        row = self._row(tag)
        port_ids = {p.id for p in row.ports}
        for c in [c for c in self.plant.connections if c.from_port_id in port_ids or c.to_port_id in port_ids]:
            self.plant.connections.remove(c)
        for i in [i for i in self.plant.instruments if i.target_equipment_id == row.id]:
            self.plant.instruments.remove(i)
        self.session.flush()
        self.plant.equipment.remove(row)
        self.session.flush()

    # ------------------------------------------------------------ connections
    def connect(self, from_tag, from_port, to_tag, to_port):
        """Raises ValueError when the connection is not allowed."""
        src, dst = self._row(from_tag), self._row(to_tag)
        if src is dst:
            raise ValueError("Cannot connect equipment to itself")
        if port_is_gas(src.type, from_port) != port_is_gas(dst.type, to_port):
            raise ValueError("Cannot connect a gas port to a liquid port")
        from Data.Model.model import connect as db_connect
        used = {(e.from_tag, e.from_port) for e in self.edges()} | {(e.to_tag, e.to_port) for e in self.edges()}
        if (from_tag, from_port) in used or (to_tag, to_port) in used:
            raise ValueError("Port already connected")
        db_connect(self.plant, src, from_port, dst, to_port)
        self.session.flush()

    def connect_auto(self, from_tag, to_tag):
        """Join the first free compatible outlet of one node to the first free inlet of the other."""
        src, dst = self._row(from_tag), self._row(to_tag)
        for out in self.free_ports(from_tag, "out"):
            for inn in self.free_ports(to_tag, "in"):
                if port_is_gas(src.type, out) == port_is_gas(dst.type, inn):
                    self.connect(from_tag, out, to_tag, inn)
                    return Edge(from_tag, out, to_tag, inn)
        raise ValueError(f"No free matching ports between {from_tag} and {to_tag}")

    def disconnect(self, from_tag, from_port, to_tag, to_port):
        for c in list(self.plant.connections):
            if (c.from_port.equipment.tag_name, c.from_port.name, c.to_port.equipment.tag_name, c.to_port.name) == \
                    (from_tag, from_port, to_tag, to_port):
                self.plant.connections.remove(c)
                self.session.flush()
                return
        raise KeyError("No such connection")

    # ------------------------------------------------------------ instruments
    def add_probe(self, type, target_tag, tag=None, params=None):
        info = INSTRUMENTS.get(type)
        if info is None:
            raise ValueError(f"Unknown instrument type '{type}'. Known: {sorted(INSTRUMENTS)}")
        tag = tag or self._next_tag(info.prefix)
        if any(e.tag_name == tag for e in self.plant.equipment) or any(i.tag_name == tag for i in self.plant.instruments):
            raise ValueError(f"Tag '{tag}' is already used")
        add_instrument(self.plant, tag, type, self._row(target_tag), {**info.params, **(params or {})})
        self.session.flush()
        return tag

    def delete_probe(self, tag):
        row = next((i for i in self.plant.instruments if i.tag_name == tag), None)
        if row is None:
            raise KeyError(f"No instrument '{tag}'")
        self.plant.instruments.remove(row)
        self.session.flush()

    # ------------------------------------------------------------ checking and saving
    def validate(self):
        """Try to build the plant. Returns a list of problems (empty = it loads and takes a step)."""
        from Data.loader import load_plant
        try:
            self.session.flush()
            model = load_plant(self.session, self.plant.name)
            model.step(0.01)
        except Exception as err:        # any failure to build is a problem worth showing, not crashing on
            return [f"{type(err).__name__}: {err}"]
        return []

    def save(self):
        self.session.commit()

    def revert(self):
        self.session.rollback()
        self.plant = self.session.scalars(select(Plant).where(Plant.id == self.plant.id)).one()
