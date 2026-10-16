import math

from Modules.Entity.Process_equipment.transfer import Transfer, k_to_resistance
from Modules.Flow.chemistry import COMPONENTS
from Modules.Flow.flow import Material

# =====================================================================
# Parent class: Transfer
# Class: Pipe (straight run, friction loss)
# Subclasses: Elbow (extra bend loss)
#
# K = friction * length / diameter + fitting_k. The flow area can be closed by three things:
#   blockage - a fault you set by hand (0..1)
#   fouling  - deposits that build up slowly while the liquid is over-saturated at the wall
#              temperature (component attributes solubility_25c / solubility_slope). It stays
#              until cleaned: set_fault(tag, "fouling", 0).
#   ice      - forms when the wall is colder than the liquid's freezing_point_c, melts when warmer.
# The wall temperature follows the liquid while it flows and drifts to the outdoor temperature
# when stagnant (ambient_ua W/K, thermal_mass J/K). Pipes have no holdup, but remember the last liquid.
# =====================================================================

FLUID_UA = 200.0        # W/K between flowing liquid and the pipe wall
ICE_RATE = 5e-5         # ice fraction per K below freezing per second
FOULING_RATE = 1e-5     # fouling fraction per (mol/L over saturation) per second


class Pipe(Transfer):
    FAULTS = ("leak_rate", "blockage", "fouling")

    def __init__(self, name, description="", status=1, inlets=None, outlets=None,
                 diameter=0.05, length=5.0, friction_factor=0.02, fitting_k=0.0,
                 ambient_ua=2.0, thermal_mass=2e4):
        super().__init__(name, description, status, inlets, outlets)
        self.diameter = diameter
        self.length = length
        self.friction_factor = friction_factor
        self.fitting_k = fitting_k
        self.ambient_ua = ambient_ua
        self.thermal_mass = thermal_mass
        self.blockage = 0.0
        self.fouling = 0.0
        self.ice = 0.0
        self.wall_temp = None
        self.contents = None        # last liquid seen in the pipe
        self.flowing = False

    @property
    def total_blockage(self):
        return 1.0 - (1.0 - self.blockage) * (1.0 - self.fouling) * (1.0 - self.ice)

    def is_blocked(self):
        return self.total_blockage >= 0.99

    def total_k(self):
        return self.friction_factor * self.length / self.diameter + self.fitting_k

    def loss_coefficient(self):
        return k_to_resistance(self.total_k(), self.diameter, 1.0 - self.total_blockage)

    def after_step(self, dt, starved, fluid=None):
        self.flowing = abs(self.flow) > 1e-6
        if fluid is not None and fluid.volume > 0 and (self.flowing or self.contents is None):
            self.contents = fluid.copy()
            if self.wall_temp is None:
                self.wall_temp = fluid.temperature

    def repair(self):
        """Clean and de-ice the line and remove a manual blockage."""
        super().repair()
        self.blockage = self.fouling = self.ice = 0.0

    def slow_update(self, dt, ambient):
        if self.contents is None or self.wall_temp is None:
            return
        ua_f = FLUID_UA if self.flowing else 0.0
        ua = ua_f + self.ambient_ua
        if ua > 0:
            t_eq = (ua_f * self.contents.temperature + self.ambient_ua * ambient) / ua
            self.wall_temp = t_eq + (self.wall_temp - t_eq) * math.exp(-ua * dt / self.thermal_mass)

        freezing = self.contents.attribute_average("freezing_point_c")
        if freezing is not None:
            self.ice = min(1.0, max(0.0, self.ice + ICE_RATE * (freezing - self.wall_temp) * dt))

        for name in self.contents.moles:
            attrs = COMPONENTS[name].attributes
            if "solubility_25c" not in attrs:
                continue
            solubility = max(0.0, attrs["solubility_25c"] + attrs.get("solubility_slope", 0.0) * (self.wall_temp - 25.0))
            excess = self.contents.concentration(name) - solubility
            if excess > 0:
                self.fouling = min(1.0, self.fouling + FOULING_RATE * excess * dt)

    def get_state(self):
        return {"blockage": float(self.blockage), "fouling": float(self.fouling), "ice": float(self.ice),
                "wall_temp": None if self.wall_temp is None else float(self.wall_temp),
                "contents": self.contents.to_state() if self.contents is not None else None}

    def set_state(self, state):
        self.blockage = state["blockage"]
        self.fouling = state.get("fouling", 0.0)
        self.ice = state.get("ice", 0.0)
        self.wall_temp = state.get("wall_temp")
        self.contents = Material.from_state(state["contents"]) if state.get("contents") else None
        self.flowing = False


class Elbow(Pipe):
    """Bend. angle is in degrees; a 90 degree elbow adds K = 0.9."""

    def __init__(self, name, description="", status=1, inlets=None, outlets=None,
                 angle=90.0, **pipe_options):
        super().__init__(name, description, status, inlets, outlets, **pipe_options)
        self.angle = angle
        self.fitting_k += 0.9 * angle / 90.0
