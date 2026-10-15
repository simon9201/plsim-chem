import math

from Modules.Entity.Process_equipment.Vessel.Reactor.Reactor import Reactor
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

    @property
    def cooling_fraction(self):
        return self.cooling_valve.position if self.cooling_valve is not None else self.cooling_command

    def update(self, dt):
        self.heat_released = 0.0
        heat_total = 0.0
        for name in self.reaction_names:
            heat_total += self._react(REACTIONS[name], dt)
        self.heat_released = heat_total / dt

        cp = self.contents.heat_capacity
        self.cooling_duty = 0.0
        if cp > 0:
            duty = self.cooling_ua * self.cooling_fraction * (self.temperature - self.coolant_temp)
            self.cooling_duty = duty
            self.contents.temperature -= duty * dt / cp

    def _react(self, reaction, dt):
        """Advance one reaction over dt. Returns the heat released (J, positive = exothermic)."""
        heat = 0.0
        enthalpy = reaction.enthalpy()
        reactants = {c: -nu for c, nu in reaction.stoichiometry.items() if nu < 0}
        steps_done, nsub = 0, 1
        while steps_done < nsub:
            volume = self.volume
            if volume <= 0:
                break
            conc = {c: self.contents.moles.get(c, 0.0) / volume for c in reaction.orders}
            rate = reaction.rate_constant(self.temperature)
            for c, order in reaction.orders.items():
                rate *= conc[c] ** order
            if steps_done == 0:
                # Substep when the reaction is fast compared to the step, to stay stable.
                smallest = min((conc[c] for c in reactants), default=0.0)
                kappa = rate / smallest if smallest > 1e-12 else 0.0
                nsub = min(200, max(1, math.ceil(kappa * dt / 0.05)))
            h = dt / nsub
            extent = rate * volume * h
            for c, coeff in reactants.items():
                extent = min(extent, self.contents.moles.get(c, 0.0) / coeff)
            if extent > 0:
                for c, nu in reaction.stoichiometry.items():
                    self.contents.moles[c] = max(0.0, self.contents.moles.get(c, 0.0) + nu * extent)
                q = -enthalpy * extent
                heat += q
                cp = self.contents.heat_capacity
                if cp > 0:
                    self.contents.temperature += q / cp
            steps_done += 1
        return heat

    def get_state(self):
        state = super().get_state()
        state["cooling_command"] = float(self.cooling_command)
        return state

    def set_state(self, state):
        super().set_state(state)
        self.cooling_command = state["cooling_command"]
