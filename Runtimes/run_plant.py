# Runtimes/run_plant.py
import os
import sys
import time  

# Setup for a dynamic search path.
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Import a class for test purposes
from Modules.Entity.Process_equipment.Vessel.StorageTank.storage_tank import StorageTank

def main():
    # Create a tank for test purposes
    H2SO4_tank_1 = StorageTank("H2SO4_Storage", max_capacity=5000.0, initial_volume=1000.0)

    print(f"Runtime engine started. Tank '{H2SO4_tank_1.name}' has been initiated.")
    print(f"Tank '{H2SO4_tank_1.name}' temperature: {H2SO4_tank_1.temperature} C")

if __name__ == "__main__":
    main() 