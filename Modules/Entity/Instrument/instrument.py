import numpy as np
from Modules.Entity.Entity import Entity

# =====================================================================
# Parent class: Entity
# Class: Instrument (observes ONE variable of ONE piece of equipment)
# Subclasses: FlowSensor, LevelSensor, PressureSensor, TemperatureSensor
#
# Instruments are not part of the flow graph. They hold a reference to
# their target and add measurement noise and range limits to the true value.
# =====================================================================
class Instrument(Entity):
    variable = None     # attribute read on the target, set by subclasses
    unit = ""
    default_range = (None, None)

    def __init__(self, name, target, noise_std=0.0, range_min=None, range_max=None, description=""):
        super().__init__(name, description, status=1)
        if not hasattr(target, self.variable):
            raise ValueError(f"{name}: {type(self).__name__} cannot measure '{self.variable}' on {target.name}")
        self.target = target
        self.noise_std = noise_std
        self.range_min = self.default_range[0] if range_min is None else range_min
        self.range_max = self.default_range[1] if range_max is None else range_max
        self._rng = np.random.default_rng()

    def true_value(self):
        return getattr(self.target, self.variable)

    def read(self):
        """The measured value: true value plus noise, limited to the instrument range."""
        value = self.true_value()
        if self.noise_std:
            value += self._rng.normal(0.0, self.noise_std)
        if self.range_min is not None:
            value = max(self.range_min, value)
        if self.range_max is not None:
            value = min(self.range_max, value)
        return value
