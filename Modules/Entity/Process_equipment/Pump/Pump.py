from Modules.Entity.Entity import Signal, TO_PLC, FROM_PLC
from Modules.Entity.Process_equipment.transfer import Transfer


class Pump(Transfer):
    """Drives flow. PLC writes RunCmd, and SpeedSP (%) when variable_speed.
    Trips on dry running (fault latched until ResetCmd). Compressors can follow the same pattern later."""
    def __init__(self, name, max_flow=5.0, variable_speed=False, ramp_time=3.0,
                 dry_run_trip_time=2.0, inlets=None, outlets=None):
        super().__init__(name, max_flow, inlets, outlets)
        self.variable_speed = variable_speed
        self.ramp_time = ramp_time                    # s from stopped to full speed
        self.dry_run_trip_time = dry_run_trip_time    # s of running with an empty suction before it trips
        self.run_cmd = False
        self.speed_sp = 1.0     # 0..1, used when variable_speed
        self.speed = 0.0        # 0..1 actual
        self.tripped = False
        self._dry_time = 0.0

    def advance(self, dt):
        if self.tripped or not self.run_cmd:
            target = 0.0
        else:
            target = self.speed_sp if self.variable_speed else 1.0
        step = dt / self.ramp_time if self.ramp_time > 0 else 1.0
        self.speed += max(-step, min(step, target - self.speed))

    def flow_limit(self):
        return self.max_flow * self.speed

    def observe(self, dt, starved):
        if self.speed > 0 and starved:
            self._dry_time += dt
            if self._dry_time >= self.dry_run_trip_time:
                self.tripped = True
        else:
            self._dry_time = 0.0

    def apply_failsafe(self):
        self.run_cmd = False

    def _set_run_cmd(self, value):
        self.run_cmd = value

    def _set_speed_sp(self, value):
        self.speed_sp = max(0.0, min(1.0, value / 100.0))

    def _reset(self, value):
        if value:
            self.tripped = False
            self._dry_time = 0.0

    def signals(self):
        sigs = [
            Signal("RunCmd", FROM_PLC, "BOOL", get=lambda: self.run_cmd, set=self._set_run_cmd),
            Signal("ResetCmd", FROM_PLC, "BOOL", get=lambda: False, set=self._reset),
        ]
        if self.variable_speed:
            sigs.append(Signal("SpeedSP", FROM_PLC, "REAL", "%",
                               get=lambda: self.speed_sp * 100.0, set=self._set_speed_sp))
            sigs.append(Signal("Speed", TO_PLC, "REAL", "%", get=lambda: self.speed * 100.0))
        sigs.append(Signal("RunningFb", TO_PLC, "BOOL", get=lambda: self.speed > 0.01))
        sigs.append(Signal("Fault", TO_PLC, "BOOL", get=lambda: self.tripped))
        return sigs
