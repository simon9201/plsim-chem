import json
from datetime import datetime

from sqlalchemy import select

from Data.Model.model import Plant, Snapshot, SnapshotState
from Modules.Editor.catalog import EQUIPMENT
from Modules.Editor.document import PlantDocument

# =====================================================================
# Exchange: move plant designs and saved plant states in and out of the database as plain JSON files, so they
# can be backed up, shared, or kept in version control. No GUI code here (the launcher and tests call it).
#   design    layout only: nodes (type, position, parameters, ports), connections and instruments
#   snapshot  running state only: the state of every piece of equipment plus plant-wide state
# A snapshot only makes sense for the design it was taken from (same equipment tags), so import it into
# that plant. Snapshot files hold equipment state, which includes hidden degradation values: treat them as
# simulator data, never as operator/AI-visible data.
# =====================================================================

DESIGN_FORMAT = "plsim-chem-design"
SNAPSHOT_FORMAT = "plsim-chem-snapshot"
VERSION = 1


def _check(data, fmt):
    if not isinstance(data, dict) or data.get("format") != fmt:
        raise ValueError(f"Not a {fmt} file")
    if data.get("version") != VERSION:
        raise ValueError(f"Unsupported {fmt} version {data.get('version')!r} (this build reads {VERSION})")


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- designs
def export_design(session, plant_name):
    doc = PlantDocument(session, plant_name)
    return {
        "format": DESIGN_FORMAT, "version": VERSION, "name": doc.name,
        "description": doc.plant.description or "",
        "nodes": [{"tag": n.tag, "type": n.type, "x": n.x, "y": n.y, "z": n.z, "params": n.params,
                   "inlets": n.inlets, "outlets": n.outlets} for n in doc.nodes()],
        "edges": [{"from": [e.from_tag, e.from_port], "to": [e.to_tag, e.to_port]} for e in doc.edges()],
        "probes": [{"tag": p.tag, "type": p.type, "target": p.target, "params": p.params} for p in doc.probes()],
    }


def import_design(session, data, name=None):
    """Create a new plant from an exported design and commit it. Returns the plant name.
    Refuses to overwrite an existing plant; everything is rolled back if the file is inconsistent."""
    _check(data, DESIGN_FORMAT)
    name = (name or data.get("name") or "").strip()
    if not name:
        raise ValueError("The design needs a plant name")
    created = False
    try:
        doc = PlantDocument.create(session, name, data.get("description", ""))
        created = True
        for n in data["nodes"]:
            if n["type"] not in EQUIPMENT:
                raise ValueError(f"Unknown equipment type '{n['type']}' for {n['tag']}")
            doc.add_node(n["type"], n["x"], n["y"], tag=n["tag"], params=n.get("params"), z=n.get("z", 0.0))
            doc.set_params(n["tag"], n.get("params") or {})      # exactly the saved parameters, no catalog defaults
            have = set(doc.node(n["tag"]).inlets) | set(doc.node(n["tag"]).outlets)
            for direction, ports in (("in", n.get("inlets", [])), ("out", n.get("outlets", []))):
                for port in ports:
                    if port not in have:
                        doc.add_port(n["tag"], direction, port)
        for e in data["edges"]:
            doc.connect(*e["from"], *e["to"])
        for p in data.get("probes", []):
            doc.add_probe(p["type"], p["target"], tag=p["tag"], params=p.get("params"))
        problems = doc.validate()
        if problems:
            raise ValueError("The imported plant does not build: " + "; ".join(problems))
        doc.save()
    except Exception:
        session.rollback()
        if created:     # some databases commit when tables are created, so make sure no half-built plant stays
            leftover = session.scalars(select(Plant).where(Plant.name == name)).first()
            if leftover is not None:
                session.delete(leftover)
                session.commit()
        raise
    return name


# ---------------------------------------------------------------- snapshots
def _snapshot_row(session, plant_name, name):
    plant = session.scalars(select(Plant).where(Plant.name == plant_name)).one()
    snap = next((s for s in plant.snapshots if s.name == name), None)
    if snap is None:
        raise ValueError(f"No snapshot '{name}'. Available: {[s.name for s in plant.snapshots]}")
    return plant, snap


def export_snapshot(session, plant_name, name):
    _, snap = _snapshot_row(session, plant_name, name)
    return {"format": SNAPSHOT_FORMAT, "version": VERSION, "plant": plant_name, "name": snap.name,
            "sim_time": snap.sim_time, "created_at": snap.created_at.isoformat(),
            "states": {s.entity_tag: s.state for s in snap.states}}


def import_snapshot(session, plant_name, data, name=None, overwrite=False):
    """Store a snapshot file under a plant. Equipment tags must exist in that plant. Returns the snapshot name."""
    _check(data, SNAPSHOT_FORMAT)
    plant = session.scalars(select(Plant).where(Plant.name == plant_name)).one()
    from Data.snapshots import PLANT_TAG
    known = {e.tag_name for e in plant.equipment} | {PLANT_TAG}
    unknown = sorted(set(data["states"]) - known)
    if unknown:
        raise ValueError(f"The snapshot does not match plant '{plant_name}': unknown equipment {unknown}")
    name = (name or data.get("name") or "").strip()
    if not name:
        raise ValueError("The snapshot needs a name")
    existing = next((s for s in plant.snapshots if s.name == name), None)
    if existing is not None:
        if not overwrite:
            raise ValueError(f"Snapshot '{name}' already exists")
        plant.snapshots.remove(existing)
        session.flush()
    try:
        created = datetime.fromisoformat(data["created_at"]) if data.get("created_at") else datetime.now()
    except ValueError:
        created = datetime.now()
    snap = Snapshot(name=name, sim_time=float(data["sim_time"]), created_at=created)
    snap.states = [SnapshotState(entity_tag=tag, state=state) for tag, state in data["states"].items()]
    plant.snapshots.append(snap)
    session.commit()
    return name


def delete_snapshot(session, plant_name, name):
    plant, snap = _snapshot_row(session, plant_name, name)
    plant.snapshots.remove(snap)
    session.commit()
