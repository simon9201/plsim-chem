# See Flow_sensor.py: an instrument only names the variable it reads; Instrument does the rest.
from Modules.Entity.Instrument.instrument import Instrument


class PressureSensor(Instrument):
    """Pressure at a piece of equipment. A differential sensor (two targets) can be added later."""
    variable = "pressure"
    unit = "bar"
    default_range = (0.0, 10.0)
