from Modules.Entity.Entity import Entity
# =====================================================================
# Parent class: Entity
# Class: process_equipment (anything the process liquid flows through)
# Subclasses: Vessel, Transfer (Pump, Valve, Pipe), Source, Sink
#
# inlets/outlets are lists of port names (same names as in the database).
# The PlantModel connects the ports and calls the "node" methods below.
# Nodes are equipment where material accumulates or enters/leaves the plant.
# =====================================================================
class process_equipment(Entity):
    def __init__(self, name, initial_temp=25.0, inlets=None, outlets=None, description=""):
        super().__init__(name, description=description, status=1)
        self.temperature = initial_temp     # C
        self.pressure = 1.013               # bar(a), static for now (hook for pressure sensors)
        self.inlets = list(inlets) if inlets is not None else []
        self.outlets = list(outlets) if outlets is not None else []

    def max_outflow(self, dt):
        """Largest volume flow (L/s) this node can supply during one step."""
        return 0.0

    def max_inflow(self, dt):
        """Largest volume flow (L/s) this node can accept during one step."""
        return 0.0

    def outlet_material(self, port, flow):
        """Material rate (mol/s) leaving through a port at a given volume flow (L/s)."""
        raise NotImplementedError(f"{self.name} cannot supply material")

    def update(self, dt, inflows, outflow_rate):
        """Advance one step. inflows: list of Material rates (mol/s), outflow_rate: total L/s leaving."""
        pass