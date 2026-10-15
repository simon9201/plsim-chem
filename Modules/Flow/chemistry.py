import math
from dataclasses import dataclass, field
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
    attributes: dict = field(default_factory=dict)

    @property
    def molar_volume(self):
        """Liter per mol"""
        return self.molar_mass / self.density


@dataclass
class Reaction:
    """A reaction with its stoichiometry, rate law and heat.

    stoichiometry: {component: coefficient}, negative = consumed, positive = produced.
    orders: reaction order per component in the rate law. Defaults to the reactant coefficients.
    rate [mol/(L*s)] = k(T) * product(concentration ** order)
    heat_of_reaction: J per mol of reaction extent (negative = exothermic).
        If left out it is calculated from the components' enthalpies of formation.
    """
    name: str
    stoichiometry: dict
    k_ref: float                # rate constant at 25 C, in units that match the orders
    activation_energy: float    # J/mol
    orders: Optional[dict] = None
    heat_of_reaction: Optional[float] = None
    description: str = ""

    def __post_init__(self):
        if not any(v < 0 for v in self.stoichiometry.values()) or not any(v > 0 for v in self.stoichiometry.values()):
            raise ValueError(f"Reaction {self.name} needs at least one reactant (negative) and one product (positive)")
        if not self.orders:
            self.orders = {c: -nu for c, nu in self.stoichiometry.items() if nu < 0}

    def rate_constant(self, temp_c):
        temp_k = max(temp_c + 273.15, 1.0)
        return self.k_ref * math.exp(-self.activation_energy / R_GAS * (1.0 / temp_k - 1.0 / T_REF_K))

    def enthalpy(self, components=None):
        """J per mol of reaction extent."""
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


DEFAULT_COMPONENTS = (
    Component("water", 18.015, 997.0, 75.3, -285830.0, {"role": "solvent", "boiling_point_c": 100.0}),
    Component("A", 60.0, 1000.0, 120.0, -100e3, {"role": "reactant"}),
    Component("B", 46.0, 800.0, 110.0, -50e3, {"role": "reactant"}),
    Component("C", 106.0, 900.0, 190.0, -290e3, {"role": "product"}),
)

DEFAULT_REACTIONS = (
    # Heat of reaction follows from the formation enthalpies: -290 - (-100) - (-50) = -140 kJ/mol
    Reaction("A_B_to_C", {"A": -1, "B": -1, "C": 1}, k_ref=0.01, activation_energy=50e3,
             description="A + B -> C, exothermic, first order in each reactant"),
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
