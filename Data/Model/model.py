from sqlalchemy import CheckConstraint, ForeignKey, JSON, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# =====================================================================
# Database description of a plant. Only generic SQLAlchemy types are used
# so the same models work on SQLite (file) and a server database.
# These classes describe WHAT exists and HOW it is wired. The physics
# lives in Modules/ and is built from these rows by a loader.
# =====================================================================

IN = "in"
OUT = "out"


class Base(DeclarativeBase):
    pass


class Plant(Base):
    """One row per plant model (a sandbox you can load and run)."""
    __tablename__ = "plant"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str] = mapped_column(String(500), default="")

    equipment: Mapped[list["Equipment"]] = relationship(
        back_populates="plant", cascade="all, delete-orphan")
    connections: Mapped[list["Connection"]] = relationship(
        back_populates="plant", cascade="all, delete-orphan")
    instruments: Mapped[list["Instrument"]] = relationship(
        back_populates="plant", cascade="all, delete-orphan")
    plc_tags: Mapped[list["PlcTag"]] = relationship(
        back_populates="plant", cascade="all, delete-orphan")


class Equipment(Base):
    """A piece of process equipment (tank, valve, pump, manifold, ...)."""
    __tablename__ = "equipment"
    __table_args__ = (UniqueConstraint("plant_id", "tag_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    plant_id: Mapped[int] = mapped_column(ForeignKey("plant.id"))
    tag_name: Mapped[str] = mapped_column(String(50))   # e.g. "TK101"
    type: Mapped[str] = mapped_column(String(50))       # e.g. "storage_tank"
    params: Mapped[dict] = mapped_column(JSON, default=dict)  # e.g. {"max_capacity": 5000}

    plant: Mapped["Plant"] = relationship(back_populates="equipment")
    ports: Mapped[list["Port"]] = relationship(
        back_populates="equipment", cascade="all, delete-orphan", order_by="Port.id")


class Port(Base):
    """A socket on a piece of equipment: an inlet or an outlet."""
    __tablename__ = "port"
    __table_args__ = (
        UniqueConstraint("equipment_id", "name"),
        CheckConstraint("direction IN ('in', 'out')", name="port_direction"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    equipment_id: Mapped[int] = mapped_column(ForeignKey("equipment.id"))
    name: Mapped[str] = mapped_column(String(50))        # e.g. "in1", "out1"
    direction: Mapped[str] = mapped_column(String(3))    # "in" or "out"

    equipment: Mapped["Equipment"] = relationship(back_populates="ports")


class Connection(Base):
    """Joins an outlet port to an inlet port. A port can have at most one connection."""
    __tablename__ = "connection"

    id: Mapped[int] = mapped_column(primary_key=True)
    plant_id: Mapped[int] = mapped_column(ForeignKey("plant.id"))
    from_port_id: Mapped[int] = mapped_column(ForeignKey("port.id"), unique=True)
    to_port_id: Mapped[int] = mapped_column(ForeignKey("port.id"), unique=True)

    plant: Mapped["Plant"] = relationship(back_populates="connections")
    from_port: Mapped["Port"] = relationship(foreign_keys=[from_port_id])
    to_port: Mapped["Port"] = relationship(foreign_keys=[to_port_id])


class Instrument(Base):
    """A sensor observing one variable of one piece of equipment. It is not part of the flow graph."""
    __tablename__ = "instrument"
    __table_args__ = (UniqueConstraint("plant_id", "tag_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    plant_id: Mapped[int] = mapped_column(ForeignKey("plant.id"))
    tag_name: Mapped[str] = mapped_column(String(50))   # e.g. "LT101"
    type: Mapped[str] = mapped_column(String(50))       # e.g. "level_sensor"
    target_equipment_id: Mapped[int] = mapped_column(ForeignKey("equipment.id"))
    params: Mapped[dict] = mapped_column(JSON, default=dict)  # e.g. {"noise_std": 0.05}

    plant: Mapped["Plant"] = relationship(back_populates="instruments")
    target: Mapped["Equipment"] = relationship()


class PlcTag(Base):
    """Links one simulation signal to one PLC variable. Edit plc_symbol to match your own PLC program."""
    __tablename__ = "plc_tag"
    __table_args__ = (
        UniqueConstraint("plant_id", "entity_tag", "signal"),
        UniqueConstraint("plant_id", "plc_symbol"),
        CheckConstraint("direction IN ('to_plc', 'from_plc')", name="tag_direction"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    plant_id: Mapped[int] = mapped_column(ForeignKey("plant.id"))
    entity_tag: Mapped[str] = mapped_column(String(50))   # equipment or instrument tag, e.g. "P101"
    signal: Mapped[str] = mapped_column(String(50))       # e.g. "RunCmd"
    direction: Mapped[str] = mapped_column(String(10))    # "to_plc" (measurement) or "from_plc" (command)
    data_type: Mapped[str] = mapped_column(String(10))    # BOOL, INT or REAL
    plc_symbol: Mapped[str] = mapped_column(String(100))  # e.g. "GVL_Sim.P101_RunCmd"

    plant: Mapped["Plant"] = relationship(back_populates="plc_tags")


def add_equipment(plant, tag_name, type, params=None, inlets=(), outlets=()):
    """Create equipment with its named ports and add it to the plant."""
    eq = Equipment(tag_name=tag_name, type=type, params=params or {})
    eq.ports = [Port(name=n, direction=IN) for n in inlets] + \
               [Port(name=n, direction=OUT) for n in outlets]
    plant.equipment.append(eq)
    return eq


def add_instrument(plant, tag_name, type, target_equipment, params=None):
    """Create an instrument observing a piece of equipment and add it to the plant."""
    if target_equipment.plant is not plant:
        raise ValueError("The target equipment must belong to the plant")
    inst = Instrument(tag_name=tag_name, type=type, target=target_equipment, params=params or {})
    plant.instruments.append(inst)
    return inst


def connect(plant, from_equipment, from_port, to_equipment, to_port):
    """Connect an outlet to an inlet. Rules a database constraint cannot express are checked here."""
    out_port = next((p for p in from_equipment.ports if p.name == from_port), None)
    in_port = next((p for p in to_equipment.ports if p.name == to_port), None)
    if out_port is None or in_port is None:
        raise ValueError("Port not found")
    if out_port.direction != OUT or in_port.direction != IN:
        raise ValueError("A connection must go from an outlet to an inlet")
    if from_equipment.plant is not plant or to_equipment.plant is not plant:
        raise ValueError("Both pieces of equipment must belong to the plant")
    conn = Connection(from_port=out_port, to_port=in_port)
    plant.connections.append(conn)
    return conn
