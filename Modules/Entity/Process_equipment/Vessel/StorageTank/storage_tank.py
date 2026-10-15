from Modules.Entity.Process_equipment.Vessel.Vessel import Vessel

# =====================================================================
# Parent class: Vessel
# Class: StorageTank
# `leak_rate` (L/s) is a fault hook: it drains liquid out of the plant.
# =====================================================================


class StorageTank(Vessel):
    def __init__(self, name, description="", status=1, inlets=None, outlets=None, leak_rate=0.0, **vessel_options):
        super().__init__(name, description, status, inlets, outlets, **vessel_options)
        self.leak_rate = leak_rate

    def update(self, dt):
        if self.leak_rate > 0:
            self.contents.remove_volume(self.leak_rate * dt)

    def get_state(self):
        state = super().get_state()
        state["leak_rate"] = float(self.leak_rate)
        return state

    def set_state(self, state):
        super().set_state(state)
        self.leak_rate = state["leak_rate"]
