from Modules.Entity.Process_equipment.process_equipment import ProcessEquipment

# =====================================================================
# Parent class: ProcessEquipment
# Class: Transfer (one inlet, one outlet, no holdup)
# Subclasses: Valve, CheckValve, Pump, Pipe, Elbow
#
# A transfer element is a resistance in a line. Hydraulics are in bar and L/s:
#     pressure drop = loss_coefficient * flow * |flow|        (bar)
# A pump adds head_at_zero_flow (bar) on top. The plant solver chains the
# elements between two vessels/junctions and solves the flow of the chain.
# Subclasses only describe themselves through the methods below.
# =====================================================================


def k_to_resistance(k, diameter=0.05, open_fraction=1.0):
    """Turn a velocity-head loss coefficient K into bar/(L/s)^2 for water in a pipe of this diameter."""
    area = 3.141592653589793 * diameter ** 2 / 4.0 * max(open_fraction, 1e-6)
    return k * 5e-9 / area ** 2


class Transfer(ProcessEquipment):
    one_way = False     # True: flow can only go from inlet to outlet
    FAULTS = ("leak_rate",)

    def __init__(self, name, description="", status=1, inlets=None, outlets=None):
        inlets = inlets or ["in"]
        outlets = outlets or ["out"]
        if len(inlets) != 1 or len(outlets) != 1:
            raise ValueError(f"{name}: a {type(self).__name__} has exactly one inlet and one outlet")
        super().__init__(name, description, status, inlets, outlets)
        self.p_in = 1.013
        self.p_out = 1.013

    @property
    def pressure(self):
        return self.p_in

    @pressure.setter
    def pressure(self, value):
        self.p_in = value

    @property
    def differential_pressure(self):
        return self.p_in - self.p_out

    def is_blocked(self):
        return False

    def loss_coefficient(self):
        """bar/(L/s)^2"""
        return 0.0

    def head_at_zero_flow(self):
        """bar added by the element (pumps)"""
        return 0.0

    def advance(self, dt):
        """Move internal dynamics (valve travel, pump ramp) before the flow is solved."""

    def after_step(self, dt, starved, fluid=None):
        """Called after the flow is solved. `starved` = nothing available at the inlet side.
        `fluid` = the Material arriving at the inlet (None when nothing flows)."""

    def condition_liquid(self, material, rate, dt):
        """Change the liquid passing through (a heat exchanger heats or cools it). `rate` in L/s. Returns the material."""
        return material
