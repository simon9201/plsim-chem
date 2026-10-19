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
#
# Freeze-thaw fatigue: every time the wall drops below the liquid's freezing point and later warms
# back up (by FATIGUE_HYSTERESIS_K) one cycle is counted, and the pipe takes `fatigue_per_cycle` damage
# (more if a lot of ice formed). Past 50 % fatigue the pipe starts to weep: effective leak grows with
# the square of the excess up to `fatigue_leak_max` L/s at 100 %. Northern winters therefore wear
# pipes out slowly; tune the damage with the constructor / database params
#   fatigue_per_cycle, fatigue_leak_max, ice_rate_scale, fouling_rate_scale
# repair() resets fatigue and the cycle count.
# =====================================================================

FLUID_UA = 200.0        # W/K between flowing liquid and the pipe wall
ICE_RATE = 5e-5         # ice fraction per K below freezing per second
FOULING_RATE = 1e-5     # fouling fraction per (mol/L over saturation) per second
FATIGUE_HYSTERESIS_K = 2.0      # the wall must warm this far above freezing to count a thaw
SLURRY_CLOG_RATE = 2e-3         # fouling fraction per (mol/L undissolved) per second while slurry flows through
HOTSPOT_ONSET = 0.2             # blockage above which the restriction creates damaging pressure hotspots
HOTSPOT_RATE = 1e-4             # fatigue per second at a fully blocked (but still flowing) line
FATIGUE_LEAK_ONSET = 0.5        # fatigue level where the pipe starts to leak


class Pipe(Transfer):
    FAULTS = ("leak_rate", "blockage", "fouling", "fatigue")

    def __init__(self, name, description="", status=1, inlets=None, outlets=None,
                 diameter=0.05, length=5.0, friction_factor=0.02, fitting_k=0.0,
                 ambient_ua=2.0, thermal_mass=2e4,
                 fatigue_per_cycle=0.002, fatigue_leak_max=0.5, ice_rate_scale=1.0, fouling_rate_scale=1.0):
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
        self.fatigue_per_cycle = fatigue_per_cycle
        self.fatigue_leak_max = fatigue_leak_max
        self.ice_rate_scale = ice_rate_scale
        self.fouling_rate_scale = fouling_rate_scale
        self.fatigue = 0.0              # 0 = sound, 1 = split open
        self.freeze_thaw_cycles = 0
        self._frozen_cycle = False      # inside a freeze-thaw cycle (wall below freezing since the last thaw)
        self._peak_ice = 0.0
        self.wall_temp = None
        self.contents = None        # last liquid seen in the pipe
        self.flowing = False
        self.slurry_load = 0.0      # mol/L undissolved solid in the stream passing now

    @property
    def total_blockage(self):
        return 1.0 - (1.0 - self.blockage) * (1.0 - self.fouling) * (1.0 - self.ice)

    @property
    def effective_leak(self):
        """Manual leak plus the weeping of a fatigued pipe (L/s)."""
        excess = max(0.0, (self.fatigue - FATIGUE_LEAK_ONSET) / (1.0 - FATIGUE_LEAK_ONSET))
        return self.leak_rate + self.fatigue_leak_max * excess * excess

    def is_blocked(self):
        return self.total_blockage >= 0.99

    def total_k(self):
        return self.friction_factor * self.length / self.diameter + self.fitting_k

    def loss_coefficient(self):
        return k_to_resistance(self.total_k(), self.diameter, 1.0 - self.total_blockage)

    def after_step(self, dt, starved, fluid=None):
        self.flowing = abs(self.flow) > 1e-6
        self.slurry_load = 0.0
        if self.flowing and fluid is not None and fluid.volume > 0:
            self._carry_solids(dt, fluid)
        if fluid is not None and fluid.volume > 0 and (self.flowing or self.contents is None):
            self.contents = fluid.copy()
            if self.wall_temp is None:
                self.wall_temp = fluid.temperature

    def _carry_solids(self, dt, fluid):
        """Undissolved solid in the stream (more of a component than the liquid can hold at its own
        temperature, e.g. solids that a tank has not had time to dissolve) packs into the line. It
        grows `fouling` quickly. Once the line is partly closed the flow squeezes through the
        restriction and the pressure hotspot at the plug damages the pipe (`fatigue`)."""
        load = 0.0
        for name in fluid.moles:
            attrs = COMPONENTS[name].attributes
            if "solubility_25c" in attrs:
                solubility = max(0.0, attrs["solubility_25c"] + attrs.get("solubility_slope", 0.0) * (fluid.temperature - 25.0))
                load += max(0.0, fluid.concentration(name) - solubility)
        self.slurry_load = load
        if load > 1e-3:
            self.fouling = min(1.0, self.fouling + SLURRY_CLOG_RATE * self.fouling_rate_scale * load * dt)
        if self.total_blockage > HOTSPOT_ONSET:
            self.fatigue = min(1.0, self.fatigue + HOTSPOT_RATE * (self.total_blockage - HOTSPOT_ONSET) * dt)

    def repair(self):
        """Clean and de-ice the line and remove a manual blockage."""
        super().repair()
        self.blockage = self.fouling = self.ice = 0.0
        self.fatigue = self._peak_ice = 0.0
        self.freeze_thaw_cycles = 0
        self._frozen_cycle = False

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
            self.ice = min(1.0, max(0.0, self.ice + ICE_RATE * self.ice_rate_scale * (freezing - self.wall_temp) * dt))
            self._count_freeze_thaw(freezing)

        for name in self.contents.moles:
            attrs = COMPONENTS[name].attributes
            if "solubility_25c" not in attrs:
                continue
            solubility = max(0.0, attrs["solubility_25c"] + attrs.get("solubility_slope", 0.0) * (self.wall_temp - 25.0))
            excess = self.contents.concentration(name) - solubility
            if excess > 0:
                self.fouling = min(1.0, self.fouling + FOULING_RATE * self.fouling_rate_scale * excess * dt)

    def _count_freeze_thaw(self, freezing):
        self._peak_ice = max(self._peak_ice, self.ice)
        if self.wall_temp < freezing:
            self._frozen_cycle = True
        elif self._frozen_cycle and self.wall_temp > freezing + FATIGUE_HYSTERESIS_K:
            self._frozen_cycle = False
            self.freeze_thaw_cycles += 1
            self.fatigue = min(1.0, self.fatigue + self.fatigue_per_cycle * (0.5 + self._peak_ice))
            self._peak_ice = 0.0

    def get_state(self):
        return {"fatigue": float(self.fatigue), "freeze_thaw_cycles": int(self.freeze_thaw_cycles),
                "frozen_cycle": bool(self._frozen_cycle), "peak_ice": float(self._peak_ice),
                "blockage": float(self.blockage), "fouling": float(self.fouling), "ice": float(self.ice),
                "wall_temp": None if self.wall_temp is None else float(self.wall_temp),
                "contents": self.contents.to_state() if self.contents is not None else None}

    def set_state(self, state):
        self.blockage = state["blockage"]
        self.fouling = state.get("fouling", 0.0)
        self.ice = state.get("ice", 0.0)
        self.fatigue = state.get("fatigue", 0.0)
        self.freeze_thaw_cycles = state.get("freeze_thaw_cycles", 0)
        self._frozen_cycle = state.get("frozen_cycle", False)
        self._peak_ice = state.get("peak_ice", 0.0)
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
