from Modules.Entity.Process_equipment.Vessel.Reactor.CSTR.CSTR import ContinuousStirredTankReactor

# =====================================================================
# Parent class: CSTR (reaction kinetics, heat of reaction, jacket cooling, jacket fouling)
# Class: PlugFlowReactor (PFR, a jacketed tubular reactor)
#
# A long narrow tube with a large jacket area per volume and a small holdup, so the residence time is
# short and the reaction is carried by throughput rather than by a large pool. It is a lumped model: the
# tube is one well-mixed cell, so it does not resolve the axial temperature profile or the hot spot a
# real tube has. What it does capture, and what the watchdog needs, is the behaviour that matters for stoppages:
#   - a small thermal mass, so it heats up in seconds when coolant is lost (much faster than a CSTR)
#   - conversion that falls when the feed rate rises (shorter residence time)
#   - jacket fouling, a hidden condition that slowly weakens heat removal
# =====================================================================


class PlugFlowReactor(ContinuousStirredTankReactor):
    def __init__(self, name, description="", status=1, inlets=None, outlets=None, tube_volume=60.0,
                 cooling_ua=3000.0, **options):
        options.setdefault("max_capacity", tube_volume * 1.2)
        options.setdefault("height", 2.0)
        options.setdefault("initial_volume", tube_volume)
        options.setdefault("composition", {"water": 1.0})
        super().__init__(name, description, status, inlets, outlets, cooling_ua=cooling_ua, **options)
        self.tube_volume = tube_volume          # L of liquid the tube holds when flooded
