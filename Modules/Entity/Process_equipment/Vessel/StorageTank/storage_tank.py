from Modules.Entity.Process_equipment.Vessel.Vessel import Vessel

# =====================================================================
# Parent: Vessel
# Class StorageTank (Simple container with a leakage simulation)
# =====================================================================
class StorageTank(Vessel):
    """ A specific vessel that inherits mass balances but has unique error cases"""
    def __init__(self, name, max_capacity, initial_volume=0.0):
        super().__init__(name, max_capacity, initial_volume, initial_temp = 25)
        self.leak_rate = 0.0  # Liter/second (Used to simulate a pipebreak)

    def update(self, dt, flow_in, flow_out):
        # If a leak is present it will add to the out flow.
        actual_outflow = flow_out + self.leak_rate
        
        # Using the parent class for its mass balance function
        self.update_mass_balance(dt, flow_in, actual_outflow)