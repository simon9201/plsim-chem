import numpy as np
from Modules.Entity.Process_equipment.process_equipment import process_equipment
# =====================================================================
# Parent class: Entity
# Class: Vessel (Handles general mass balance for vessels)
# Subclasses, Reactor, Storagetank
# =====================================================================
class Vessel(process_equipment):
    """Base class for all processcontainers that are accumulating volume/mass"""
    def __init__(self, name, max_capacity, initial_volume=0.0, initial_temp=25.0):
        super().__init__(name, initial_temp, inlets = 1, outlets = 1)
        self.max_capacity = max_capacity    # Liter
        self.volume = initial_volume        # Liter (Local state)
        self.sensor_noise_std = 0.05        # Standard deviation from noise
        
    @property
    def level_percent(self):
        """Calculates the percentual fill level based on current volume"""
        return (self.volume / self.max_capacity) * 100.0

    def update_mass_balance(self, dt, flow_in, flow_out):
        """Mass balance: dV = (F_in - F_out) * dt"""
        net_flow = flow_in - flow_out
        self.volume += net_flow * dt
        
        # Handles extremes conditions (Overflow and Empty)
        if self.volume > self.max_capacity:
            self.volume = self.max_capacity
        elif self.volume < 0.0:
            self.volume = 0.0

    def get_level_sensor(self):
        """General interface for level sensors with noise interference"""
        noise = np.random.normal(0, self.sensor_noise_std)
        raw_reading = self.level_percent + noise
        return max(0.0, min(100.0, raw_reading))
