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
# =====================================================================


class Pump(Transfer):
    one_way = True

    def __init__(self, name, description="", status=1, inlets=None, outlets=None,
                 shutoff_head=3.0, max_flow=6.0, ramp_time=2.0, dry_run_trip=2.0):
        super().__init__(name, description, status, inlets, outlets)
        self.shutoff_head = shutoff_head
        self.max_flow = max_flow
        self.ramp_time = ramp_time
        self.dry_run_trip = dry_run_trip
        self.run_cmd = False
        self.speed_sp = 100.0       # %
        self._last_speed_written = 0.0
        self.speed = 0.0            # % actual
        self.tripped = False
        self.dry_time = 0.0

    @property
    def running(self):
        return self.speed > 1.0 and not self.tripped

    def is_blocked(self):
        return self.speed < 1.0 or self.tripped

    def loss_coefficient(self):
        return self.shutoff_head / self.max_flow ** 2

    def head_at_zero_flow(self):
        return self.shutoff_head * (self.speed / 100.0) ** 2

    def advance(self, dt):
        target = self.speed_sp if self.run_cmd and not self.tripped else 0.0
        step = 100.0 * dt / self.ramp_time if self.ramp_time > 0 else 100.0
        self.speed += max(-step, min(step, target - self.speed))

    def after_step(self, dt, starved):
        if self.run_cmd and not self.tripped and starved:
            self.dry_time += dt
            if self.dry_time >= self.dry_run_trip:
                self.tripped = True
        else:
            self.dry_time = max(0.0, self.dry_time - dt)

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
                "tripped": bool(self.tripped), "dry_time": float(self.dry_time)}

    def set_state(self, state):
        self.run_cmd, self.speed_sp, self.speed = state["run_cmd"], state["speed_sp"], state["speed"]
        self.tripped, self.dry_time = state["tripped"], state["dry_time"]
        self._last_speed_written = 0.0

    def apply_failsafe(self):
        self.run_cmd = False
