import math
from dataclasses import dataclass, field
from functools import cached_property
from typing import Optional

# =====================================================================
# Chemistry library: which components exist and which reactions can happen.
#
# COMPONENTS and REACTIONS are plain dictionaries shared by the whole program.
# They start with a built-in test chemistry and are replaced by the contents
# of the database when a plant is loaded (Data/chemistry.py).
# All numbers below are PLACEHOLDERS for a made-up A + B -> C system in water,
# except water itself.
# =====================================================================

R_GAS = 8.314       # J/(mol*K)
T_REF_K = 298.15    # kinetic constants are given at 25 C


@dataclass(frozen=True)
class Component:
    name: str
    molar_mass: float                           # g/mol
    density: float                              # g/L (pure liquid)
    cp: float                                   # J/(mol*K), liquid heat capacity
    enthalpy_formation: Optional[float] = None  # J/mol, liquid at 25 C (used to derive reaction enthalpies)
    # Free-form facts worth tracking: {"boiling_point_c": 118, "hazard": "corrosive", ...}
    # Used by the simulation: boiling_point_c (cavitation), freezing_point_c (ice in cold pipes),
    # solubility_25c [mol/L] and solubility_slope [mol/L per C]: deposits in pipes and precipitates as a solid in vessels
    # when the liquid is over-saturated. Optional solid_density [g/L] sets the volume of the precipitate.
    # Vapor-liquid data (antoine_a/b/c, heat_of_vaporization, cp_gas, noncondensable) is described in Modules/Flow/thermo.py.
    attributes: dict = field(default_factory=dict)

    @cached_property
    def molar_volume(self):
        """Liter per mol (cached: the volume of every vessel is recomputed many times per step)"""
        return self.molar_mass / self.density


@dataclass
class Reaction:
    """A reaction with its stoichiometry, rate law and heat.

    stoichiometry: {component: coefficient}, negative = consumed, positive = produced.
    orders: reaction order per component in the forward rate law. Defaults to the reactant coefficients.
        Any component can appear here, so a catalyst (positive order, never consumed) or an inhibitor
        (negative order) is just an extra entry.
    forward rate [mol/(L*s)] = k(T) * product(concentration ** order)
    heat_of_reaction: J per mol of reaction extent at 25 C (negative = exothermic).
        If left out it is calculated from the components' enthalpies of formation.
        At other temperatures the heat follows the heat capacities (Kirchhoff's law, see enthalpy_at).

    Reversible reactions: set reversible=True and give k_equilibrium_ref, the equilibrium constant Kc at 25 C
    (in the same concentration units as the orders). K moves with temperature by the van 't Hoff equation
    (exothermic reactions favour products when cold) and the reverse rate constant is k / K, so the reaction
    always relaxes towards equilibrium. reverse_orders defaults to the product coefficients.
    """
    name: str
    stoichiometry: dict
    k_ref: float                # rate constant at 25 C, in units that match the orders
    activation_energy: float    # J/mol
    orders: Optional[dict] = None
    heat_of_reaction: Optional[float] = None
    description: str = ""
    reversible: bool = False
    k_equilibrium_ref: Optional[float] = None   # Kc at 25 C, required when reversible
    reverse_orders: Optional[dict] = None

    def __post_init__(self):
        if not any(v < 0 for v in self.stoichiometry.values()) or not any(v > 0 for v in self.stoichiometry.values()):
            raise ValueError(f"Reaction {self.name} needs at least one reactant (negative) and one product (positive)")
        if not self.orders:
            self.orders = {c: -nu for c, nu in self.stoichiometry.items() if nu < 0}
        if self.reversible:
            if not self.k_equilibrium_ref or self.k_equilibrium_ref <= 0:
                raise ValueError(f"Reaction {self.name} is reversible: give a positive k_equilibrium_ref")
            if not self.reverse_orders:
                self.reverse_orders = {c: nu for c, nu in self.stoichiometry.items() if nu > 0}

    def rate_constant(self, temp_c):
        temp_k = max(temp_c + 273.15, 1.0)
        return self.k_ref * math.exp(-self.activation_energy / R_GAS * (1.0 / temp_k - 1.0 / T_REF_K))

    def equilibrium_constant(self, temp_c, components=None):
        """Kc at a temperature (van 't Hoff, using the heat of reaction at 25 C). None for an irreversible reaction."""
        if not self.reversible:
            return None
        temp_k = max(temp_c + 273.15, 1.0)
        exponent = -self.enthalpy(components) / R_GAS * (1.0 / temp_k - 1.0 / T_REF_K)
        return self.k_equilibrium_ref * math.exp(max(-50.0, min(50.0, exponent)))

    def reverse_rate_constant(self, temp_c, components=None):
        k_eq = self.equilibrium_constant(temp_c, components)
        return 0.0 if k_eq is None else self.rate_constant(temp_c) / k_eq

    def rate_parts(self, conc, temp_c, components=None):
        """(forward, backward) rates in mol/(L*s) for concentrations {component: mol/L}."""
        forward = self.rate_constant(temp_c)
        for c, order in self.orders.items():
            forward *= _power(conc.get(c, 0.0), order)
        backward = 0.0
        if self.reversible:
            backward = self.reverse_rate_constant(temp_c, components)
            for c, order in self.reverse_orders.items():
                backward *= _power(conc.get(c, 0.0), order)
        return forward, backward

    def rate(self, conc, temp_c, components=None):
        """Net rate in mol/(L*s). Negative means a reversible reaction runs backwards."""
        forward, backward = self.rate_parts(conc, temp_c, components)
        return forward - backward

    def enthalpy(self, components=None):
        """J per mol of reaction extent at 25 C."""
        if self.heat_of_reaction is not None:
            return self.heat_of_reaction
        components = COMPONENTS if components is None else components
        total = 0.0
        for name, nu in self.stoichiometry.items():
            hf = components[name].enthalpy_formation
            if hf is None:
                raise ValueError(f"Reaction {self.name}: set heat_of_reaction, or give {name} an enthalpy_formation")
            total += nu * hf
        return total

    def enthalpy_at(self, temp_c, components=None):
        """J per mol of reaction extent at a temperature: the 25 C value plus the change of the products' and
        reactants' heat capacities (Kirchhoff). This is the heat that really goes into the energy balance."""
        components = COMPONENTS if components is None else components
        delta_cp = sum(nu * components[name].cp for name, nu in self.stoichiometry.items())
        return self.enthalpy(components) + delta_cp * (temp_c - 25.0)


def _power(x, order):
    """x ** order that is safe at x = 0 (a missing inhibitor must not divide by zero) and for tiny negative x."""
    if order == 0:
        return 1.0
    x = max(x, 0.0)
    if order < 0:
        return max(x, 1e-9) ** order
    return x ** order


DEFAULT_COMPONENTS = (
    Component("water", 18.015, 997.0, 75.3, -285830.0, {"role": "solvent", "boiling_point_c": 100.0, "freezing_point_c": 0.0,
                    "antoine_a": 4.6543, "antoine_b": 1435.264, "antoine_c": -64.848,
                    "heat_of_vaporization": 40660.0, "cp_gas": 33.6}),
    Component("A", 60.0, 1000.0, 120.0, -100e3, {"role": "reactant"}),
    Component("B", 46.0, 800.0, 110.0, -50e3, {"role": "reactant"}),
    Component("C", 106.0, 900.0, 190.0, -290e3, {"role": "product"}),
    # Permanent gas for headspaces and gas lines. Its liquid values are only used if it is ever forced into a liquid.
    Component("N2", 28.014, 808.0, 57.0, 0.0, {"role": "inert", "noncondensable": True, "cp_gas": 29.1}),
    # Quality chemistry (placeholders, mass balanced against A, C): an impurity that arrives with a poor feed
    # lot, the off-spec product it makes with A, and a reversible dimer of the product.
    Component("I", 40.0, 900.0, 100.0, -20e3, {"role": "impurity"}),
    Component("X", 100.0, 950.0, 170.0, -150e3, {"role": "byproduct"}),
    Component("D", 212.0, 950.0, 380.0, -600e3, {"role": "byproduct"}),
    # Volatile organic for separation work (distillation, evaporation). Antoine constants converted to bar and K.
    Component("ethanol", 46.07, 789.0, 112.0, -277.6e3, {"role": "solvent", "boiling_point_c": 78.4,
              "freezing_point_c": -114.0, "antoine_a": 5.3291, "antoine_b": 1642.89, "antoine_c": -42.85,
              "heat_of_vaporization": 38560.0, "cp_gas": 65.6}),
)

DEFAULT_REACTIONS = (
    # Heat of reaction follows from the formation enthalpies: -290 - (-100) - (-50) = -140 kJ/mol
    Reaction("A_B_to_C", {"A": -1, "B": -1, "C": 1}, k_ref=0.01, activation_energy=50e3,
             description="A + B -> C, exothermic, first order in each reactant"),
    # Parallel side reaction: impurity I steals reactant A and makes off-spec X (-30 kJ/mol).
    Reaction("A_I_to_X", {"A": -1, "I": -1, "X": 1}, k_ref=0.02, activation_energy=40e3,
             description="A + I -> X, side reaction caused by an impure feed lot"),
    # Reversible consecutive reaction: the product dimerises (-20 kJ/mol), more at low temperature and high concentration.
    Reaction("C_dimerization", {"C": -2, "D": 1}, k_ref=1e-3, activation_energy=60e3, reversible=True,
             k_equilibrium_ref=50.0, description="2 C <-> D, reversible, exothermic"),
)

COMPONENTS = {}
REACTIONS = {}


def set_chemistry(components, reactions):
    """Replace the chemistry library (in place, so everything that imported the dictionaries sees it)."""
    comp = {c.name: c for c in components}
    rx = {r.name: r for r in reactions}
    for r in rx.values():
        unknown = [c for c in r.stoichiometry if c not in comp]
        if unknown:
            raise ValueError(f"Reaction {r.name} uses unknown component(s) {unknown}")
        r.enthalpy(comp)    # fails early if the heat cannot be determined
    COMPONENTS.clear()
    COMPONENTS.update(comp)
    REACTIONS.clear()
    REACTIONS.update(rx)


def use_default_chemistry():
    set_chemistry(DEFAULT_COMPONENTS, DEFAULT_REACTIONS)


use_default_chemistry()
