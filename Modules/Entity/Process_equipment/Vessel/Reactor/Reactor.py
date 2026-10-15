# Modules/Vessel/Reactor/reactor_base.py
from Modules.Entity.Process_equipment.Vessel.Vessel import Vessel

# =====================================================================
# Parent class: Vessel
# Class: Reactor (Handles temperatures and concentrations for reactors)
# CSTR, PFR
# =====================================================================

class Reactor(Vessel):
    """Subclass of vessel, parent of reactor types. Reactions change the composition and temperature of the contents."""
    def __init__(self, name, max_capacity, initial_moles=None, initial_temp=25.0,
                 inlets=None, outlets=None):
        super().__init__(name, max_capacity, initial_moles, initial_temp, inlets, outlets)