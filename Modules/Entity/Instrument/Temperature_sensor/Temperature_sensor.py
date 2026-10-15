from Modules.Entity.Instrument.instrument import Instrument


class TemperatureSensor(Instrument):
    """Temperature of the liquid in or through a piece of equipment."""
    variable = "temperature"
    unit = "C"
    default_range = (-50.0, 400.0)
