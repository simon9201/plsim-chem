from Modules.Entity.Instrument.instrument import Instrument


class FlowSensor(Instrument):
    """Volume flow through an in-line element (pump, valve, pipe)."""
    variable = "flow"
    unit = "L/s"
    default_range = (0.0, None)
