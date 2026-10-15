from Modules.Flow.chemistry import COMPONENTS, R_GAS    # noqa: F401  (re-exported for convenience)

# =====================================================================
# Material: an amount of liquid, tracked as moles per component plus a
# temperature. Volume, concentration, density and heat capacity are derived
# from that, assuming ideal mixing. Vessels hold a Material; streams are a
# Material used as a rate (mol/s).
# =====================================================================


def _check_names(names):
    unknown = [n for n in names if n not in COMPONENTS]
    if unknown:
        raise KeyError(f"Unknown component(s) {unknown}. Known: {sorted(COMPONENTS)}")


class Material:
    """moles: {component: mol} (or mol/s for a stream), temperature in C."""

    def __init__(self, moles=None, temperature=25.0):
        self.moles = dict(moles or {})
        _check_names(self.moles)
        self.temperature = temperature

    @classmethod
    def from_fractions(cls, fractions, volume, temperature=25.0):
        """Build a material of a given volume (L) from mole fractions (normalized)."""
        _check_names(fractions)
        total = sum(fractions.values())
        if total <= 0:
            raise ValueError("Composition must have a positive total")
        x = {k: v / total for k, v in fractions.items()}
        mixture_molar_volume = sum(x[k] * COMPONENTS[k].molar_volume for k in x)
        n_total = volume / mixture_molar_volume
        return cls({k: n_total * v for k, v in x.items()}, temperature)

    @property
    def volume(self):
        """Liter"""
        return sum(n * COMPONENTS[k].molar_volume for k, n in self.moles.items())

    @property
    def mass(self):
        """Gram"""
        return sum(n * COMPONENTS[k].molar_mass for k, n in self.moles.items())

    @property
    def density(self):
        """g/L, 0 when empty"""
        volume = self.volume
        return self.mass / volume if volume > 0 else 0.0

    @property
    def heat_capacity(self):
        """J/K"""
        return sum(n * COMPONENTS[k].cp for k, n in self.moles.items())

    def concentration(self, name):
        """mol/L"""
        volume = self.volume
        return self.moles.get(name, 0.0) / volume if volume > 0 else 0.0

    def mole_fractions(self):
        total = sum(self.moles.values())
        return {k: n / total for k, n in self.moles.items()} if total > 0 else {}

    def mass_fractions(self):
        total = self.mass
        return {k: n * COMPONENTS[k].molar_mass / total for k, n in self.moles.items()} if total > 0 else {}

    def attribute_average(self, attribute):
        """Mole-fraction weighted average of a numeric component attribute, for example 'boiling_point_c'.
        Components that do not have the attribute are ignored. None if no component has it."""
        total, weight = 0.0, 0.0
        for name, x in self.mole_fractions().items():
            value = COMPONENTS[name].attributes.get(attribute)
            if isinstance(value, (int, float)):
                total += x * value
                weight += x
        return total / weight if weight > 0 else None

    def copy(self):
        return Material(self.moles, self.temperature)

    def scaled(self, factor):
        return Material({k: n * factor for k, n in self.moles.items()}, self.temperature)

    def scaled_to_volume(self, volume):
        """Same composition and temperature, but with the given volume."""
        current = self.volume
        return self.scaled(volume / current if current > 0 else 0.0)

    def add(self, other):
        """Mix another material in (energy balance on temperature)."""
        cp_self, cp_other = self.heat_capacity, other.heat_capacity
        if cp_self + cp_other > 0:
            self.temperature = (cp_self * self.temperature + cp_other * other.temperature) / (cp_self + cp_other)
        for k, n in other.moles.items():
            self.moles[k] = self.moles.get(k, 0.0) + n

    def remove_volume(self, volume):
        """Take out a volume (L) with the current composition. Returns what was removed."""
        current = self.volume
        if current <= 0 or volume <= 0:
            return Material({}, self.temperature)
        fraction = min(1.0, volume / current)
        removed = self.scaled(fraction)
        self.moles = {k: n * (1.0 - fraction) for k, n in self.moles.items()}
        return removed

    def to_state(self):
        return {"moles": {k: float(n) for k, n in self.moles.items()}, "temperature": float(self.temperature)}

    @classmethod
    def from_state(cls, state):
        return cls(state["moles"], state["temperature"])
