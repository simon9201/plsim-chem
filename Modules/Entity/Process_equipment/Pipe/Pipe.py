from Modules.Entity.Process_equipment.transfer import Transfer


class Pipe(Transfer):
    """Always open. blockage (0..1) is a fault that reduces the flow it allows."""
    def __init__(self, name, max_flow=50.0, blockage=0.0, inlets=None, outlets=None):
        super().__init__(name, max_flow, 1.0, inlets, outlets)
        self.blockage = blockage

    def flow_limit(self):
        return self.max_flow * (1.0 - min(1.0, max(0.0, self.blockage)))
