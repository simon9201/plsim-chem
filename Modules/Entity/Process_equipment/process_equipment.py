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
    # Fault hooks the plant may set at run time (PlantModel.set_fault). Subclasses list what they support.
    # leak_rate (L/s, mol/s for gas equipment): material lost to the outside. On a vessel it drains the holdup,
    # on a line element it removes material from the flow passing through.
    FAULTS = ()
    leak_rate = 0.0
    # "liquid" or "gas": which network the equipment's own ports belong to. A vessel that also has a vapor
    # space (FlashDrum) stays "liquid" and lists its gas ports separately in gas_inlets / gas_outlets.
    domain = "liquid"

    def __init__(self, name, description="", status=1, inlets=None, outlets=None):
        super().__init__(name, description, status)
        self.inlets = list(inlets or [])
        self.outlets = list(outlets or [])
        self.gas_inlets = []
        self.gas_outlets = []
        self.flow = 0.0     # L/s through the equipment during the last step (mol/s for gas equipment)
        # Location in the plant in metres (x, y horizontal, z = elevation of the equipment's base/centreline).
        # The loader sets it from the editor. Only z changes the hydraulics (static head); x, y give pipe lengths.
        self.location = (0.0, 0.0, 0.0)

    def split_gas_ports(self):
        """Mixed liquid/gas equipment: ports named 'gas...' or 'vapor...' move to gas_inlets / gas_outlets."""
        def is_gas(p):
            return p.lower().startswith(("gas", "vapor", "vapour"))
        self.gas_inlets = [p for p in self.inlets if is_gas(p)]
        self.gas_outlets = [p for p in self.outlets if is_gas(p)]
        self.inlets = [p for p in self.inlets if not is_gas(p)]
        self.outlets = [p for p in self.outlets if not is_gas(p)]

    def is_gas_port(self, port):
        return self.domain == "gas" or port in self.gas_inlets or port in self.gas_outlets

    def repair(self):
        """Restore the equipment's condition to like-new (maintenance work). Subclasses reset their own
        wear, fouling, stuck parts etc. and call this. Operator state (commands, trips, status) is left alone."""
        self.leak_rate = 0.0

    # ---- what the flow solver asks of anything that is a node (not a Transfer) ----
    def port_pressure(self, port):
        """Pressure (bar) seen by a pipe attached to this port."""
        raise NotImplementedError(f"{self.name} cannot be a flow-network node")

    def slow_update(self, dt, ambient):
        pass

    def draw_at(self, port, material):
        """Liquid leaves through port. Equipment with several separate holdups (a column's sump and reflux drum) overrides this."""
        self.draw(material)

    def receive_at(self, port, material):
        self.receive(material)

    def can_supply(self, port):
        return False

    def can_accept(self, port):
        return False

    def max_outflow_at(self, port, dt):
        return self.max_outflow(dt)

    def max_inflow_at(self, port, dt):
        return self.max_inflow(dt)

    def max_outflow(self, dt):
        """L/s the equipment could deliver for one step in total."""
        return float("inf")

    def max_inflow(self, dt):
        """L/s the equipment could take in for one step in total."""
        return float("inf")

    @property
    def is_empty(self):
        return False
