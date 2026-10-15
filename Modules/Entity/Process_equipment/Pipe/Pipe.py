import math

from Modules.Entity.Process_equipment.transfer import Transfer, k_to_resistance

# =====================================================================
# Parent class: Transfer
# Class: Pipe (straight run, friction loss)
# Subclasses: Elbow (extra bend loss)
#
# K = friction * length / diameter + fitting_k. `blockage` (0..1) is a fault
# hook that closes part of the flow area (it makes the loss larger).
# Pipes have no holdup: the liquid in them is neglected.
# =====================================================================


class Pipe(Transfer):
    def __init__(self, name, description="", status=1, inlets=None, outlets=None,
                 diameter=0.05, length=5.0, friction_factor=0.02, fitting_k=0.0):
        super().__init__(name, description, status, inlets, outlets)
        self.diameter = diameter
        self.length = length
        self.friction_factor = friction_factor
        self.fitting_k = fitting_k
        self.blockage = 0.0

    def is_blocked(self):
        return self.blockage >= 0.99

    def total_k(self):
        return self.friction_factor * self.length / self.diameter + self.fitting_k

    def loss_coefficient(self):
        return k_to_resistance(self.total_k(), self.diameter, 1.0 - self.blockage)

    def get_state(self):
        return {"blockage": float(self.blockage)}

    def set_state(self, state):
        self.blockage = state["blockage"]


class Elbow(Pipe):
    """Bend. angle is in degrees; a 90 degree elbow adds K = 0.9."""

    def __init__(self, name, description="", status=1, inlets=None, outlets=None,
                 angle=90.0, **pipe_options):
        super().__init__(name, description, status, inlets, outlets, **pipe_options)
        self.angle = angle
        self.fitting_k += 0.9 * angle / 90.0
