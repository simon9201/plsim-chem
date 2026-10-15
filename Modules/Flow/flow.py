from dataclasses import dataclass

# =====================================================================
# Material model for liquids: what a stream or vessel content consists of.
# Amounts are tracked as moles per component; volume, concentration and
# heat capacity are derived from that. Ideal mixing is assumed.
# =====================================================================

R_GAS = 8.314  # J/(mol*K)


@dataclass(frozen=True)
class Component:
    name: str
    molar_mass: float   # g/mol
    density: float      # g/L (pure liquid)
    cp: float           # J/(mol*K) (liquid heat capacity)

    @property
    def molar_volume(self):
        """Liter per mol"""
        return self.molar_mass / self.density


# Placeholder test chemistry: A + B -> C in water.
COMPONENTS = {c.name: c for c in (
    Component("water", 18.015, 997.0, 75.3),
    Component("A", 60.0, 1000.0, 120.0),
    Component("B", 46.0, 800.0, 110.0),
    Component("C", 106.0, 900.0, 190.0),
)}


def _check_names(names):
    unknown = [n for n in names if n not in COMPONENTS]
    if unknown:
        raise KeyError(f"Unknown component(s) {unknown}. Known: {sorted(COMPONENTS)}")


class Material:
    """An amount of liquid (moles per component, temperature in C).
    The same class is used as a rate (mol/s) for streams; temperature is then the stream temperature."""

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
    def heat_capacity(self):
        """J/K"""
        return sum(n * COMPONENTS[k].cp for k, n in self.moles.items())

    def concentration(self, name):
        """mol/L"""
        volume = self.volume
        return self.moles.get(name, 0.0) / volume if volume > 0 else 0.0

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
