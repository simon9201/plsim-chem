from Modules.Entity.Process_equipment.Vessel.Vessel import Vessel

# =====================================================================
# Parent class: Vessel
# Class: Reactor (a vessel where named reactions from the chemistry library take place)
# Subclasses: CSTR
# =====================================================================


class Reactor(Vessel):
    def __init__(self, name, description="", status=1, inlets=None, outlets=None, reactions=None, **vessel_options):
        super().__init__(name, description, status, inlets, outlets, **vessel_options)
        self.reaction_names = list(reactions or [])
        self.heat_released = 0.0    # W, from reactions during the last step (negative = endothermic)
