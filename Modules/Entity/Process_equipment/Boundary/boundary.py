from Modules.Entity.Process_equipment.process_equipment import ProcessEquipment, P_ATM
from Modules.Flow.flow import Material

# =====================================================================
# Parent class: ProcessEquipment
# Classes: Source (arriving goods: liquid enters the plant), Sink (liquid leaves the plant)
#
# Both sit at the edge of the model and have fixed pressure. A Source can
# only supply, a Sink can only accept. A source of an external chemical is
# defined by its composition (mole fractions), temperature and supply pressure.
# =====================================================================


class Source(ProcessEquipment):
    def __init__(self, name, description="", status=1, inlets=None, outlets=None,
                 composition=None, temperature=25.0, supply_pressure=2.0):
        super().__init__(name, description, status, [], outlets or ["out"])
        if not composition:
            raise ValueError(f"{name}: a Source needs a composition, e.g. {{'A': 0.2, 'water': 0.8}}")
        self.composition = dict(composition)
        self.temperature = temperature
        self.supply_pressure = supply_pressure
        self.supplied = Material({}, temperature)   # everything that has entered the plant here
        self.lot = ""                               # id of the delivery batch currently arriving

    @property
    def pressure(self):
        return self.supply_pressure

    @property
    def supplied_volume(self):
        return self.supplied.volume

    def port_pressure(self, port):
        return self.supply_pressure

    def can_supply(self, port):
        return True

    def stream(self, port):
        return Material.from_fractions(self.composition, 1.0, self.temperature)

    def draw(self, material):
        self.supplied.add(material)

    def set_lot(self, composition, lot_id=""):
        """Change what arrives (a new delivery batch). The composition is normalised by Material.from_fractions."""
        self.composition = dict(composition)
        self.lot = lot_id

    def get_state(self):
        return {"supplied": self.supplied.to_state(), "composition": dict(self.composition), "lot": self.lot}

    def set_state(self, state):
        self.supplied = Material.from_state(state["supplied"])
        self.composition = dict(state.get("composition", self.composition))
        self.lot = state.get("lot", self.lot)


class Sink(ProcessEquipment):
    def __init__(self, name, description="", status=1, inlets=None, outlets=None, sink_pressure=P_ATM):
        super().__init__(name, description, status, inlets or ["in"], [])
        self.sink_pressure = sink_pressure
        self.received = Material({}, 25.0)   # everything that has left the plant here

    @property
    def pressure(self):
        return self.sink_pressure

    @property
    def received_volume(self):
        return self.received.volume

    def port_pressure(self, port):
        return self.sink_pressure

    def can_accept(self, port):
        return True

    def receive(self, material):
        self.received.add(material)

    def get_state(self):
        return {"received": self.received.to_state()}

    def set_state(self, state):
        self.received = Material.from_state(state["received"])
