from Modules.Entity.Process_equipment.transfer import Transfer


class Pump(Transfer):
    """Drives flow. command = speed 0..1. Later: head/pressure, and compressors as a gas counterpart."""
    def __init__(self, name, max_flow=5.0, command=0.0, inlets=None, outlets=None):
        super().__init__(name, max_flow, command, inlets, outlets)
