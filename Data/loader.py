from sqlalchemy import select

from Data.Model.model import IN, OUT, Plant
from Modules.Plant.plant_model import PlantModel
from Modules.Entity.Process_equipment.Vessel.StorageTank.storage_tank import StorageTank
from Modules.Entity.Process_equipment.Vessel.Reactor.CSTR.CSTR import ContinuousStirredTankReactor
from Modules.Entity.Process_equipment.Pump.Pump import Pump
from Modules.Entity.Process_equipment.Valve.Valve import Valve
from Modules.Entity.Process_equipment.Pipe.Pipe import Pipe
from Modules.Entity.Process_equipment.Boundary.boundary import Source, Sink
from Modules.Entity.Instrument.Flow_sensor.Flow_sensor import FlowSensor
from Modules.Entity.Instrument.Level_sensor.Level_sensor import LevelSensor
from Modules.Entity.Instrument.Pressure_sensor.Pressure_sensor import PressureSensor
from Modules.Entity.Instrument.Temperature_sensor.Temperature_sensor import TemperatureSensor

# Maps the `type` column in the database to the Python class that implements it.
EQUIPMENT_TYPES = {
    "storage_tank": StorageTank,
    "cstr": ContinuousStirredTankReactor,
    "pump": Pump,
    "valve": Valve,
    "pipe": Pipe,
    "source": Source,
    "sink": Sink,
}

INSTRUMENT_TYPES = {
    "flow_sensor": FlowSensor,
    "level_sensor": LevelSensor,
    "pressure_sensor": PressureSensor,
    "temperature_sensor": TemperatureSensor,
}


def _lookup(registry, type_name, tag):
    try:
        return registry[type_name]
    except KeyError:
        raise ValueError(f"{tag}: unknown type '{type_name}'. Known: {sorted(registry)}") from None


def load_plant(session, plant_name):
    """Build a runnable PlantModel from the rows of a stored plant."""
    plant = session.scalars(select(Plant).where(Plant.name == plant_name)).one()
    model = PlantModel(plant.name)

    for row in plant.equipment:
        cls = _lookup(EQUIPMENT_TYPES, row.type, row.tag_name)
        inlets = [p.name for p in row.ports if p.direction == IN]
        outlets = [p.name for p in row.ports if p.direction == OUT]
        model.add_equipment(cls(row.tag_name, inlets=inlets, outlets=outlets, **row.params))

    for c in plant.connections:
        model.connect(c.from_port.equipment.tag_name, c.from_port.name,
                      c.to_port.equipment.tag_name, c.to_port.name)

    for row in plant.instruments:
        cls = _lookup(INSTRUMENT_TYPES, row.type, row.tag_name)
        model.add_instrument(cls(row.tag_name, model.equipment[row.target.tag_name], **row.params))

    return model
