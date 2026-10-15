from Modules.Entity.Process_equipment.Vessel.Vessel import Vessel

# =====================================================================
# Parent: Vessel
# Class StorageTank (Simple container with a leakage simulation)
# =====================================================================
class StorageTank(Vessel):
    """ A specific vessel that inherits mass balances but has unique error cases"""
    def __init__(self, name, max_capacity, initial_moles=None, initial_temp=25.0,
                 inlets=None, outlets=None):
        super().__init__(name, max_capacity, initial_moles, initial_temp, inlets, outlets)
        self.leak_rate = 0.0  # Liter/second (Used to simulate a pipebreak)

    def update(self, dt, inflows, outflow_rate):
        # A leak drains the tank in addition to the normal outflow.
        super().update(dt, inflows, outflow_rate + self.leak_rate)