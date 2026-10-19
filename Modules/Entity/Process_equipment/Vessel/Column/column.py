import math

from Modules.Entity.Entity import Signal, TO_PLC, FROM_PLC
from Modules.Entity.Process_equipment.Vessel.Vessel import Vessel
from Modules.Flow import thermo
from Modules.Flow.flow import Material

# =====================================================================
# Parent class: Vessel (the SUMP / reboiler liquid)
# Class: DistillationColumn - trays, reboiler, total condenser and reflux drum in one piece of equipment.
#
# Ports (the default names; the editor creates these):
#   feed       liquid in. Enters on tray `feed_tray` (0 = top tray) and runs down by overflow.
#   bottoms    liquid out of the sump (a normal vessel outlet).
#   distillate liquid out of the reflux drum (a separate small holdup, see `drum`).
# Any other port is treated like a sump port.
#
# Model (equilibrium stages, constant molar overflow, the usual textbook column):
#   * The reboiler boils the sump with HeaterCmd % of heater_power. Boil-up V [mol/s] = duty / latent heat.
#   * The vapor rises through the trays. On each tray it is pushed toward equilibrium with that tray's liquid
#     by the stage efficiency (Murphree): y_out = y_in + E (y_eq - y_in), y_eq from thermo.k_values (Raoult, or
#     NRTL if thermo.set_activity_model was used). The light component leaves the tray liquid and goes up,
#     the heavy one condenses. This is what makes the top rich in the light key.
#   * The top vapor is totally condensed into the reflux drum. RefluxCmd % of the vapor flow returns to the top
#     tray, the rest stays in the drum for the `distillate` outlet. A full drum forces extra reflux.
#   * Each tray holds `tray_holdup_l` litres and any excess overflows to the tray below, ending in the sump.
#     Liquid therefore falls through the column instantly; only the composition has dynamics.
# Pressure is set by `column_pressure` (bar, top); the sump is higher by trays * tray_dp.
#
# Things that go wrong (all feed the AI watchdog):
#   flooding  vapor above max_vapor (mol/s): stage efficiency collapses, pressure drop rises
#   weeping   vapor below min_vapor_fraction * max_vapor: liquid drips through, efficiency drops
#   fouling   deposit on the trays (grows with vapor load, flooding doubles it): efficiency and capacity fall.
#   scaling   deposit on the reboiler surface (grows when solids settle in the sump): duty falls.
# set_fault(tag, "fouling", 0) or repair() (cleaning) resets both.
#
# Simplifications (deliberate, replace when you need more): no feed enthalpy (the feed does not cool or heat
# the trays), one temperature per tray from its bubble point, no entrainment, a total condenser with perfect
# cooling, Murphree efficiency the same on all trays, no tray hydraulics beyond the flooding limit.
# =====================================================================

MAX_SUBSTEPS = 20


def _normalise(d):
    total = sum(d.values())
    return {k: v / total for k, v in d.items()} if total > 0 else {}


class DistillationColumn(Vessel):
    FAULTS = ("leak_rate", "fouling", "scaling")

    def __init__(self, name, description="", status=1, inlets=None, outlets=None,
                 trays=10, feed_tray=5, tray_holdup_l=5.0, column_pressure=1.013, tray_dp=0.005,
                 efficiency=0.7, heater_power=100e3, max_vapor=20.0, min_vapor_fraction=0.15,
                 drum_capacity_l=40.0, fouling_rate=2e-7, **vessel_options):
        composition = vessel_options.get("composition")
        super().__init__(name, description, status, inlets or ["feed"], outlets or ["bottoms", "distillate"],
                         **vessel_options)
        self.n_trays = int(trays)
        self.feed_tray = min(max(int(feed_tray), 0), self.n_trays - 1)
        self.tray_capacity_l = tray_holdup_l
        self.column_pressure = column_pressure
        self.tray_dp = tray_dp
        self.efficiency = efficiency
        self.heater_power = heater_power
        self.max_vapor = max_vapor
        self.min_vapor_fraction = min_vapor_fraction
        self.drum_capacity_l = drum_capacity_l
        self.fouling_rate = fouling_rate
        self.heater_command = 0.0       # % reboiler duty
        self.reflux_command = 70.0      # % of the overhead vapor returned to the top tray
        self.fouling = 0.0
        self.scaling = 0.0
        self.boilup = 0.0               # mol/s
        self.flooding = False
        self.weeping = False
        start = self.contents.temperature
        if composition:
            self.trays = [Material.from_fractions(composition, tray_holdup_l, start) for _ in range(self.n_trays)]
        else:
            self.trays = [Material({}, start) for _ in range(self.n_trays)]
        self.drum = Material({}, start)
        self.tray_temps = [start] * self.n_trays
        self.headspace_pressure = column_pressure + self.n_trays * tray_dp

    # ---- what the plant asks of a node, per port
    def _is_drum(self, port):
        return port == "distillate"

    def port_pressure(self, port):
        if self._is_drum(port):
            return self.column_pressure + 0.05 * min(1.0, self.drum.volume / self.drum_capacity_l)
        if port == "feed":
            return self.column_pressure + self.feed_tray * self.tray_dp
        return super().port_pressure(port)

    def can_supply(self, port):
        if self._is_drum(port):
            return not self.frozen
        return super().can_supply(port)

    def max_outflow_at(self, port, dt):
        return (self.drum.volume if self._is_drum(port) else self.volume) / dt

    def stream(self, port):
        return (self.drum if self._is_drum(port) else self.contents).copy()

    def draw_at(self, port, material):
        holder = self.drum if self._is_drum(port) else self.contents
        for k, n in material.moles.items():
            holder.moles[k] = max(0.0, holder.moles.get(k, 0.0) - n)

    def receive_at(self, port, material):
        if port == "feed":
            self.trays[self.feed_tray].add(material)
        else:
            self.contents.add(material)

    # ---- readings
    @property
    def top_temperature(self):
        return self.tray_temps[0]

    @property
    def top_composition(self):
        """Mole fractions on the top tray (what the overhead vapor is in equilibrium with)."""
        return self.trays[0].mole_fractions()

    @property
    def drum_composition(self):
        return self.drum.mole_fractions()

    @property
    def stage_efficiency(self):
        e = self.efficiency * (1.0 - 0.7 * self.fouling)
        if self.flooding:
            e *= 0.3
        elif self.weeping:
            e *= 0.6
        return e

    def profile(self, component):
        """Liquid mole fraction of `component` from the top tray down to the sump."""
        return [t.mole_fractions().get(component, 0.0) for t in self.trays] + \
               [self.contents.mole_fractions().get(component, 0.0)]

    # ---- physics
    def _bubble(self, x, p, guess):
        if not x:
            return guess
        t = thermo.bubble_temperature(x, p, guess - 40.0, guess + 40.0, 24)
        if t <= guess - 39.9 or t >= guess + 39.9:
            t = thermo.bubble_temperature(x, p)
        return t

    def _equilibrium_vapor(self, x, t, p):
        k = thermo.k_values(list(x), t, p, x)
        return _normalise({n: k[n] * x[n] for n in x if thermo.is_volatile(n) and x[n] > 0})

    def update(self, dt):
        p_sump = self.column_pressure + self.n_trays * self.tray_dp * (1.0 + (3.0 if self.flooding else 0.0))
        self.headspace_pressure = p_sump
        for j, tray in enumerate(self.trays):
            if tray.moles:
                self.tray_temps[j] = self._bubble(tray.mole_fractions(), self.column_pressure + j * self.tray_dp,
                                                  self.tray_temps[j])
        self.boilup = 0.0
        x_sump = self.contents.mole_fractions() if self.volume > 0 else {}
        volatile = [n for n in x_sump if thermo.is_volatile(n)]
        duty = self.heater_power * (1.0 - self.scaling) * self.heater_command / 100.0 if self.status else 0.0
        if volatile and duty > 0 and not self.frozen:
            t_sump = self._bubble(x_sump, p_sump, self.contents.temperature)
            self.contents.temperature = t_sump
            y0 = self._equilibrium_vapor(x_sump, t_sump, p_sump)
            latent = sum(y * thermo.heat_of_vaporization(n) for n, y in y0.items()) or 1.0
            # never boil off more of a component than the sump holds
            available = min(0.5 * self.contents.moles.get(n, 0.0) / (y * dt) for n, y in y0.items() if y > 0)
            vapor_rate = min(duty / latent, available)
            self.boilup = vapor_rate
            self._run_stages(vapor_rate * dt, y0)
        self._overflow()
        limit = self.max_vapor * (1.0 - 0.5 * self.fouling)
        self.flooding = self.boilup > limit
        self.weeping = 0.0 < self.boilup < self.min_vapor_fraction * limit
        if self.drum.volume > 0:
            self.drum.temperature = self.tray_temps[0]

    def _run_stages(self, vapor_moles, y0):
        top_moles = sum(self.trays[0].moles.values())
        steps = 1 if top_moles <= 0 else min(MAX_SUBSTEPS, max(1, math.ceil(vapor_moles / (0.25 * top_moles))))
        per_step = vapor_moles / steps
        efficiency = self.stage_efficiency
        for _ in range(steps):
            vapor = {n: y * per_step for n, y in y0.items()}
            self.contents.moles = {n: max(0.0, self.contents.moles.get(n, 0.0) - vapor.get(n, 0.0))
                                   for n in set(self.contents.moles) | set(vapor)}
            for j in range(self.n_trays - 1, -1, -1):
                tray = self.trays[j]
                x = tray.mole_fractions()
                total = sum(vapor.values())
                if total <= 0 or not x:
                    continue
                y_eq = self._equilibrium_vapor(x, self.tray_temps[j], self.column_pressure + j * self.tray_dp)
                out = {}
                for n in set(vapor) | set(y_eq):
                    y_in = vapor.get(n, 0.0) / total
                    y_out = y_in + efficiency * (y_eq.get(n, 0.0) - y_in)
                    gain = vapor.get(n, 0.0) - total * y_out            # moles condensing on this tray
                    gain = max(gain, -tray.moles.get(n, 0.0))
                    tray.moles[n] = tray.moles.get(n, 0.0) + gain
                    out[n] = max(0.0, vapor.get(n, 0.0) - gain)
                vapor = out
            self._condense(vapor)

    def _condense(self, vapor):
        """Totally condense the top vapor into the drum and send the reflux share back to the top tray."""
        self.drum.add(Material({n: m for n, m in vapor.items() if m > 0}, self.tray_temps[0]))
        reflux_moles = self.reflux_command / 100.0 * sum(vapor.values())
        over = self.drum.volume - self.drum_capacity_l
        drum_moles = sum(self.drum.moles.values())
        if drum_moles <= 0:
            return
        if over > 0:        # a full drum cannot hold more: the excess has to go back as extra reflux
            reflux_moles = max(reflux_moles, drum_moles * over / self.drum.volume)
        share = min(1.0, reflux_moles / drum_moles)
        back = self.drum.copy().scaled(share)
        self.drum.moles = {n: m * (1.0 - share) for n, m in self.drum.moles.items()}
        self.trays[0].add(back)

    def _overflow(self):
        carry = None
        for tray in self.trays:
            if carry is not None:
                tray.add(carry)
            excess = tray.volume - self.tray_capacity_l
            if excess > 1e-9:
                carry = tray.copy().scaled_to_volume(excess)
                tray.remove_volume(excess)
            else:
                carry = None
        if carry is not None:
            self.contents.add(carry)

    def slow_update(self, dt, ambient):
        super().slow_update(dt, ambient)
        load = min(2.0, self.boilup / self.max_vapor) if self.max_vapor > 0 else 0.0
        self.fouling = min(1.0, self.fouling + self.fouling_rate * dt * load * load * (2.0 if self.flooding else 1.0))
        if self.solids:
            self.scaling = min(1.0, self.scaling + self.fouling_rate * 5.0 * dt * self.solids_percent)

    def repair(self):
        super().repair()
        self.fouling = 0.0
        self.scaling = 0.0

    # ---- PLC
    def signals(self):
        return [
            Signal("HeaterCmd", FROM_PLC, "REAL", "%", get=lambda: self.heater_command,
                   set=lambda v: setattr(self, "heater_command", min(100.0, max(0.0, v)))),
            Signal("RefluxCmd", FROM_PLC, "REAL", "%", get=lambda: self.reflux_command,
                   set=lambda v: setattr(self, "reflux_command", min(100.0, max(0.0, v)))),
            Signal("TopTemp", TO_PLC, "REAL", "C", get=lambda: self.top_temperature),
            Signal("BottomTemp", TO_PLC, "REAL", "C", get=lambda: self.contents.temperature),
            Signal("Boilup", TO_PLC, "REAL", "mol/s", get=lambda: self.boilup),
            Signal("DrumLevel", TO_PLC, "REAL", "%", get=lambda: 100.0 * self.drum.volume / self.drum_capacity_l),
            Signal("ColumnDp", TO_PLC, "REAL", "bar", get=lambda: self.headspace_pressure - self.column_pressure),
            Signal("Flooding", TO_PLC, "BOOL", get=lambda: self.flooding),
        ]

    def apply_failsafe(self):
        self.heater_command = 0.0
        self.reflux_command = 100.0

    def get_state(self):
        state = super().get_state()
        state.update({"trays": [t.to_state() for t in self.trays], "drum": self.drum.to_state(),
                      "tray_temps": [float(t) for t in self.tray_temps],
                      "heater_command": float(self.heater_command), "reflux_command": float(self.reflux_command),
                      "fouling": float(self.fouling), "scaling": float(self.scaling)})
        return state

    def set_state(self, state):
        super().set_state(state)
        if "trays" in state:
            self.trays = [Material.from_state(s) for s in state["trays"]]
            self.drum = Material.from_state(state["drum"])
            self.tray_temps = list(state["tray_temps"])
        self.heater_command = state.get("heater_command", 0.0)
        self.reflux_command = state.get("reflux_command", 70.0)
        self.fouling = state.get("fouling", 0.0)
        self.scaling = state.get("scaling", 0.0)
