from Modules.Entity.Entity import Signal, TO_PLC, FROM_PLC
from Modules.Entity.Process_equipment.Motor.motor import Motor
from Modules.Entity.Process_equipment.transfer import Transfer

# =====================================================================
# Parent class: Transfer
# Class: Pump (centrifugal, variable speed)
#
# Head at speed n (0..1):  H = shutoff_head * n^2  -  loss * Q^2      (affinity laws)
# shutoff_head = bar at zero flow, max_flow = L/s at zero head at full speed.
# A pump only pushes forward and blocks the line when stopped.
# It trips on dry run (nothing at its inlet for `dry_run_trip` s) until ResetCmd.
#
# Cavitation: when the pressure at the inlet drops below the liquid's vapor pressure plus
# `npsh_required` (bar), the pump cavitates. Severity (0..1) grows with the shortfall, cuts the
# head by up to 60 % and, if it lasts (`cavitation_trip` severity-seconds), trips the pump.
# Typical causes: closed or throttled suction valve, low tank level, hot liquid, blocked suction line.
#
# Electrical drive: `self.motor` (Motor, see Motor/motor.py) feels the plant's PowerSupply. A sagging
# supply makes the pump draw more current and run hotter, it loses speed below 90 % voltage, and it
# trips on undervoltage / overtemperature. Long hot running ages the insulation until the motor burns
# out (permanent until repair). Signals: Current, WindingTemp, Voltage, Vibration.
# Optional protection relays (off by default): `low_flow_trip_time` trips a pump that runs too long below
# `low_flow_fraction` of max flow (blocked discharge or suction), `vibration_trip` one that shakes too hard.
# =====================================================================

CAVITATION_HEAD_LOSS = 0.6
WEAR_HEAD_LOSS = 0.4
# Seconds of continuous abuse that wear a pump out completely
CAVITATION_LIFE = 3.6e5     # 100 h of full cavitation
DEADHEAD_LIFE = 1.8e6       # 500 h running against a closed or blocked discharge
DRY_LIFE = 3.6e3            # 1 h of running dry


class Pump(Transfer):
    FAULTS = ("leak_rate", "wear")

    def __init__(self, name, description="", status=1, inlets=None, outlets=None,
                 shutoff_head=3.0, max_flow=6.0, ramp_time=2.0, dry_run_trip=2.0,
                 npsh_required=0.3, cavitation_trip=10.0, rated_kw=2.2,
                 low_flow_trip_time=0.0, low_flow_fraction=0.15, vibration_trip=0.0, vibration_trip_time=60.0):
        super().__init__(name, description, status, inlets, outlets)
        self.motor = Motor(rated_kw)
        self.low_flow_trip_time = low_flow_trip_time      # s below low_flow_fraction of max_flow before a trip (0 = off)
        self.low_flow_fraction = low_flow_fraction
        self.vibration_trip = vibration_trip              # mm/s (0 = off)
        self.vibration_trip_time = vibration_trip_time
        self.low_flow_time = 0.0
        self.vibration_time = 0.0
        self.shutoff_head = shutoff_head
        self.max_flow = max_flow
        self.ramp_time = ramp_time
        self.dry_run_trip = dry_run_trip
        self.npsh_required = npsh_required
        self.cavitation_trip = cavitation_trip
        self.cavitation = 0.0       # severity 0..1
        self.cavitation_time = 0.0
        self.wear = 0.0             # 0 = new, 1 = worn out (head down 40 %). Overhaul: set_fault(tag, "wear", 0)
        self.deadheading = False
        self.run_cmd = False
        self.speed_sp = 100.0       # %
        self._last_speed_written = 0.0
        self.speed = 0.0            # % actual
        self.tripped = False
        self.dry_time = 0.0

    @property
    def running(self):
        return bool(self.status) and self.speed > 1.0 and not self.tripped

    def is_blocked(self):
        return not self.status or self.speed < 1.0 or self.tripped

    def loss_coefficient(self):
        return self.shutoff_head / self.max_flow ** 2

    def head_at_zero_flow(self):
        head = self.shutoff_head * (self.speed / 100.0) ** 2
        return head * (1.0 - CAVITATION_HEAD_LOSS * self.cavitation) * (1.0 - WEAR_HEAD_LOSS * self.wear)

    def advance(self, dt):
        target = self.speed_sp * self.motor.speed_factor() if self.status and self.run_cmd and not self.tripped else 0.0
        step = 100.0 * dt / self.ramp_time if self.ramp_time > 0 else 100.0
        self.speed += max(-step, min(step, target - self.speed))

    def after_step(self, dt, starved, fluid=None):
        if self.run_cmd and not self.tripped and starved:
            self.dry_time += dt
            if self.dry_time >= self.dry_run_trip:
                self.tripped = True
        else:
            self.dry_time = max(0.0, self.dry_time - dt)
        self._update_cavitation(dt, fluid)
        self.deadheading = self.running and abs(self.flow) < 0.05 * self.max_flow
        # Shaft load follows the affinity laws (n^3) and rises with flow; the motor turns it into amps and heat.
        n = self.speed / 100.0
        load = n ** 3 * (0.5 + 0.5 * min(1.0, abs(self.flow) / self.max_flow))
        vibration = 4.0 * self.wear + 3.0 * self.cavitation + (1.5 if self.deadheading else 0.0)
        if self.motor.update(dt, load, self.running, vibration):
            self.tripped = True
        self._protect(dt)

    def _protect(self, dt):
        """Optional protection relays (off unless configured): low flow for too long, vibration too high for too long."""
        if self.low_flow_trip_time > 0:
            low = self.running and self.speed >= 99.0 and abs(self.flow) < self.low_flow_fraction * self.max_flow
            self.low_flow_time = self.low_flow_time + dt if low else max(0.0, self.low_flow_time - dt)
            if self.low_flow_time >= self.low_flow_trip_time:
                self.tripped = True
        if self.vibration_trip > 0:
            high = self.running and self.motor.vibration > self.vibration_trip
            self.vibration_time = self.vibration_time + dt if high else max(0.0, self.vibration_time - dt)
            if self.vibration_time >= self.vibration_trip_time:
                self.tripped = True

    def repair(self):
        """Overhaul: new wear parts and a new motor winding. A trip stays until the PLC sends ResetCmd, like on a real pump."""
        super().repair()
        self.wear = 0.0
        self.cavitation_time = 0.0
        self.motor.repair()

    def slow_update(self, dt, ambient):
        """Wear from the abuse seen in the last step: cavitation, running dry, running against a closed discharge."""
        self.motor.slow_update(dt, ambient)
        rate = self.cavitation / CAVITATION_LIFE
        rate += (1.0 / DEADHEAD_LIFE) if self.deadheading else 0.0
        rate += (1.0 / DRY_LIFE) if self.dry_time > 0 else 0.0
        self.wear = min(1.0, self.wear + rate * dt)

    def _update_cavitation(self, dt, fluid):
        """Cavitation severity 0..1 from the suction margin: pressure at the inlet minus vapor pressure,
        compared with the required NPSH. It takes effect on the next step."""
        if self.running and fluid is not None and fluid.volume > 0 and self.npsh_required > 0:
            margin = self.p_in - fluid.vapor_pressure()
            self.cavitation = min(1.0, max(0.0, (self.npsh_required - margin) / self.npsh_required))
        else:
            self.cavitation = 0.0
        if self.cavitation > 0:
            self.cavitation_time += dt * self.cavitation
            if self.cavitation_time >= self.cavitation_trip:
                self.tripped = True
        else:
            self.cavitation_time = max(0.0, self.cavitation_time - dt)

    def _write_speed(self, value):
        """An untouched PLC variable reads 0, which must not stop a pump that was never given a speed:
        a speed setpoint only counts once it differs from the last one written (starting at 0)."""
        if value != self._last_speed_written:
            self._last_speed_written = value
            self.speed_sp = min(100.0, max(0.0, value))

    def _reset(self, value):
        if value and self.motor.reset():     # a burnt-out motor stays tripped
            self.tripped = False
            self.dry_time = 0.0
            self.cavitation_time = 0.0
            self.low_flow_time = self.vibration_time = 0.0

    def signals(self):
        return [
            Signal("RunCmd", FROM_PLC, "BOOL", get=lambda: self.run_cmd, set=lambda v: setattr(self, "run_cmd", v)),
            Signal("SpeedSP", FROM_PLC, "REAL", "%", get=lambda: self.speed_sp, set=self._write_speed),
            Signal("ResetCmd", FROM_PLC, "BOOL", get=lambda: False, set=self._reset),
            Signal("RunningFb", TO_PLC, "BOOL", get=lambda: self.running),
            Signal("Fault", TO_PLC, "BOOL", get=lambda: self.tripped),
            Signal("Speed", TO_PLC, "REAL", "%", get=lambda: self.speed),
            Signal("Current", TO_PLC, "REAL", "A", get=lambda: self.motor.current),
            Signal("WindingTemp", TO_PLC, "REAL", "C", get=lambda: self.motor.winding_temp),
            Signal("Voltage", TO_PLC, "REAL", "V", get=lambda: self.motor.voltage),
            Signal("Vibration", TO_PLC, "REAL", "mm/s", get=lambda: self.motor.vibration),
        ]

    def get_state(self):
        return {"run_cmd": bool(self.run_cmd), "speed_sp": float(self.speed_sp), "speed": float(self.speed),
                "tripped": bool(self.tripped), "dry_time": float(self.dry_time),
                "cavitation": float(self.cavitation), "cavitation_time": float(self.cavitation_time),
                "wear": float(self.wear), "low_flow_time": float(self.low_flow_time),
                "vibration_time": float(self.vibration_time), "motor": self.motor.get_state()}

    def set_state(self, state):
        self.run_cmd, self.speed_sp, self.speed = state["run_cmd"], state["speed_sp"], state["speed"]
        self.tripped, self.dry_time = state["tripped"], state["dry_time"]
        self.cavitation = state.get("cavitation", 0.0)
        self.cavitation_time = state.get("cavitation_time", 0.0)
        self.wear = state.get("wear", 0.0)
        self.low_flow_time = state.get("low_flow_time", 0.0)
        self.vibration_time = state.get("vibration_time", 0.0)
        self.motor.set_state(state.get("motor", {}))
        self._last_speed_written = 0.0

    def apply_failsafe(self):
        self.run_cmd = False
