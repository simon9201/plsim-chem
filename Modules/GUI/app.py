import tkinter as tk
from tkinter import ttk

from sqlalchemy.orm import Session

from Data.database import get_engine
from Modules.GUI.health_tab import HealthTab
from Modules.GUI.plant_tab import PlantTab
from Modules.GUI.plc_tab import PlcTab
from Modules.GUI.run_tab import RunTab
from Modules.GUI.scenario_tab import ScenarioTab
from Modules.GUI import theme

# =====================================================================
# The window: three tabs (Plant, Scenario, Run) sharing one database session. The tabs do not know about
# each other; the app tells the others when the plant being edited changes.
# =====================================================================


class App(tk.Tk):
    def __init__(self, engine=None):
        super().__init__()
        self.title("plsim-chem")
        self.geometry("1280x740")
        theme.apply(self)
        self.session = Session(engine or get_engine())
        notebook = self.notebook = ttk.Notebook(self)       # the launcher adds its own tab to this
        notebook.pack(fill="both", expand=True)
        self.plant_tab = PlantTab(notebook, self)
        self.scenario_tab = ScenarioTab(notebook, self)
        self.run_tab = RunTab(notebook, self)
        self.health_tab = HealthTab(notebook, self)
        self.plc_tab = PlcTab(notebook, self)
        notebook.add(self.plant_tab, text="Plant")
        notebook.add(self.scenario_tab, text="Scenario")
        notebook.add(self.run_tab, text="Run")
        notebook.add(self.health_tab, text="AI health")
        notebook.add(self.plc_tab, text="PLC")
        notebook.bind("<<NotebookTabChanged>>", lambda e: self.plant_changed())
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.ready = True
        if self.plant_tab.doc:
            self.plant_changed()

    def plant_changed(self, name=None):
        """Refresh the scenario view (nodes may have changed). Called on plant changes and tab switches."""
        if not getattr(self, "ready", False):       # tabs open a plant while they are still being built
            return
        doc = self.plant_tab.doc
        if doc is not None:
            self.scenario_tab.set_plant(doc.name)

    def close(self):
        self.run_tab.stop()
        self.session.close()
        self.destroy()


def main():
    App().mainloop()
