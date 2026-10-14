from Modules.Entity.Entity import Entity

class process_equipment(Entity):
    def __init__(self,name, initial_temp, inlets, outlets):
        super().__init__(name, description = "test", status = 1)
        self.temperature = initial_temp     # °C
        self.inlets = inlets
        self.outlets = outlets