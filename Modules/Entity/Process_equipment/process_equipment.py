from Modules.Entity.Entity import Entity

# =====================================================================
# Parent class: Entity
# Class: ProcessEquipment (anything with ports that is part of the flow network)
# Subclasses: Vessel (tanks, reactors), Transfer (pump, valve, pipe), Junction (manifold, tee), Boundary (source, sink)
#
# Ports are named sockets. `inlets` and `outlets` are lists of port names and
# must match the Port rows in the database. A connection always joins an
# outlet of one piece of equipment to an inlet of another.
# =====================================================================
G = 9.81            # m/s2
P_ATM = 1.013       # bar


class ProcessEquipment(Entity):
    def __init__(self, name, description="", status=1, inlets=None, outlets=None):
        super().__init__(name, description, status)
        self.inlets = list(inlets or [])
        self.outlets = list(outlets or [])
        self.flow = 0.0     # L/s through the equipment during the last step

    # ---- what the flow solver asks of anything that is a node (not a Transfer) ----
    def port_pressure(self, port):
        """Pressure (bar) seen by a pipe attached to this port."""
        raise NotImplementedError(f"{self.name} cannot be a flow-network node")

    def can_supply(self, port):
        return False

    def can_accept(self, port):
        return False

    def max_outflow(self, dt):
        """L/s the equipment could deliver for one step in total."""
        return float("inf")

    def max_inflow(self, dt):
        """L/s the equipment could take in for one step in total."""
        return float("inf")

    @property
    def is_empty(self):
        return False
