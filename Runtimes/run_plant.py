# Runtimes/run_plant.py
import os
import sys

# Setup for a dynamic search path.
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sqlalchemy.orm import Session

from Data.database import get_engine
from Data.loader import load_plant

PLANT_NAME = "test_plant_3tank_reactor"
DT = 1.0            # s per simulation step
DURATION = 1800     # s
PRINT_EVERY = 120   # s


def apply_test_commands(plant):
    """Fixed commands standing in for the PLC until it is connected."""
    eq = plant.equipment
    for tag in ("XV101", "XV102", "XV103", "XV111", "XV112", "XV113", "XV201"):
        eq[tag].command = 1.0
    eq["P101"].command = 0.4
    eq["P102"].command = 0.4
    eq["P103"].command = 0.8
    eq["R201"].cooling_command = 0.5


def main():
    with Session(get_engine()) as session:
        plant = load_plant(session, PLANT_NAME)
    apply_test_commands(plant)

    print(f"Runtime engine started: plant '{plant.name}', {len(plant.equipment)} equipment, "
          f"{len(plant.instruments)} instruments")
    reactor = plant.equipment["R201"]
    steps = int(DURATION / DT)
    for i in range(steps + 1):
        if (i * DT) % PRINT_EVERY == 0:
            m = plant.read_instruments()
            print(f"t={plant.time:6.0f}s  LT201={m['LT201']:5.1f}%  TT201={m['TT201']:5.1f}C  "
                  f"FT201={m['FT201']:4.2f}L/s  [C]={reactor.concentration('C'):5.2f} mol/L  "
                  f"LT101={m['LT101']:5.1f}%")
        plant.step(DT)


if __name__ == "__main__":
    main()
