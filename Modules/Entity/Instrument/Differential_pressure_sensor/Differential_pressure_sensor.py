from Modules.Entity.Instrument.instrument import Instrument


class DifferentialPressureSensor(Instrument):
    """Pressure drop across a transfer element (valve, pipe, pump head shows as negative)."""
    variable = "differential_pressure"
    unit = "bar"
    default_range = (-1.0, 5.0)
