from Modules.Entity.Process_equipment.process_equipment import ProcessEquipment
from Modules.Entity.Process_equipment.transfer import k_to_resistance

# =====================================================================
# Parent class: ProcessEquipment
# Class: Junction (liquid joins/splits here, no holdup)
# Subclasses: Manifold, Tee
#
# The junction pressure is unknown and found by the plant solver so that
# everything flowing in equals everything flowing out. Every port has a loss
# coefficient K (`port_ks`), added to the line attached to that port.
# The mixed stream leaving a junction is computed by the plant.
# =====================================================================


class Junction(ProcessEquipment):
    def __init__(self, name, description="", status=1, inlets=None, outlets=None,
                 diameter=0.05, port_ks=None, default_k=0.5):
        if not (inlets or []) or not (outlets or []):
            raise ValueError(f"{name}: needs at least one inlet and one outlet")
        super().__init__(name, description, status, inlets, outlets)
        self.diameter = diameter
        self.port_ks = {p: default_k for p in self.inlets + self.outlets}
        self.port_ks.update(port_ks or {})
        self.pressure = 1.013
        self.last_mix = None

    def port_pressure(self, port):
        return self.pressure

    def can_supply(self, port):
        return True

    def can_accept(self, port):
        return True

    def port_resistance(self, port):
        return k_to_resistance(self.port_ks[port], self.diameter)

    @property
    def temperature(self):
        return self.last_mix.temperature if self.last_mix else 25.0

    def get_state(self):
        return {}


class Manifold(Junction):
    """Any number of inlets and outlets on one header, for example 3 inlets and 2 outlets."""


class Tee(Manifold):
    """Three ports. Two of them are the straight run (low loss), the third is the branch (high loss).
    With 2 inlets + 1 outlet (a merge) the outlet and the first inlet are the run.
    With 1 inlet + 2 outlets (a split) the inlet and the first outlet are the run."""

    def __init__(self, name, description="", status=1, inlets=None, outlets=None,
                 run_k=0.2, branch_k=1.0, **options):
        inlets, outlets = list(inlets or []), list(outlets or [])
        if len(inlets) + len(outlets) != 3:
            raise ValueError(f"{name}: a Tee has exactly 3 ports")
        single, pair = (outlets, inlets) if len(outlets) == 1 else (inlets, outlets)
        if len(single) != 1:
            raise ValueError(f"{name}: a Tee needs 2 inlets + 1 outlet or 1 inlet + 2 outlets")
        port_ks = {single[0]: run_k, pair[0]: run_k, pair[1]: branch_k}
        port_ks.update(options.pop("port_ks", None) or {})
        super().__init__(name, description, status, inlets, outlets, port_ks=port_ks, **options)
