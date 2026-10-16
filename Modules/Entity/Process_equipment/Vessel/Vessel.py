from Modules.Entity.Process_equipment.process_equipment import ProcessEquipment, G, P_ATM
import math
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
    FAULTS = ("leak_rate",)
    # Solids and freezing (see update_solids / update_freezing):
    CRYSTALLIZATION_RATE = 0.05     # 1/s, how fast excess dissolved solid comes out of solution (and redissolves)
    THAW_MARGIN = 1.0               # K above the freezing point before a frozen vessel thaws
    SOLIDS_PLUG_FRACTION = 0.5      # solids filling this fraction of the vessel plug the bottom outlets

    def __init__(self, name, description="", status=1, inlets=None, outlets=None,
                 max_capacity=1000.0, height=3.0, headspace_pressure=P_ATM,
                 initial_volume=0.0, composition=None, temperature=25.0, ambient_ua=0.0):
        super().__init__(name, description, status, inlets, outlets)
        self.ambient_ua = ambient_ua            # W/K heat exchange with the outdoor air (0 = perfectly insulated)
        self.max_capacity = max_capacity        # L
        self.height = height                    # m
        self.headspace_pressure = headspace_pressure
        self.solids = {}                        # {component: mol} settled solid on the bottom, out of solution
        self.frozen = False                     # error state: the contents are frozen solid, nothing can flow
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
    def solids_volume(self):
        """L occupied by settled solids"""
        return sum(n * COMPONENTS[k].molar_mass / COMPONENTS[k].attributes.get("solid_density", COMPONENTS[k].density)
                   for k, n in self.solids.items())

    @property
    def solids_percent(self):
        """% of the vessel capacity filled with settled solids"""
        return 100.0 * self.solids_volume / self.max_capacity

    @property
    def plugged(self):
        """Settled solids have buried the bottom outlets."""
        return self.solids_volume >= self.SOLIDS_PLUG_FRACTION * self.max_capacity

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
        return port in self.outlets and not self.frozen and not self.plugged

    def can_accept(self, port):
        return not self.frozen

    def max_outflow(self, dt):
        return self.volume / dt

    def max_inflow(self, dt):
        return max(0.0, self.max_capacity - self.volume - self.solids_volume) / dt

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

    def apply_leak(self, dt):
        if self.leak_rate > 0:
            self.contents.remove_volume(self.leak_rate * dt)

    def slow_update(self, dt, ambient):
        cp = self.contents.heat_capacity
        if self.ambient_ua > 0 and cp > 0:
            self.temperature = ambient + (self.temperature - ambient) * math.exp(-self.ambient_ua * dt / cp)

    def update(self, dt):
        """Internal behaviour per step (reactions, cooling). Subclasses override."""

    def run_step(self, dt):
        """Everything a vessel does in one step, in order: leak, solids, freezing, then the subclass behaviour."""
        self.apply_leak(dt)
        self.update_solids(dt)
        self.update_freezing()
        self.update(dt)

    def update_solids(self, dt):
        """Precipitation and redissolving. A component with a `solubility_25c` attribute [mol/L] (and optional
        `solubility_slope` [mol/L per C]) can only stay dissolved up to that concentration at the current
        temperature. The excess drops out as a solid, relaxing exponentially (CRYSTALLIZATION_RATE), and settled
        solid dissolves again when the liquid becomes under-saturated. Typical causes: cooling a concentrated
        liquid, or a reaction making more product than the solvent can hold. Solids stay in the vessel."""
        volume = self.volume
        if volume <= 0:
            return
        fraction = 1.0 - math.exp(-self.CRYSTALLIZATION_RATE * dt)
        for name in set(self.contents.moles) | set(self.solids):
            attrs = COMPONENTS[name].attributes
            if "solubility_25c" not in attrs:
                continue
            solubility = max(0.0, attrs["solubility_25c"] + attrs.get("solubility_slope", 0.0) * (self.temperature - 25.0))
            dissolved = self.contents.moles.get(name, 0.0)
            excess = dissolved - solubility * volume
            if excess > 0:
                moved = excess * fraction
            elif self.solids.get(name, 0.0) > 0:
                moved = -min(self.solids[name], -excess * fraction)
            else:
                continue
            self.contents.moles[name] = dissolved - moved
            self.solids[name] = max(0.0, self.solids.get(name, 0.0) + moved)
            if self.solids[name] < 1e-12:
                del self.solids[name]

    def update_freezing(self):
        """Freezing is an error state, not a dynamic: below the liquid's freezing point (mole-fraction average
        of the components' freezing_point_c) the vessel is frozen and nothing can enter or leave until it
        thaws THAW_MARGIN above the freezing point. Heat it, or keep it warm, to avoid it."""
        freezing = self.contents.attribute_average("freezing_point_c") if self.volume > 0 else None
        if freezing is None:
            self.frozen = False
        elif self.temperature < freezing:
            self.frozen = True
        elif self.temperature > freezing + self.THAW_MARGIN:
            self.frozen = False

    def clean(self):
        """Remove settled solids (drain the sump)."""
        self.solids.clear()

    def repair(self):
        super().repair()
        self.clean()

    def get_state(self):
        state = self.contents.to_state()
        if self.solids or self.frozen:
            state["solids"] = {k: float(n) for k, n in self.solids.items()}
            state["frozen"] = bool(self.frozen)
        return state

    def set_state(self, state):
        self.contents = Material.from_state(state)
        self.solids = dict(state.get("solids", {}))
        self.frozen = bool(state.get("frozen", False))
