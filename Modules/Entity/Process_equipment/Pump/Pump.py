from Modules.Entity.Entity import Signal, TO_PLC, FROM_PLC
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
                 npsh_required=0.3, cavitation_trip=10.0):
        super().__init__(name, description, status, inlets, outlets)
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
        target = self.speed_sp if self.status and self.run_cmd and not self.tripped else 0.0
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

    def repair(self):
        """Overhaul: new wear parts. A trip stays until the PLC sends ResetCmd, like on a real pump."""
        super().repair()
        self.wear = 0.0
        self.cavitation_time = 0.0

    def slow_update(self, dt, ambient):
        """Wear from the abuse seen in the last step: cavitation, running dry, running against a closed discharge."""
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
        if value:
            self.tripped = False
            self.dry_time = 0.0
            self.cavitation_time = 0.0

    def signals(self):
        return [
            Signal("RunCmd", FROM_PLC, "BOOL", get=lambda: self.run_cmd, set=lambda v: setattr(self, "run_cmd", v)),
            Signal("SpeedSP", FROM_PLC, "REAL", "%", get=lambda: self.speed_sp, set=self._write_speed),
            Signal("ResetCmd", FROM_PLC, "BOOL", get=lambda: False, set=self._reset),
            Signal("RunningFb", TO_PLC, "BOOL", get=lambda: self.running),
            Signal("Fault", TO_PLC, "BOOL", get=lambda: self.tripped),
            Signal("Speed", TO_PLC, "REAL", "%", get=lambda: self.speed),
        ]

    def get_state(self):
        return {"run_cmd": bool(self.run_cmd), "speed_sp": float(self.speed_sp), "speed": float(self.speed),
                "tripped": bool(self.tripped), "dry_time": float(self.dry_time),
                "cavitation": float(self.cavitation), "cavitation_time": float(self.cavitation_time),
                "wear": float(self.wear)}

    def set_state(self, state):
        self.run_cmd, self.speed_sp, self.speed = state["run_cmd"], state["speed_sp"], state["speed"]
        self.tripped, self.dry_time = state["tripped"], state["dry_time"]
        self.cavitation = state.get("cavitation", 0.0)
        self.cavitation_time = state.get("cavitation_time", 0.0)
        self.wear = state.get("wear", 0.0)
        self._last_speed_written = 0.0

    def apply_failsafe(self):
        self.run_cmd = False
