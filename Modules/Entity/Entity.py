# =====================================================================
# Parent class: Entity
# Class: Vessel (Handles general mass balance for vessels)
# Subclasses: process_equipment, Instruments
# =====================================================================
class Entity:
    def __init__(self, name, description, status):
        self.name = name
        self.description = description
        self.status = status


    def to_plc_dict(self):
        return

    def update_from_plc(self):
        return
