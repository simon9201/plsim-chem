import numpy as np
from Modules.Entity.Process_equipment.Vessel.Reactor.Reactor import Reactor
from Modules.Flow.flow import R_GAS

# =====================================================================
# Parent class: Reactor
# Class: ContinuousStirredTankReactor (CSTR)
# Reaction: A + B -> C, exothermic, rate = k(T) * [A] * [B]
# =====================================================================

T_REF_K = 298.15  # k_ref is given at 25 C


class ContinuousStirredTankReactor(Reactor):
    """Perfectly mixed reactor with Arrhenius kinetics, reaction heat and jacket cooling."""
    def __init__(self, name, max_capacity, initial_moles=None, initial_temp=25.0,
                 inlets=None, outlets=None,
                 k_ref=0.002,               # L/(mol*s) at 25 C
                 activation_energy=50e3,    # J/mol
                 heat_of_reaction=-40e3,    # J/mol (negative = exothermic)
                 cooling_ua=20e3,           # W/K at fully open cooling
                 coolant_temp=15.0):        # C
        super().__init__(name, max_capacity, initial_moles, initial_temp, inlets, outlets)
        self.k_ref = k_ref
        self.activation_energy = activation_energy
        self.heat_of_reaction = heat_of_reaction
        self.cooling_ua = cooling_ua
        self.coolant_temp = coolant_temp
        self.cooling_command = 0.0          # 0..1, used only when no cooling valve is linked
        self.cooling_valve = None           # a UtilityValve, linked by the loader

    @property
    def cooling_fraction(self):
        """How open the cooling is, 0..1."""
        if self.cooling_valve is not None:
            return self.cooling_valve.position
        return self.cooling_command

    def update(self, dt, inflows, outflow_rate):
        # 1. Flows and mixing via the vessel
        super().update(dt, inflows, outflow_rate)

        volume = self.volume
        if volume <= 0:
            return
        contents = self.contents

        # 2. Reaction: extent is limited by the available reactants
        temp_k = self.temperature + 273.15
        k = self.k_ref * np.exp(-self.activation_energy / R_GAS * (1.0 / temp_k - 1.0 / T_REF_K))
        rate = k * contents.concentration("A") * contents.concentration("B")   # mol/(L*s)
        extent = min(rate * volume * dt, contents.moles.get("A", 0.0), contents.moles.get("B", 0.0))
        for name, coeff in (("A", -1.0), ("B", -1.0), ("C", 1.0)):
            contents.moles[name] = contents.moles.get(name, 0.0) + coeff * extent

        # 3. Energy balance: reaction heat in, jacket cooling out (cannot overshoot the coolant temperature)
        heat_cap = contents.heat_capacity
        heat_released = -self.heat_of_reaction * extent
        delta = self.temperature - self.coolant_temp
        limit = abs(delta) * heat_cap
        cooling = self.cooling_ua * min(1.0, max(0.0, self.cooling_fraction)) * delta * dt
        cooling = max(-limit, min(limit, cooling))
        self.temperature += (heat_released - cooling) / heat_cap
