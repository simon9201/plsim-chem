from sqlalchemy import select

from Data.Model.model import Plant, Snapshot, SnapshotState

# =====================================================================
# Snapshots: save the full running state of a plant to the database and
# restore it later. Only the state is stored; the plant layout comes from the
# plant tables, so load the same plant before restoring.
# =====================================================================


PLANT_TAG = "__plant__"     # snapshot row holding plant-wide state (slow clock, weather offset)


def _plant(session, plant_name):
    return session.scalars(select(Plant).where(Plant.name == plant_name)).one()


def save_snapshot(session, plant_name, model, name, overwrite=False):
    plant = _plant(session, plant_name)
    existing = next((s for s in plant.snapshots if s.name == name), None)
    if existing is not None:
        if not overwrite:
            raise ValueError(f"Snapshot '{name}' already exists (use overwrite=True)")
        plant.snapshots.remove(existing)
        session.flush()
    state = model.get_state()
    snap = Snapshot(name=name, sim_time=state["time"])
    snap.states = [SnapshotState(entity_tag=tag, state=s) for tag, s in state["equipment"].items()]
    plant_state = {key: value for key, value in state.items() if key != "equipment"}
    snap.states.append(SnapshotState(entity_tag=PLANT_TAG, state=plant_state))
    plant.snapshots.append(snap)
    session.commit()
    return snap


def load_snapshot(session, plant_name, model, name):
    plant = _plant(session, plant_name)
    snap = next((s for s in plant.snapshots if s.name == name), None)
    if snap is None:
        raise ValueError(f"No snapshot '{name}'. Available: {[s.name for s in plant.snapshots]}")
    states = {s.entity_tag: s.state for s in snap.states}
    plant_state = states.pop(PLANT_TAG, {})
    model.set_state({"time": snap.sim_time, "equipment": states, **plant_state})


def list_snapshots(session, plant_name):
    """[(name, sim_time, created_at)]"""
    return [(s.name, s.sim_time, s.created_at) for s in _plant(session, plant_name).snapshots]
