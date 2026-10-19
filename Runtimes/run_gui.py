# Starts the desktop application (Plant, Scenario, Run, AI health and PLC tabs): python Runtimes\run_gui.py
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Modules.GUI.app import main

if __name__ == "__main__":
    main()
