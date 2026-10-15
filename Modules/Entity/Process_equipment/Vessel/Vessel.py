from Modules.Entity.Process_equipment.process_equipment import ProcessEquipment, G, P_ATM
from Modules.Flow.flow import Material, COMPONENTS

# =====================================================================
# Parent class: ProcessEquipment
# Class: Vessel (holds liquid: a Material of moles per component + temperature)
# Subclasses: StorageTank, Reactor -> CSTR
#
# Inlets enter at the top (headspace pressure). Outlets leave at the bottom
# (headspace pressure + the pressure of the liquid column), so a tank can
# drain by gravity. A vessel can supply liquid only from its outlets, but can
# accept liquid at any port. `pressure` is the bottom pressure (what a level
# transmitter or bottom pressure transmitter sees).
# =====================================================================


class Vessel(ProcessEquipment):
    def __init__(self, name, description="", status=1, inlets=None, outlets=None,
                 max_capacity=1000.0, height=3.0, headspace_pressure=P_ATM,
                 initial_volume=0.0, composition=None, temperature=25.0):
        super().__init__(name, description, status, inlets, outlets)
        self.max_capacity = max_capacity        # L
        self.height = height                    # m
        self.headspace_pressure = headspace_pressure
        if initial_volume > 0:
            if not composition:
                raise ValueError(f"{name}: give a composition for the initial volume")
            self.contents = Material.from_fractions(composition, initial_volume, temperature)
        else:
            self.contents = Material({}, temperature)

    @property
    def volume(self):
        return self.contents.volume

    @property
    def level_percent(self):
        return 100.0 * self.volume / self.max_capacity

    @property
    def level(self):
        """m"""
        return self.height * self.volume / self.max_capacity

    @property
    def temperature(self):
        return self.contents.temperature

    @temperature.setter
    def temperature(self, value):
        self.contents.temperature = value

    @property
    def hydrostatic_pressure(self):
        return self.contents.density * G * self.level / 1e5

    @property
    def pressure(self):
        """Bottom pressure, bar"""
        return self.headspace_pressure + self.hydrostatic_pressure

    @pressure.setter
    def pressure(self, value):
        self.headspace_pressure = value

    def concentration(self, component):
        return self.contents.concentration(component)

    # ---- flow-network node protocol ----
    def port_pressure(self, port):
        return self.pressure if port in self.outlets else self.headspace_pressure

    def can_supply(self, port):
        return port in self.outlets

    def can_accept(self, port):
        return True

    def max_outflow(self, dt):
        return self.volume / dt

    def max_inflow(self, dt):
        return max(0.0, self.max_capacity - self.volume) / dt

    @property
    def is_empty(self):
        return self.volume < 0.01

    def stream(self, port):
        return self.contents.copy()

    def draw(self, material):
        for k, n in material.moles.items():
            self.contents.moles[k] = max(0.0, self.contents.moles.get(k, 0.0) - n)

    def receive(self, material):
        self.contents.add(material)

    def update(self, dt):
        """Internal behaviour per step (reactions, leaks). Subclasses override."""

    def get_state(self):
        return self.contents.to_state()

    def set_state(self, state):
        self.contents = Material.from_state(state)
