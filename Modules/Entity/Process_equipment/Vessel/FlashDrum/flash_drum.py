import math

from Modules.Entity.Entity import Signal, TO_PLC, FROM_PLC
from Modules.Entity.Process_equipment.Gas.gas import GasHolder
from Modules.Entity.Process_equipment.Vessel.Vessel import Vessel
from Modules.Flow import thermo
from Modules.Flow.chemistry import COMPONENTS, R_GAS    # noqa: F401
from Modules.Flow.flow import Gas, gas_pressure_factor

# =====================================================================
# Parent classes: GasHolder (the vapor space), Vessel (the liquid holdup)
# Class: FlashDrum - a two-phase vessel: separator, knock-out drum, condenser/receiver, boiler.
#
# The liquid sits in the bottom as in any Vessel (liquid ports, hydraulics unchanged). The space above
# it is a gas holdup (`gas`) whose pressure is n R T / V and which becomes the vessel's
# headspace_pressure, so a pump suction below it sees the real vessel pressure.
# Ports named "gas..." or "vapor..." are connected in the gas network; all other ports are liquid.
# E.g. inlets=["in", "vapor_in"], outlets=["out", "vapor_out"].
#
# Each step, for every volatile component, vapor and liquid relax toward equilibrium:
#     partial pressure in the vapor  ->  x_i * gamma_i * Psat_i(T)       (modified Raoult, thermo.py)
# at rate `mass_transfer` (1/s). Evaporation takes latent heat from the liquid (it cools), condensation
# gives it back. Permanent gases (noncondensable) stay in the vapor space. Boiling therefore happens
# by itself when heat is added or pressure drops; a closed drum builds pressure, an open one vents vapor.
# The relaxation is only fast-enough-to-be-stable bookkeeping: the equilibrium it converges to is the
# same one thermo.flash_tp gives, which a distillation stage can use directly later.
#
# Heating/cooling: HeaterCmd (0..100 %) of heater_power (W) and CoolingCmd (0..100 %) of cooling_ua (W/K)
# against coolant_temp. relief_pressure (bar) vents excess vapor (see `vented`).
# =====================================================================

MIN_GAS_VOLUME_FRACTION = 0.01      # the vapor space never shrinks below this fraction of the vessel


class FlashDrum(GasHolder, Vessel):
    def __init__(self, name, description="", status=1, inlets=None, outlets=None,
                 headspace_gas=None, mass_transfer=5.0, heater_power=0.0, cooling_ua=0.0, coolant_temp=15.0,
                 relief_pressure=None, **vessel_options):
        super().__init__(name, description, status, inlets, outlets, **vessel_options)
        self.split_gas_ports()
        self.mass_transfer = mass_transfer
        self.heater_power = heater_power
        self.cooling_ua = cooling_ua
        self.coolant_temp = coolant_temp
        self.relief_pressure = relief_pressure
        self.heater_command = 0.0       # %
        self.cooling_command = 0.0      # %
        self.heat_duty = 0.0            # W net into the contents during the last step
        self.relieving = False
        if headspace_gas is None:
            headspace_gas = {"N2": 1.0} if "N2" in COMPONENTS else {}
        # Fill the vapor space with the inert gas at the starting headspace_pressure
        volume = self.gas_volume_l()
        self.gas = (Gas.at_pressure(headspace_gas, self.headspace_pressure, volume, self.temperature)
                    if headspace_gas else Gas({}, self.temperature))
        self.vented = Gas({}, self.temperature)

    # ---- gas side
    def gas_volume_l(self):
        free = self.max_capacity - self.volume - self.solids_volume
        return max(free, MIN_GAS_VOLUME_FRACTION * self.max_capacity)

    def gas_pressure(self, port):
        return self.headspace_pressure

    def can_supply_gas(self, port):
        return bool(self.status) and not self.frozen

    def can_accept_gas(self, port):
        return bool(self.status)

    @property
    def vapor_fraction(self):
        """mol of vapor per mol of everything in the drum"""
        total = self.gas.total_moles + sum(self.contents.moles.values())
        return self.gas.total_moles / total if total > 0 else 0.0

    # ---- per-step physics
    def update(self, dt):
        volume = self.gas_volume_l()
        self.gas.temperature = self.temperature     # vapor and liquid share one temperature
        factor = gas_pressure_factor(self.temperature)
        x = self.contents.mole_fractions()
        gamma = thermo.activity_coefficients(x, self.temperature) if x else {}
        relax = 1.0 - math.exp(-self.mass_transfer * dt)
        latent = 0.0                    # J absorbed by evaporation (negative = released by condensation)
        for name in set(self.contents.moles) | set(self.gas.moles):
            if not thermo.is_volatile(name):
                continue
            p_eq = x.get(name, 0.0) * gamma.get(name, 1.0) * thermo.psat(name, self.temperature)
            target = p_eq * volume / factor                 # mol of vapor in equilibrium with the liquid
            moved = (target - self.gas.moles.get(name, 0.0)) * relax
            moved = min(moved, self.contents.moles.get(name, 0.0))      # cannot evaporate more than the liquid has
            moved = max(moved, -self.gas.moles.get(name, 0.0))
            self.contents.moles[name] = self.contents.moles.get(name, 0.0) - moved
            self.gas.moles[name] = self.gas.moles.get(name, 0.0) + moved
            latent += moved * thermo.heat_of_vaporization(name)

        heater = self.heater_power * self.heater_command / 100.0 if self.status else 0.0
        cooling = self.cooling_ua * self.cooling_command / 100.0 * (self.temperature - self.coolant_temp) if self.status else 0.0
        self.heat_duty = heater - cooling - latent / dt
        cp = self.contents.heat_capacity + self.gas.heat_capacity
        if cp > 0:
            self.temperature += self.heat_duty * dt / cp
            self.gas.temperature = self.temperature

        self.relieving = False
        pressure = self.gas.pressure(volume)
        if self.relief_pressure is not None and pressure > self.relief_pressure:
            excess = (pressure - self.relief_pressure) * self.gas_buffer()
            lost = self.gas.scaled_to_moles(min(excess, self.gas.total_moles))
            self.gas.remove(lost)
            self.vented.add(lost)
            self.relieving = True
            pressure = self.gas.pressure(volume)
        self.headspace_pressure = pressure

    def slow_update(self, dt, ambient):
        cp = self.contents.heat_capacity + self.gas.heat_capacity
        if self.ambient_ua > 0 and cp > 0:
            self.temperature = ambient + (self.temperature - ambient) * math.exp(-self.ambient_ua * dt / cp)
            self.gas.temperature = self.temperature

    # ---- PLC
    def signals(self):
        return [
            Signal("HeaterCmd", FROM_PLC, "REAL", "%", get=lambda: self.heater_command,
                   set=lambda v: setattr(self, "heater_command", min(100.0, max(0.0, v)))),
            Signal("CoolingCmd", FROM_PLC, "REAL", "%", get=lambda: self.cooling_command,
                   set=lambda v: setattr(self, "cooling_command", min(100.0, max(0.0, v)))),
        ]

    def apply_failsafe(self):
        self.heater_command = 0.0
        self.cooling_command = 100.0 if self.cooling_ua > 0 else 0.0

    def get_state(self):
        state = super().get_state()
        state.update({"gas": self.gas.to_state(), "vented": self.vented.to_state(),
                      "heater_command": float(self.heater_command), "cooling_command": float(self.cooling_command),
                      "headspace_pressure": float(self.headspace_pressure)})
        return state

    def set_state(self, state):
        super().set_state(state)
        self.gas = Gas.from_state(state["gas"])
        self.vented = Gas.from_state(state["vented"])
        self.heater_command = state.get("heater_command", 0.0)
        self.cooling_command = state.get("cooling_command", 0.0)
        self.headspace_pressure = state.get("headspace_pressure", self.headspace_pressure)
