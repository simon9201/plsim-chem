import math

from Modules.Entity.Entity import Signal, TO_PLC, FROM_PLC
from Modules.Entity.Process_equipment.Pipe.Pipe import Pipe

# =====================================================================
# Parent class: Pipe (hydraulics, fouling blockage, freeze-thaw, leaks, all unchanged)
# Class: HeatExchanger - heats or cools the liquid flowing through it against a utility (steam, hot oil,
# cooling water) held at `utility_temp`. Also used as a heater, cooler or condenser-style trim cooler.
#
# Effectiveness method (epsilon-NTU with a constant utility temperature):
#     eps = 1 - exp(-UA_eff / C),    T_out = T_in + eps * (utility_temp - T_in)
# where C = flow * density * cp of the stream (W/K) and UA_eff = ua * (1 - scaling) * DutyCmd / 100.
# More flow or less UA means less temperature change, so the outlet temperature follows the flow.
# `scaling` is a slow deposit on the heat-transfer surface (grows while liquid flows, faster the bigger the
# temperature difference); the pipe's own `fouling` still narrows the flow area. repair() cleans both.
# DutyCmd (0..100 %) is the PLC's utility valve. The energy comes from the utility: nothing is conserved on
# the utility side. Pressure drop is the Pipe's (set diameter/length/fitting_k to taste, K = 2 + friction is typical).
# =====================================================================

SCALING_RATE = 1e-9         # per K of temperature difference per second (slow clock)


class HeatExchanger(Pipe):
    FAULTS = Pipe.FAULTS + ("scaling",)

    def __init__(self, name, description="", status=1, inlets=None, outlets=None,
                 ua=2000.0, utility_temp=120.0, scaling_rate=SCALING_RATE, fitting_k=3.0, **pipe_options):
        super().__init__(name, description, status, inlets, outlets, fitting_k=fitting_k, **pipe_options)
        self.ua = ua                    # W/K, clean
        self.utility_temp = utility_temp
        self.scaling_rate = scaling_rate
        self.scaling = 0.0
        self.duty_command = 100.0       # % utility valve
        self.duty = 0.0                 # W into the liquid (negative = cooling) during the last step
        self.outlet_temp = None

    @property
    def effective_ua(self):
        return self.ua * (1.0 - self.scaling) * self.duty_command / 100.0 if self.status else 0.0

    def condition_liquid(self, material, rate, dt):
        heat_capacity = material.heat_capacity          # J/K of this step's packet
        if material.volume <= 0 or heat_capacity <= 0 or self.effective_ua <= 0:
            self.duty = 0.0
            self.outlet_temp = material.temperature if material.volume > 0 else self.outlet_temp
            return material
        eps = 1.0 - math.exp(-self.effective_ua * dt / heat_capacity)
        delta = eps * (self.utility_temp - material.temperature)
        self.duty = heat_capacity * delta / dt
        material.temperature += delta
        self.outlet_temp = material.temperature
        return material

    def slow_update(self, dt, ambient):
        super().slow_update(dt, ambient)
        if self.flowing and self.contents is not None:
            self.scaling = min(1.0, self.scaling + self.scaling_rate * abs(self.utility_temp - self.contents.temperature) * dt)

    def repair(self):
        super().repair()
        self.scaling = 0.0

    def signals(self):
        return [
            Signal("DutyCmd", FROM_PLC, "REAL", "%", get=lambda: self.duty_command,
                   set=lambda v: setattr(self, "duty_command", min(100.0, max(0.0, v)))),
            Signal("Duty", TO_PLC, "REAL", "kW", get=lambda: self.duty / 1000.0),
            Signal("OutletTemp", TO_PLC, "REAL", "C", get=lambda: self.outlet_temp if self.outlet_temp is not None else 0.0),
        ]

    def apply_failsafe(self):
        self.duty_command = 0.0

    def get_state(self):
        state = super().get_state()
        state.update({"scaling": float(self.scaling), "duty_command": float(self.duty_command)})
        return state

    def set_state(self, state):
        super().set_state(state)
        self.scaling = state.get("scaling", 0.0)
        self.duty_command = state.get("duty_command", 100.0)
