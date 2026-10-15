from dataclasses import dataclass
from typing import Callable, Optional

# =====================================================================
# Parent class: Entity
# Subclasses: process_equipment, Instrument
#
# An entity exposes "signals": named values that cross the border between
# the simulation and the PLC.
#   TO_PLC   - measurement or feedback. The simulation writes it, the PLC reads it.
#   FROM_PLC - command. The PLC writes it, the simulation reads it.
# Everything the PLC can see or do goes through signals, nothing else.
# =====================================================================

TO_PLC = "to_plc"
FROM_PLC = "from_plc"

_CASTS = {"BOOL": bool, "INT": int, "REAL": float}


@dataclass(frozen=True)
class Signal:
    name: str                       # e.g. "RunCmd"
    direction: str                  # TO_PLC or FROM_PLC
    data_type: str                  # BOOL, INT or REAL (IEC 61131-3 names)
    unit: str = ""
    get: Optional[Callable] = None
    set: Optional[Callable] = None

    def read(self):
        return _CASTS[self.data_type](self.get())

    def write(self, value):
        self.set(_CASTS[self.data_type](value))


class Entity:
    def __init__(self, name, description, status):
        self.name = name
        self.description = description
        self.status = status

    def signals(self):
        """The signals this entity exposes to the PLC."""
        return []

    def to_plc_dict(self):
        return {s.name: s.read() for s in self.signals() if s.direction == TO_PLC}

    def update_from_plc(self, values):
        by_name = {s.name: s for s in self.signals() if s.direction == FROM_PLC}
        for name, value in values.items():
            by_name[name].write(value)

    def get_state(self):
        """Everything that changes while the plant runs, as plain numbers/bools/dicts (saved in snapshots)."""
        return {}

    def set_state(self, state):
        pass

    def apply_failsafe(self):
        """Put the commands in their safe state, used when the PLC connection is lost."""
        pass
