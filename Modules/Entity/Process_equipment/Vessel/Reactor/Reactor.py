# Modules/Vessel/Reactor/reactor_base.py
from Modules.Entity.Process_equipment.Vessel.Vessel import Vessel

# =====================================================================
# Parent class: Vessel
# Class: Reactor (Handles temperatures and concentrations for reactors)
# CSTR, PFR
# =====================================================================

class Reactor(Vessel):
    """Subclass of vessel, parent of reactor types, handles temperature and concerntrations"""
    def __init__(self, name, max_capacity, initial_volume=0.0):
        # Initiera den totala volymen via Vessel
        super().__init__(name, max_capacity, initial_volume)
        
        # Reaktorspecifika tillstånd
        self.concentration_A = 1.0            # mol/L (Reaktant)
        self.concentration_B = 0.0            # mol/L (Produkt)
        
    def get_temperature_sensor(self):
        """Temperaturgivare med mätbrus för PLC:n."""
        import numpy as np
        noise = np.random.normal(0, 0.05)
        return self.temperature + noise