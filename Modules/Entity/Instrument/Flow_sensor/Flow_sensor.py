# Instruments are thin: they name which attribute of the target they read (`variable`), its unit and range;
# noise, lag, scaling to the raw 0..32767 PLC value and the signals live in Instrument.
from Modules.Entity.Instrument.instrument import Instrument


class FlowSensor(Instrument):
    """Volume flow through an in-line element (pump, valve, pipe)."""
    variable = "flow"
    unit = "L/s"
    default_range = (0.0, 10.0)
