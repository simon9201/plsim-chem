from Modules.Entity.Entity import Signal, TO_PLC, FROM_PLC
from Modules.Entity.Process_equipment.process_equipment import process_equipment
from Modules.Entity.Process_equipment.transfer import Transfer


def _clamp(value, low=0.0, high=1.0):
    return max(low, min(high, value))


class _ValveActuator:
    """Behaviour shared by process valves and utility valves: slow travel, feedback, fail-safe.

    On/off valve: PLC writes OpenCmd (BOOL).
    Modulating valve: PLC writes PositionSP (REAL, %).
    Both report Position (%), OpenFb and ClosedFb, like a valve with limit switches.
    """

    def _init_actuator(self, modulating, travel_time, fail_safe):
        self.modulating = modulating
        self.travel_time = travel_time  # s for a full stroke
        self.fail_safe = fail_safe      # command (0..1) applied when the PLC connection is lost
        self.command = 0.0              # target position 0..1
        self.position = 0.0             # actual position 0..1
        self.stuck = False              # fault: the valve does not move

    def advance(self, dt):
        if self.stuck:
            return
        step = dt / self.travel_time if self.travel_time > 0 else 1.0
        self.position += _clamp(_clamp(self.command) - self.position, -step, step)

    def apply_failsafe(self):
        self.command = self.fail_safe

    def _set_open_cmd(self, value):
        self.command = 1.0 if value else 0.0

    def _set_position_sp(self, value):
        self.command = _clamp(value / 100.0)

    def signals(self):
        if self.modulating:
            command = Signal("PositionSP", FROM_PLC, "REAL", "%",
                             get=lambda: self.command * 100.0, set=self._set_position_sp)
        else:
            command = Signal("OpenCmd", FROM_PLC, "BOOL",
                             get=lambda: self.command > 0.5, set=self._set_open_cmd)
        return [
            command,
            Signal("Position", TO_PLC, "REAL", "%", get=lambda: self.position * 100.0),
            Signal("OpenFb", TO_PLC, "BOOL", get=lambda: self.position >= 0.98),
            Signal("ClosedFb", TO_PLC, "BOOL", get=lambda: self.position <= 0.02),
        ]


class Valve(_ValveActuator, Transfer):
    """In-line valve. Limits the flow in proportion to its position."""
    def __init__(self, name, max_flow=5.0, modulating=False, travel_time=5.0, fail_safe=0.0,
                 inlets=None, outlets=None):
        super().__init__(name, max_flow, inlets, outlets)
        self._init_actuator(modulating, travel_time, fail_safe)

    def flow_limit(self):
        return self.max_flow * self.position


class UtilityValve(_ValveActuator, process_equipment):
    """Valve on a utility line (for example cooling water) that is not part of the process flow graph.
    A reactor refers to it by tag to get its cooling."""
    def __init__(self, name, modulating=True, travel_time=10.0, fail_safe=0.0):
        super().__init__(name)
        self._init_actuator(modulating, travel_time, fail_safe)
