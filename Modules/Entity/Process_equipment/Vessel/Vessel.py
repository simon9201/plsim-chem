from Modules.Entity.Process_equipment.process_equipment import process_equipment
from Modules.Flow.flow import Material
# =====================================================================
# Parent class: process_equipment
# Class: Vessel (holds liquid: moles per component and a temperature)
# Subclasses, Reactor, Storagetank
# =====================================================================
class Vessel(process_equipment):
    """Base class for all process containers that accumulate liquid"""
    def __init__(self, name, max_capacity, initial_moles=None, initial_temp=25.0,
                 inlets=None, outlets=None):
        # contents must exist before the base class sets self.temperature
        self.contents = Material(initial_moles, initial_temp)
        super().__init__(name, initial_temp=initial_temp, inlets=inlets, outlets=outlets)
        self.max_capacity = max_capacity    # Liter
        if self.contents.volume > max_capacity:
            raise ValueError(f"{name}: initial contents ({self.contents.volume:.0f} L) exceed max_capacity")

    @property
    def temperature(self):
        return self.contents.temperature

    @temperature.setter
    def temperature(self, value):
        self.contents.temperature = value

    @property
    def volume(self):
        """Liter"""
        return self.contents.volume

    @property
    def level_percent(self):
        """Fill level based on current volume"""
        return (self.volume / self.max_capacity) * 100.0

    def concentration(self, component):
        """mol/L"""
        return self.contents.concentration(component)

    def max_outflow(self, dt):
        return self.volume / dt

    def max_inflow(self, dt):
        return max(0.0, self.max_capacity - self.volume) / dt

    def outlet_material(self, port, flow):
        return self.contents.scaled_to_volume(flow)

    def update(self, dt, inflows, outflow_rate):
        """Mass balance: take out what leaves (at current composition), then mix in what arrives."""
        self.contents.remove_volume(outflow_rate * dt)
        for rate in inflows:
            self.contents.add(rate.scaled(dt))
