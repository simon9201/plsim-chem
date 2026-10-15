from Modules.Entity.Process_equipment.process_equipment import process_equipment

# =====================================================================
# Parent class: process_equipment
# Class: Transfer (in-line element with one inlet and one outlet)
# Subclasses: Pump, Valve, Pipe
#
# Flow model: each transfer element offers a maximum flow. The plant solver
# lets the most restrictive element in a line set the flow, limited by what
# the upstream vessel holds and the downstream vessel has room for.
# This abstracts a pressure-driven network; a pressure model can replace
# flow_limit() later.
# =====================================================================
class Transfer(process_equipment):
    def __init__(self, name, max_flow, command=0.0, inlets=None, outlets=None):
        super().__init__(name, inlets=inlets or ["in1"], outlets=outlets or ["out1"])
        self.max_flow = max_flow    # L/s at command = 1
        self.command = command      # 0..1 (pump speed or valve opening), set by the PLC
        self.flow = 0.0             # L/s, set by the plant solver every step

    def flow_limit(self):
        """Largest flow (L/s) this element allows right now."""
        return self.max_flow * min(1.0, max(0.0, self.command))
