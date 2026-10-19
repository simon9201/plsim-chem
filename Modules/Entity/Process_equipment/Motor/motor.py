import math

# =====================================================================
# Motor: the electrical drive of a pump or compressor (used by composition: `equipment.motor`).
#
# Every step the equipment gives the motor its mechanical load (0..1 of rated, can exceed 1) and the
# motor works out what the electrical network does to it:
#
#   current      I = load * I_rated / V          (constant-power load: a sagging supply draws MORE current)
#                above nominal voltage the iron saturates and the current climbs too
#   winding temp first-order lag (`thermal_tau`) toward  ambient + temp_rise_rated * (I / I_rated)^2
#   speed        below 80 % voltage the motor cannot hold speed (speed_factor() falls to 0 at the 70 % trip point);
#                above that it keeps its speed and simply draws more current, which is what overheats it
#   trips        undervoltage below 70 % (contactor drops out), overtemperature above `trip_temp`
#                (thermal overload relay). Both clear with the PLC's ResetCmd.
#   insulation   ages every slow step by the 10-degree rule: life halves for every 10 C above
#                `insulation_ref_temp`. At 100 % used the winding burns out: `failed` is permanent,
#                ResetCmd does nothing, only repair() (a rewind / new motor) clears it.
#   vibration    mm/s, base level plus whatever the driven machine adds (wear, cavitation, surge).
#
# A worn-out insulation also draws extra current (leakage / turn-to-turn shorts), which is the kind of
# slow drift a predictive watchdog can learn to spot before the stop.
# The numbers are illustrative, not from a motor datasheet.
# =====================================================================

UNDERVOLTAGE_TRIP = 0.70
SPEED_DERATE_BELOW = 0.80       # a motor holds speed through a moderate sag (and overheats); below this it loses it
SATURATION_GAIN = 3.0           # extra current per unit of over-voltage
INSULATION_CURRENT_GAIN = 0.25  # extra current at 100 % insulation used
BASE_VIBRATION = 0.8            # mm/s of a healthy running machine


class Motor:
    def __init__(self, rated_kw=2.2, rated_voltage=400.0, efficiency=0.9, power_factor=0.85,
                 temp_rise_rated=80.0, thermal_tau=600.0, trip_temp=140.0,
                 insulation_life_h=4.0e4, insulation_ref_temp=105.0):
        self.rated_kw = rated_kw
        self.rated_current = rated_kw * 1000.0 / (math.sqrt(3.0) * rated_voltage * efficiency * power_factor)
        self.temp_rise_rated = temp_rise_rated
        self.thermal_tau = thermal_tau
        self.trip_temp = trip_temp
        self.insulation_life_h = insulation_life_h
        self.insulation_ref_temp = insulation_ref_temp
        self.supply = None              # PowerSupply, attached by PlantModel.add_equipment
        self.ambient = 20.0             # C, updated from the weather by slow_update
        self.current = 0.0              # A
        self.winding_temp = self.ambient
        self.insulation_used = 0.0      # 0 = new, 1 = burnt out
        self.failed = False
        self.trip_reason = ""
        self.vibration = 0.0            # mm/s

    @property
    def voltage_factor(self):
        return self.supply.voltage_factor if self.supply is not None else 1.0

    @property
    def voltage(self):
        return self.supply.voltage if self.supply is not None else 400.0

    def speed_factor(self):
        """Fraction of the commanded speed the motor can hold at the present voltage: full at 80 %, zero at the 70 % trip point."""
        v = self.voltage_factor
        if v >= SPEED_DERATE_BELOW:
            return 1.0
        return max(0.0, (v - UNDERVOLTAGE_TRIP) / (SPEED_DERATE_BELOW - UNDERVOLTAGE_TRIP))

    def update(self, dt, load, running, extra_vibration=0.0):
        """Advance the electrical and thermal state. Returns a trip reason ("" when fine)."""
        v = self.voltage_factor
        if running and v > 0.05:
            amps = load * self.rated_current / v if v <= 1.0 else load * self.rated_current * (1.0 + SATURATION_GAIN * (v - 1.0))
            self.current = amps * (1.0 + INSULATION_CURRENT_GAIN * self.insulation_used ** 2)
        else:
            self.current = 0.0
        rel = self.current / self.rated_current
        target = self.ambient + self.temp_rise_rated * rel * rel
        self.winding_temp += (target - self.winding_temp) * (1.0 - math.exp(-dt / self.thermal_tau))
        self.vibration = BASE_VIBRATION + extra_vibration if running else 0.0

        reason = ""
        if self.failed or self.insulation_used >= 1.0:
            self.failed = True
            reason = "burnout"
        elif running and v < UNDERVOLTAGE_TRIP:
            reason = "undervoltage"
        elif self.winding_temp > self.trip_temp:
            reason = "overtemperature"
        if reason:
            self.trip_reason = reason
        return reason

    def slow_update(self, dt, ambient):
        """Insulation ageing (10-degree rule). dt is slow-clock seconds, so slow_time_scale accelerates it."""
        self.ambient = ambient
        if self.failed:
            return
        speedup = 2.0 ** ((self.winding_temp - self.insulation_ref_temp) / 10.0)
        self.insulation_used = min(1.0, self.insulation_used + dt * speedup / (self.insulation_life_h * 3600.0))

    def reset(self):
        """The PLC's ResetCmd. Returns True if the trip was cleared (a burnt-out motor cannot be reset)."""
        if self.failed:
            return False
        self.trip_reason = ""
        return True

    def repair(self):
        """Rewind / replace the motor windings: new insulation, cold winding, burnout cleared."""
        self.insulation_used = 0.0
        self.failed = False
        self.trip_reason = ""
        self.winding_temp = min(self.winding_temp, self.ambient + 5.0)

    def get_state(self):
        return {"current": float(self.current), "winding_temp": float(self.winding_temp),
                "insulation_used": float(self.insulation_used), "failed": bool(self.failed),
                "trip_reason": self.trip_reason, "ambient": float(self.ambient)}

    def set_state(self, state):
        self.ambient = state.get("ambient", 20.0)
        self.current = state.get("current", 0.0)
        self.winding_temp = state.get("winding_temp", self.ambient)
        self.insulation_used = state.get("insulation_used", 0.0)
        self.failed = state.get("failed", False)
        self.trip_reason = state.get("trip_reason", "")
