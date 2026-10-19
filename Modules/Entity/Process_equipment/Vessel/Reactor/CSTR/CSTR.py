import math

from Modules.Entity.Process_equipment.Vessel.Reactor.Reactor import Reactor
from Modules.Flow import kinetics
from Modules.Flow.chemistry import REACTIONS

# =====================================================================
# Parent class: Reactor
# Class: ContinuousStirredTankReactor (CSTR)
#
# Perfectly mixed. Every named reaction runs each step:
#     rate [mol/(L*s)] = k(T) * product(c_i ** order_i)
# Reactants are consumed and products made per the stoichiometry, and the heat
# of reaction changes the temperature. Cooling: UA * valve position * (T - coolant).
# =====================================================================


class ContinuousStirredTankReactor(Reactor):
    def __init__(self, name, description="", status=1, inlets=None, outlets=None,
                 cooling_ua=500.0, coolant_temp=15.0, cooling_command=0.0, **reactor_options):
        super().__init__(name, description, status, inlets, outlets, **reactor_options)
        self.cooling_ua = cooling_ua            # W/K at fully open cooling valve
        self.coolant_temp = coolant_temp        # C
        self.cooling_command = cooling_command  # 0..1, used when no cooling valve is linked
        self.cooling_valve = None               # a UtilityValve, linked by the loader
        self.cooling_duty = 0.0                 # W removed during the last step
        self.fouling = 0.0                      # 0..1 hidden condition: the jacket/coil scales up and removes less heat

    FAULTS = Reactor.FAULTS + ("fouling",)

    def repair(self):
        self.fouling = 0.0
        super().repair()

    @property
    def cooling_fraction(self):
        if self.cooling_valve is not None:
            return self.cooling_valve.position if self.cooling_valve.status else 0.0
        return self.cooling_command

    def update(self, dt):
        self.heat_released = 0.0
        reactions = [REACTIONS[name] for name in self.reaction_names]
        if reactions and self.volume > 0:
            # All reactions advance together (coupled RK4) so consecutive, competing and reversible ones are right.
            self.contents.temperature, heat_total = kinetics.advance(
                self.contents.moles, self.contents.temperature, reactions, dt)
            self.heat_released = heat_total / dt

        cp = self.contents.heat_capacity
        self.cooling_duty = 0.0
        if cp > 0:
            old_temperature = self.temperature
            ua = self.cooling_ua * self.cooling_fraction * (1.0 - min(1.0, max(0.0, self.fouling)))
            self.contents.temperature = self.coolant_temp + (old_temperature - self.coolant_temp) * math.exp(
                -ua * dt / cp)
            self.cooling_duty = cp * (old_temperature - self.temperature) / dt

    def get_state(self):
        state = super().get_state()
        state["cooling_command"] = float(self.cooling_command)
        state["fouling"] = float(self.fouling)
        return state

    def set_state(self, state):
        super().set_state(state)
        self.cooling_command = state["cooling_command"]
        self.fouling = state.get("fouling", 0.0)
