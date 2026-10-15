from Modules.Entity.Process_equipment.process_equipment import process_equipment
from Modules.Flow.flow import Material

# =====================================================================
# Parent class: process_equipment
# Classes: Source (material entering the plant), Sink (material leaving it)
# =====================================================================
class Source(process_equipment):
    """Unlimited supply with a fixed composition (mole fractions) and temperature."""
    def __init__(self, name, composition, temperature=25.0, inlets=None, outlets=None):
        super().__init__(name, initial_temp=temperature, inlets=inlets or [], outlets=outlets or ["out1"])
        Material.from_fractions(composition, 1.0)   # validates component names
        self.composition = dict(composition)
        self.supplied_volume = 0.0                  # L, total delivered

    def max_outflow(self, dt):
        return float("inf")

    def outlet_material(self, port, flow):
        return Material.from_fractions(self.composition, flow, self.temperature)

    def update(self, dt, inflows, outflow_rate):
        self.supplied_volume += outflow_rate * dt


class Sink(process_equipment):
    """Takes everything delivered to it and keeps a running total."""
    def __init__(self, name, inlets=None, outlets=None):
        super().__init__(name, inlets=inlets or ["in1"], outlets=outlets or [])
        self.received = Material()

    def max_inflow(self, dt):
        return float("inf")

    def update(self, dt, inflows, outflow_rate):
        for rate in inflows:
            self.received.add(rate.scaled(dt))
