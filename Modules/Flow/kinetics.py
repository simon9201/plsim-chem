import math

from Modules.Flow.chemistry import COMPONENTS

# =====================================================================
# Kinetics: advance ALL the reactions of a vessel together over one time step.
#
# State = moles of every component + temperature. For every reaction the rate [mol/(L*s)] comes from
# Reaction.rate (Arrhenius, reaction orders, reversible reactions relaxing to equilibrium), multiplied by the
# liquid volume it gives an extent rate. Then
#     dn_i/dt = sum_j nu_ij * extent_rate_j
#     dT/dt   = sum_j ( -dH_j(T) * extent_rate_j ) / Cp(n)          (adiabatic: cooling is applied by the vessel)
# where dH_j(T) is the heat of reaction at the current temperature (Reaction.enthalpy_at).
# Solving everything at once matters: reactions that share components (consecutive, competing, reversible)
# give wrong answers if they are run one after the other. The step is classical RK4, split into smaller
# steps when a component would be used up quickly or the temperature would jump (stiff or runaway cases),
# and moles are never allowed to go negative.
# Both the material balance (the moles) and the energy balance (the temperature) are exact for the model:
# see tests/test_chemistry_lab.py.
#
# advance() changes `moles` in place and returns (new temperature in C, heat released in J).
# =====================================================================

MAX_FRACTION_PER_SUBSTEP = 0.1      # a substep may change a component by at most this fraction
MAX_TEMPERATURE_STEP = 2.0          # K, largest temperature change in one substep
MAX_SUBSTEPS = 400
TINY = 1e-12


def _volume(moles):
    return sum(n * COMPONENTS[k].molar_volume for k, n in moles.items())


def _derivatives(moles, temp_c, reactions):
    """(dn/dt dict, dT/dt in K/s, heat rate in W, stiffness in 1/s). Stiffness is the fastest gross
    consumption rate of any component, which sets how small a safe substep is."""
    volume = _volume(moles)
    if volume <= 0:
        return {}, 0.0, 0.0, 0.0
    conc = {k: n / volume for k, n in moles.items()}
    dn, heat, stiff = {}, 0.0, 0.0
    for rx in reactions:
        forward, backward = rx.rate_parts(conc, temp_c)
        net = (forward - backward) * volume
        if forward == 0.0 and backward == 0.0:
            continue
        for c, nu in rx.stoichiometry.items():
            dn[c] = dn.get(c, 0.0) + nu * net
            n = moles.get(c, 0.0)
            if n > TINY:
                gross = (forward if nu < 0 else backward) * volume * abs(nu)
                stiff = max(stiff, gross / n)
        heat += -rx.enthalpy_at(temp_c) * net
    cp = sum(n * COMPONENTS[k].cp for k, n in moles.items())
    return dn, (heat / cp if cp > 0 else 0.0), heat, stiff


def _shifted(moles, dn, h):
    out = dict(moles)
    for c, d in dn.items():
        out[c] = max(0.0, out.get(c, 0.0) + d * h)
    return out


def _rk4(moles, temp_c, reactions, h):
    k1 = _derivatives(moles, temp_c, reactions)
    k2 = _derivatives(_shifted(moles, k1[0], h / 2), temp_c + k1[1] * h / 2, reactions)
    k3 = _derivatives(_shifted(moles, k2[0], h / 2), temp_c + k2[1] * h / 2, reactions)
    k4 = _derivatives(_shifted(moles, k3[0], h), temp_c + k3[1] * h, reactions)
    new = dict(moles)
    for c in set(k1[0]) | set(k2[0]) | set(k3[0]) | set(k4[0]):
        d = (k1[0].get(c, 0.0) + 2 * k2[0].get(c, 0.0) + 2 * k3[0].get(c, 0.0) + k4[0].get(c, 0.0)) / 6.0
        new[c] = max(0.0, new.get(c, 0.0) + d * h)
    d_temp = (k1[1] + 2 * k2[1] + 2 * k3[1] + k4[1]) / 6.0
    q = (k1[2] + 2 * k2[2] + 2 * k3[2] + k4[2]) / 6.0
    return new, temp_c + d_temp * h, q * h


def advance(moles, temp_c, reactions, dt):
    """Run `reactions` (a list of Reaction) on `moles` for dt seconds. Returns (temperature C, heat released J)."""
    if not reactions or dt <= 0:
        return temp_c, 0.0
    state, temp, heat, t, count = dict(moles), temp_c, 0.0, 0.0, 0
    while t < dt - 1e-12:
        dn, d_temp, _, stiff = _derivatives(state, temp, reactions)
        if not dn:
            break                                           # nothing can react right now
        h = dt - t
        if stiff > 0:
            h = min(h, MAX_FRACTION_PER_SUBSTEP / stiff)
        if d_temp != 0.0:
            h = min(h, MAX_TEMPERATURE_STEP / abs(d_temp))
        count += 1
        if count >= MAX_SUBSTEPS:
            h = dt - t                                      # out of budget: finish stably (moles are clamped)
        state, temp, q = _rk4(state, temp, reactions, min(h, dt - t))
        heat += q
        t += h
    moles.clear()
    moles.update(state)
    return temp, heat
