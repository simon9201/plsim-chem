from Modules.Entity.Process_equipment.transfer import Transfer


class Valve(Transfer):
    """Limits flow. command = opening 0..1."""
    def __init__(self, name, max_flow=5.0, command=0.0, inlets=None, outlets=None):
        super().__init__(name, max_flow, command, inlets, outlets)
