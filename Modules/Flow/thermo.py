import math

from Modules.Flow.chemistry import COMPONENTS, R_GAS

# =====================================================================
# Thermodynamics: the one place where phase equilibrium and enthalpy are calculated.
#
# Everything here is a pure function of (composition, temperature, pressure) and the
# component data in COMPONENTS, so equipment (flash drums, later distillation stages)
# only asks questions and never carries thermo formulas of its own.
# Units: C, bar, J, mol. Compositions are dicts {component: moles or mole fraction}.
#
# Component attributes used (all optional, stored in Component.attributes / the DB JSON column):
#   antoine_a, antoine_b, antoine_c   log10(Psat[bar]) = A - B / (T[K] + C)
#   boiling_point_c                   normal boiling point; Psat falls back to Clausius-Clapeyron
#                                     through it when no Antoine constants are given
#   heat_of_vaporization              J/mol, taken as constant (falls back to Trouton's rule)
#   cp_gas                            J/(mol*K), ideal-gas heat capacity (default 29.1)
#   noncondensable                    true for permanent gases (N2, air): always vapor, never in the liquid
# A component with none of these is treated as NON-VOLATILE (salts, heavy products).
#
# What is deliberately simple, and where to improve it later:
#   * The liquid is ideal: activity coefficients are 1. `set_activity_model` swaps in a real model
#     (NRTL, Wilson, ...) without touching any equipment.
#   * The vapor is an ideal gas (no fugacity coefficients).
#   * Latent heat does not vary with temperature (Watson correlation would go in heat_of_vaporization).
# =====================================================================

P_ATM_BAR = 1.01325
TROUTON = 10.58                 # dHvap / (R * Tb), dimensionless
DEFAULT_CP_GAS = 29.1           # J/(mol*K)
T_REF_C = 25.0                  # enthalpy reference: pure liquid at 25 C = 0
INFINITE_K = 1e6                # K-value of a permanent gas


def _attrs(name):
    return COMPONENTS[name].attributes


def is_noncondensable(name):
    return bool(_attrs(name).get("noncondensable"))


def is_volatile(name):
    """True if the component can exist in both phases (it has vapor pressure data)."""
    a = _attrs(name)
    return not is_noncondensable(name) and ("antoine_a" in a or "boiling_point_c" in a)


def heat_of_vaporization(name):
    """J/mol (0 for components that do not evaporate)."""
    a = _attrs(name)
    if "heat_of_vaporization" in a:
        return float(a["heat_of_vaporization"])
    if "boiling_point_c" in a:
        return TROUTON * R_GAS * (a["boiling_point_c"] + 273.15)
    return 0.0


def cp_gas(name):
    return float(_attrs(name).get("cp_gas", DEFAULT_CP_GAS))


def psat(name, t_c):
    """Pure-component vapor pressure in bar (0 if the component is not volatile)."""
    a = _attrs(name)
    t_k = max(t_c + 273.15, 1.0)
    if "antoine_a" in a:
        return 10.0 ** (a["antoine_a"] - a["antoine_b"] / (t_k + a.get("antoine_c", 0.0)))
    if "boiling_point_c" in a and not is_noncondensable(name):
        tb_k = a["boiling_point_c"] + 273.15
        return P_ATM_BAR * math.exp(heat_of_vaporization(name) / R_GAS * (1.0 / tb_k - 1.0 / t_k))
    return 0.0


# ---------------------------------------------------------------- activity model hook
def _ideal_activity(x, t_c):
    return {k: 1.0 for k in x}


_activity_model = _ideal_activity


def set_activity_model(model=None):
    """Replace the liquid-phase activity coefficients. `model(x, t_c) -> {component: gamma}`.
    Call with no argument to return to the ideal liquid."""
    global _activity_model
    _activity_model = model or _ideal_activity


def activity_coefficients(x, t_c):
    return _activity_model(x, t_c)


# ---------------------------------------------------------------- equilibrium
def _normalize(comp):
    total = sum(v for v in comp.values() if v > 0)
    return {k: v / total for k, v in comp.items() if v > 0} if total > 0 else {}


def k_values(names, t_c, p_bar, x=None):
    """K = y/x for each component: gamma * Psat / P (modified Raoult). Permanent gases get a huge K,
    non-volatile components zero."""
    gamma = activity_coefficients(x or {n: 1.0 for n in names}, t_c)
    k = {}
    for n in names:
        if is_noncondensable(n):
            k[n] = INFINITE_K
        elif is_volatile(n):
            k[n] = gamma.get(n, 1.0) * psat(n, t_c) / max(p_bar, 1e-9)
        else:
            k[n] = 0.0
    return k


def bubble_pressure(x, t_c):
    """Pressure (bar) at which a liquid of composition x starts to boil."""
    x = _normalize(x)
    gamma = activity_coefficients(x, t_c)
    return sum(gamma.get(n, 1.0) * xi * psat(n, t_c) for n, xi in x.items() if is_volatile(n))


def dew_pressure(y, t_c):
    """Pressure (bar) at which a vapor of composition y starts to condense."""
    y = _normalize(y)
    total = sum(yi / psat(n, t_c) for n, yi in y.items() if is_volatile(n) and psat(n, t_c) > 0)
    return 1.0 / total if total > 0 else 0.0


def _bisect(f, lo, hi, iterations=80):
    """Root of an increasing function f on [lo, hi]. Returns the bound if there is no sign change."""
    if f(lo) >= 0:
        return lo
    if f(hi) <= 0:
        return hi
    for _ in range(iterations):
        mid = 0.5 * (lo + hi)
        if f(mid) < 0:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def bubble_temperature(x, p_bar, t_min=-100.0, t_max=800.0):
    """Temperature (C) at which a liquid of composition x boils at pressure p."""
    x = _normalize(x)
    return _bisect(lambda t: sum(k * x.get(n, 0.0) for n, k in k_values(x, t, p_bar, x).items()) - 1.0, t_min, t_max)


def dew_temperature(y, p_bar, t_min=-100.0, t_max=800.0):
    """Temperature (C) at which a vapor of composition y starts to condense at pressure p."""
    y = _normalize(y)

    def f(t):
        k = k_values(y, t, p_bar, y)
        return 1.0 - sum(yi / k[n] for n, yi in y.items() if k[n] > 0)
    return _bisect(f, t_min, t_max)


def flash_tp(z, t_c, p_bar):
    """Isothermal flash (Rachford-Rice). Returns (beta, x, y): vapor fraction (mol/mol) and the
    liquid and vapor mole fractions. beta 0 = all liquid (x = z), 1 = all vapor."""
    z = _normalize(z)
    if not z:
        return 0.0, {}, {}
    k = k_values(z, t_c, p_bar, z)

    def rr(beta):
        return sum(zi * (k[n] - 1.0) / max(1.0 + beta * (k[n] - 1.0), 1e-15) for n, zi in z.items())

    if rr(0.0) <= 0:
        beta = 0.0
    elif rr(1.0 - 1e-12) >= 0:
        beta = 1.0
    else:
        lo, hi = 0.0, 1.0
        for _ in range(100):
            mid = 0.5 * (lo + hi)
            if rr(mid) > 0:
                lo = mid
            else:
                hi = mid
        beta = 0.5 * (lo + hi)
    x = _normalize({n: zi / (1.0 + beta * (k[n] - 1.0)) for n, zi in z.items()}) if beta < 1.0 else {}
    y = _normalize({n: k[n] * x[n] for n in x}) if beta < 1.0 else dict(z)
    if beta == 0.0:
        y = {}
    if beta == 1.0:
        x = {}
    return beta, x, y


# ---------------------------------------------------------------- enthalpy
def liquid_enthalpy(name, t_c):
    """J/mol relative to the pure liquid at 25 C."""
    return COMPONENTS[name].cp * (t_c - T_REF_C)


def vapor_enthalpy(name, t_c):
    """J/mol: the liquid enthalpy at the same temperature plus the latent heat."""
    return liquid_enthalpy(name, t_c) + heat_of_vaporization(name)


def mixture_enthalpy(moles, t_c, phase):
    """J for the given moles in one phase ('liquid' or 'vapor')."""
    h = liquid_enthalpy if phase == "liquid" else vapor_enthalpy
    return sum(n * h(name, t_c) for name, n in moles.items())


def flash_ph(moles, enthalpy, p_bar, t_min=-100.0, t_max=800.0):
    """Adiabatic flash: find the temperature at which the feed (moles, total enthalpy J relative to
    liquid at 25 C) is in equilibrium at pressure p. Returns (T, beta, x, y)."""
    def total(t):
        beta, x, y = flash_tp(moles, t, p_bar)
        n = sum(moles.values())
        return (1 - beta) * mixture_enthalpy({k: v * n for k, v in x.items()} if x else {}, t, "liquid") + \
            beta * mixture_enthalpy({k: v * n for k, v in y.items()} if y else {}, t, "vapor")

    t = _bisect(lambda t: total(t) - enthalpy, t_min, t_max)
    return (t,) + flash_tp(moles, t, p_bar)
