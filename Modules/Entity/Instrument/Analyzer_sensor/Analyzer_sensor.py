from Modules.Entity.Instrument.instrument import Instrument


class AnalyzerSensor(Instrument):
    """Concentration of one component in a vessel (online analyzer), in mol/L."""
    variable = "contents"
    unit = "mol/L"
    default_range = (0.0, 20.0)

    def __init__(self, name, target, component, **options):
        super().__init__(name, target, **options)
        self.component = component

    def true_value(self):
        return self.target.concentration(self.component)
