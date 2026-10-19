import math

from Modules.Entity.Entity import Signal, TO_PLC, FROM_PLC
from Modules.Entity.Process_equipment.Vessel.FlashDrum.flash_drum import FlashDrum
from Modules.Flow import thermo
from Modules.Flow.chemistry import COMPONENTS

# =====================================================================
# Parent class: FlashDrum (liquid holdup + vapor space in equilibrium, heater, relief)
# Classes: Evaporator - boils solvent off a solution to concentrate it (sugar, black liquor, API solutions)
#          Dryer      - removes moisture from a wet product with hot air
#
# Both inherit all of FlashDrum: the vapor leaves through the gas ports (connect them to a condenser, which
# is just another FlashDrum with cooling, or to a gas sink), liquid ports and level work as in any vessel,
# and dissolved solids that become over-saturated settle out as solids (Vessel.update_solids), which is what
# a concentrating evaporator does to its product.
#
# Evaporator adds
#   scaling   heating-surface deposit, grows with the settled solids; it lowers the usable heater power.
#             `evaporation_rate` (mol/s) shows what the heater actually boils off. repair() cleans it.
# Dryer adds
#   AirCmd 0..100 % of the drying air: it heats the product (air_ua W/K against air_temp) and sweeps the
#   vapor out of the headspace (sweep, 1/s), which keeps the driving force for evaporation up.
#   moisture_percent  mass % of volatile liquid in the product (liquid + solids); the PLC reads it as Moisture.
#   thermal_damage    0..1 accumulated while the product is above `max_product_temp` (degradation of a
#                     heat-sensitive product: pharma, polymers). It never recovers by itself; clean() resets it.
# =====================================================================


class Evaporator(FlashDrum):
    FAULTS = ("leak_rate", "scaling")

    def __init__(self, name, description="", status=1, inlets=None, outlets=None,
                 heater_power=100e3, scaling_rate=1e-6, **flash_options):
        super().__init__(name, description, status, inlets, outlets, heater_power=heater_power, **flash_options)
        self.scaling = 0.0
        self.scaling_rate = scaling_rate
        self.evaporation_rate = 0.0     # mol/s of solvent boiled off in the last step
        self._nominal_power = heater_power

    def update(self, dt):
        before = self._volatile_liquid()
        self.heater_power = self._nominal_power * (1.0 - self.scaling)
        super().update(dt)
        self.heater_power = self._nominal_power
        self.evaporation_rate = max(0.0, before - self._volatile_liquid()) / dt

    def _volatile_liquid(self):
        return sum(n for k, n in self.contents.moles.items() if thermo.is_volatile(k))

    def slow_update(self, dt, ambient):
        super().slow_update(dt, ambient)
        if self.solids:
            self.scaling = min(1.0, self.scaling + self.scaling_rate * self.solids_percent * dt)

    def repair(self):
        super().repair()
        self.scaling = 0.0

    def signals(self):
        return super().signals() + [
            Signal("EvapRate", TO_PLC, "REAL", "mol/s", get=lambda: self.evaporation_rate)]

    def get_state(self):
        state = super().get_state()
        state["scaling"] = float(self.scaling)
        return state

    def set_state(self, state):
        super().set_state(state)
        self.scaling = state.get("scaling", 0.0)


class Dryer(FlashDrum):
    FAULTS = ("leak_rate", "thermal_damage")

    def __init__(self, name, description="", status=1, inlets=None, outlets=None,
                 air_temp=120.0, air_ua=300.0, sweep=0.5, max_product_temp=90.0, damage_rate=2e-3, **flash_options):
        super().__init__(name, description, status, inlets, outlets, **flash_options)
        self.air_temp = air_temp
        self.air_ua = air_ua
        self.sweep = sweep
        self.max_product_temp = max_product_temp
        self.damage_rate = damage_rate
        self.air_command = 0.0          # %
        self.thermal_damage = 0.0

    @property
    def moisture_percent(self):
        """Mass % of volatile liquid in the wet product (liquid plus settled solids)."""
        wet = sum(n * COMPONENTS[k].molar_mass for k, n in self.contents.moles.items() if thermo.is_volatile(k))
        dry = sum(n * COMPONENTS[k].molar_mass for k, n in self.contents.moles.items() if not thermo.is_volatile(k))
        dry += sum(n * COMPONENTS[k].molar_mass for k, n in self.solids.items())
        return 100.0 * wet / (wet + dry) if wet + dry > 0 else 0.0

    def update(self, dt):
        air = self.air_command / 100.0 if self.status else 0.0
        cp = self.contents.heat_capacity + self.gas.heat_capacity
        if air > 0 and cp > 0:
            self.temperature += self.air_ua * air * (self.air_temp - self.temperature) * dt / cp
        super().update(dt)
        if air > 0:
            fraction = 1.0 - math.exp(-self.sweep * air * dt)
            for name in [n for n in self.gas.moles if thermo.is_volatile(n)]:
                swept = self.gas.moles[name] * fraction
                self.gas.moles[name] -= swept
                self.vented.moles[name] = self.vented.moles.get(name, 0.0) + swept
        if self.temperature > self.max_product_temp and self.volume > 0:
            self.thermal_damage = min(1.0, self.thermal_damage + self.damage_rate * (self.temperature - self.max_product_temp) * dt / 10.0)

    def clean(self):
        super().clean()
        self.thermal_damage = 0.0

    def signals(self):
        return super().signals() + [
            Signal("AirCmd", FROM_PLC, "REAL", "%", get=lambda: self.air_command,
                   set=lambda v: setattr(self, "air_command", min(100.0, max(0.0, v)))),
            Signal("Moisture", TO_PLC, "REAL", "%", get=lambda: self.moisture_percent),
        ]

    def apply_failsafe(self):
        super().apply_failsafe()
        self.air_command = 0.0

    def get_state(self):
        state = super().get_state()
        state.update({"air_command": float(self.air_command), "thermal_damage": float(self.thermal_damage)})
        return state

    def set_state(self, state):
        super().set_state(state)
        self.air_command = state.get("air_command", 0.0)
        self.thermal_damage = state.get("thermal_damage", 0.0)
