# See Flow_sensor.py: an instrument only names the variable it reads; Instrument does the rest.
from Modules.Entity.Instrument.instrument import Instrument


class LevelSensor(Instrument):
    """Fill level of a vessel."""
    variable = "level_percent"
    unit = "%"
    default_range = (0.0, 100.0)
