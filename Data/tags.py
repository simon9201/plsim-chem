from dataclasses import dataclass

from sqlalchemy import select

from Data.Model.model import Plant, PlcTag

DEFAULT_GVL = "GVL_Sim"


@dataclass(frozen=True)
class TagMapping:
    entity_tag: str
    signal: str
    direction: str
    data_type: str
    plc_symbol: str


def sync_tags(session, plant_name, model):
    """Make the plc_tag rows match the signals the plant exposes.
    New signals get a default symbol (GVL_Sim.<tag>_<signal>), existing symbols you edited are kept,
    and rows for signals that no longer exist are removed."""
    plant = session.scalars(select(Plant).where(Plant.name == plant_name)).one()
    table = model.signal_table()
    existing = {(t.entity_tag, t.signal): t for t in plant.plc_tags}

    for key, row in existing.items():
        if key not in table:
            session.delete(row)
    for (tag, name), signal in table.items():
        if (tag, name) not in existing:
            plant.plc_tags.append(PlcTag(
                entity_tag=tag, signal=name, direction=signal.direction,
                data_type=signal.data_type, plc_symbol=f"{DEFAULT_GVL}.{tag}_{name}"))
        else:
            row = existing[(tag, name)]
            row.direction, row.data_type = signal.direction, signal.data_type
    session.commit()


def load_tags(session, plant_name):
    plant = session.scalars(select(Plant).where(Plant.name == plant_name)).one()
    return [TagMapping(t.entity_tag, t.signal, t.direction, t.data_type, t.plc_symbol)
            for t in sorted(plant.plc_tags, key=lambda t: t.id)]


def export_gvl(tags):
    """TwinCAT declarations (structured text) for every PLC symbol, one VAR_GLOBAL block per GVL."""
    groups = {}
    for t in tags:
        gvl, _, variable = t.plc_symbol.partition(".")
        if not variable:
            raise ValueError(f"PLC symbol '{t.plc_symbol}' must look like GVL_Name.Variable")
        groups.setdefault(gvl, []).append((variable, t))

    blocks = []
    for gvl, items in groups.items():
        lines = [f"// ---- {gvl} ----", "{attribute 'qualified_only'}", "VAR_GLOBAL"]
        for variable, t in items:
            arrow = "PLC reads (measurement/feedback)" if t.direction == "to_plc" else "PLC writes (command)"
            lines.append(f"    {variable} : {t.data_type};  // {t.entity_tag}.{t.signal}: {arrow}")
        lines.append("END_VAR")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"
