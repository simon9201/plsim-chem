from Modules.Entity.Entity import Entity, Signal, TO_PLC, FROM_PLC
from Modules.Entity.Process_equipment.transfer import Transfer, k_to_resistance

# =====================================================================
# Parent class: Transfer
# Classes: ActuatedMixin (travelling actuator, shared by Valve and UtilityValve), Valve, CheckValve, UtilityValve
#
# The PLC commands a position (OpenCmd for on/off, PositionSP 0..100 % for
# control valves). The real valve takes `travel_time` seconds to move and
# reports OpenFb / ClosedFb. `stuck` is a fault hook: the valve stops moving.
# =====================================================================


# What an untouched PLC variable reads. These values are not treated as commands.
UNTOUCHED = {"OpenCmd": False, "PositionSP": 0.0}


class ActuatedMixin:
    def _init_actuator(self, travel_time, fail_position, initial_position):
        self.travel_time = travel_time
        self.fail_position = fail_position
        self.position = initial_position     # 0..1, actual
        self.command = initial_position      # 0..1, wanted
        self.stuck = False
        self._last_written = dict(UNTOUCHED)

    def _write_command(self, signal, value, command):
        """OpenCmd and PositionSP both set the same wanted position. The PLC writes every cycle,
        so a value only counts when it changes: the signal the PLC program actually uses wins."""
        if self._last_written.get(signal) != value:
            self._last_written[signal] = value
            self.command = min(1.0, max(0.0, command))

    def _move(self, dt):
        if self.stuck:
            return
        step = dt / self.travel_time if self.travel_time > 0 else 1.0
        self.position += max(-step, min(step, self.command - self.position))
        self.position = min(1.0, max(0.0, self.position))

    def _actuator_signals(self):
        return [
            Signal("OpenCmd", FROM_PLC, "BOOL", get=lambda: self.command >= 0.5,
                   set=lambda v: self._write_command("OpenCmd", v, 1.0 if v else 0.0)),
            Signal("PositionSP", FROM_PLC, "REAL", "%", get=lambda: self.command * 100.0,
                   set=lambda v: self._write_command("PositionSP", v, v / 100.0)),
            Signal("OpenFb", TO_PLC, "BOOL", get=lambda: self.position >= 0.99),
            Signal("ClosedFb", TO_PLC, "BOOL", get=lambda: self.position <= 0.01),
            Signal("Position", TO_PLC, "REAL", "%", get=lambda: self.position * 100.0),
        ]

    def _actuator_state(self):
        return {"command": float(self.command), "position": float(self.position), "stuck": bool(self.stuck)}

    def _set_actuator_state(self, s):
        self.command, self.position, self.stuck = s["command"], s["position"], s["stuck"]
        self._last_written = dict(UNTOUCHED)

    def apply_failsafe(self):
        self.command = self.fail_position
        self._last_written = dict(UNTOUCHED)


class Valve(ActuatedMixin, Transfer):
    """Control/on-off valve. rated_dp (bar) is the pressure drop at max_flow (L/s) when fully open."""

    def __init__(self, name, description="", status=1, inlets=None, outlets=None,
                 rated_dp=0.5, max_flow=4.0, travel_time=3.0, fail_position=0.0, initial_position=0.0):
        super().__init__(name, description, status, inlets, outlets)
        self.rated_dp = rated_dp
        self.max_flow = max_flow
        self._init_actuator(travel_time, fail_position, initial_position)

    def is_blocked(self):
        return self.position <= 0.005

    def loss_coefficient(self):
        pos = max(self.position, 0.005)
        return self.rated_dp / self.max_flow ** 2 / pos ** 2

    def advance(self, dt):
        self._move(dt)

    def signals(self):
        return self._actuator_signals()

    def get_state(self):
        return self._actuator_state()

    def set_state(self, state):
        self._set_actuator_state(state)


class CheckValve(Transfer):
    """Passive valve that lets flow through in one direction only."""
    one_way = True

    def __init__(self, name, description="", status=1, inlets=None, outlets=None, rated_dp=0.05, max_flow=4.0):
        super().__init__(name, description, status, inlets, outlets)
        self.rated_dp = rated_dp
        self.max_flow = max_flow

    def loss_coefficient(self):
        return self.rated_dp / self.max_flow ** 2




class UtilityValve(ActuatedMixin, Entity):
    """Valve on a utility line (e.g. reactor cooling water). It has no ports: the equipment it serves
    reads `position` to know how much utility flows. It is linked through an equipment parameter such as "cooling_valve"."""

    def __init__(self, name, description="", status=1, inlets=None, outlets=None,
                 travel_time=3.0, fail_position=1.0, initial_position=0.0):
        super().__init__(name, description, status)
        self._init_actuator(travel_time, fail_position, initial_position)

    def advance(self, dt):
        self._move(dt)

    def signals(self):
        return self._actuator_signals()

    def get_state(self):
        return self._actuator_state()

    def set_state(self, state):
        self._set_actuator_state(state)
