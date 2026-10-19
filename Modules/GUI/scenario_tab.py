import json
import tkinter as tk
from tkinter import messagebox, ttk

from Data.scenarios import delete_scenario, list_scenarios, load_scenario, save_scenario
from Modules.Editor.catalog import DEFAULT_FAULTS
from Modules.Editor.document import PlantDocument
from Modules.GUI.view3d import ViewCanvas
from Modules.GUI.plant_tab import graph_data, node_colours
from Modules.Scenario.scenario import ACTIONS, NEEDS_TARGET, PRESETS, Event, Scenario, leak

# =====================================================================
# Scenario tab: click a node to target it, describe what happens to it (or to the grid / weather), and
# collect the events into a Scenario. Scenarios are stored per plant in the database and replayed by the
# Run tab (or by ScenarioPlayer in any script). Times are plant seconds from the start of the run.
# =====================================================================


class ScenarioTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.plant_name = None
        self.scenario = Scenario("New scenario")
        self.target = tk.StringVar()
        self.vars = {k: tk.StringVar(value=v) for k, v in
                     dict(action="fault", name="leak_rate", value="0.2", time="60", duration="0", ramp="0", data="").items()}
        self._build()

    def _build(self):
        left = ttk.Frame(self)
        left.pack(side="left", fill="both", expand=True)
        ttk.Label(left, text="Click a node to make it the target of the next event").pack(anchor="w", padx=4)
        self.canvas = ViewCanvas(left, on_click=self.clicked, draggable=False, width=700, height=600)
        self.canvas.pack(fill="both", expand=True)

        right = ttk.Frame(self)
        right.pack(side="left", fill="y", padx=6)
        top = ttk.Frame(right)
        top.pack(fill="x", pady=4)
        ttk.Label(top, text="Scenario:").grid(row=0, column=0, sticky="w")
        self.name = ttk.Entry(top, width=24)
        self.name.grid(row=0, column=1, columnspan=2)
        self.name.insert(0, self.scenario.name)
        self.saved = ttk.Combobox(top, state="readonly", width=22)
        self.saved.grid(row=1, column=1)
        self.saved.bind("<<ComboboxSelected>>", lambda e: self.load())
        ttk.Button(top, text="Save", command=self.save).grid(row=0, column=3, padx=2)
        ttk.Button(top, text="Delete", command=self.delete).grid(row=1, column=3, padx=2)
        ttk.Label(top, text="Saved:").grid(row=1, column=0, sticky="w")

        form = ttk.LabelFrame(right, text="New event")
        form.pack(fill="x", pady=4)
        rows = (("Action", "action", ACTIONS), ("Fault / parameter", "name", DEFAULT_FAULTS), ("Value", "value", None),
                ("Start (s)", "time", None), ("Duration (s, 0 = permanent)", "duration", None),
                ("Ramp (s)", "ramp", None),
                ("Data (JSON, feed_lot / charge)", "data", None))
        ttk.Label(form, text="Target").grid(row=0, column=0, sticky="w")
        ttk.Label(form, textvariable=self.target, font=("Segoe UI", 9, "bold")).grid(row=0, column=1, sticky="w")
        for i, (text, key, choices) in enumerate(rows, start=1):
            ttk.Label(form, text=text).grid(row=i, column=0, sticky="w")
            if choices:
                ttk.Combobox(form, textvariable=self.vars[key], values=choices, width=18).grid(row=i, column=1)
            else:
                ttk.Entry(form, textvariable=self.vars[key], width=20).grid(row=i, column=1)
        ttk.Button(form, text="Add event", command=self.add_event).grid(row=len(rows) + 1, column=0, columnspan=2, pady=4)

        presets = ttk.LabelFrame(right, text="Presets (added to the list)")
        presets.pack(fill="x", pady=4)
        for i, key in enumerate(PRESETS):
            ttk.Button(presets, text=key.replace("_", " "), command=lambda k=key: self.preset(k)).grid(
                row=i // 2, column=i % 2, sticky="ew", padx=2, pady=1)

        ttk.Label(right, text="Events").pack(anchor="w")
        self.events = tk.Listbox(right, width=58, height=12)
        self.events.pack(fill="both", expand=True)
        ttk.Button(right, text="Remove selected event", command=self.remove_event).pack(pady=3)
        ttk.Button(right, text="Clear all", command=self.clear).pack()

    # ------------------------------------------------------------ plant
    def set_plant(self, plant_name):
        self.plant_name = plant_name
        self.target.set("")
        doc = PlantDocument(self.app.session, plant_name)
        self.canvas.fill = node_colours(doc)
        self.canvas.selected = None
        self.canvas.set_graph(*graph_data(doc))
        self.saved["values"] = list_scenarios(self.app.session, plant_name)

    def clicked(self, tag, button):
        self.target.set(tag)
        self.canvas.select(tag)

    # ------------------------------------------------------------ events
    def add_event(self):
        v = {k: x.get().strip() for k, x in self.vars.items()}
        try:
            event = Event(float(v["time"]), v["action"], self.target.get() if v["action"] in NEEDS_TARGET else "",
                          v["name"] if v["action"] in ("fault", "param") else "", float(v["value"]),
                          float(v["duration"]), float(v["ramp"]), data=json.loads(v["data"]) if v["data"] else {})
            self.scenario.add(event)
        except ValueError as err:      # includes bad JSON (JSONDecodeError is a ValueError)
            messagebox.showerror("Event", str(err))
            return
        self.refresh()

    def preset(self, key):
        try:
            if key == "leak":
                if not self.target.get():
                    messagebox.showinfo("Leak", "Click a node first: the leak is placed on it.")
                    return
                made = leak(self.target.get(), float(self.vars["value"].get()), float(self.vars["time"].get()),
                            float(self.vars["ramp"].get()))
            else:
                made = PRESETS[key]()
        except ValueError as err:
            messagebox.showerror("Preset", str(err))
            return
        for event in made.events:
            self.scenario.add(event)
        self.refresh()

    def remove_event(self):
        for i in reversed(self.events.curselection()):
            del self.scenario.events[i]
        self.refresh()

    def clear(self):
        self.scenario.events.clear()
        self.refresh()

    def refresh(self):
        self.events.delete(0, "end")
        for event in self.scenario.events:
            self.events.insert("end", event.describe())

    # ------------------------------------------------------------ storage
    def save(self):
        if not self.plant_name:
            return
        self.scenario.name = self.name.get().strip() or "Scenario"
        save_scenario(self.app.session, self.plant_name, self.scenario)     # note: commits the session
        self.saved["values"] = list_scenarios(self.app.session, self.plant_name)

    def load(self):
        try:
            self.scenario = load_scenario(self.app.session, self.plant_name, self.saved.get())
        except ValueError as err:
            messagebox.showerror("Load", str(err))
            return
        self.name.delete(0, "end")
        self.name.insert(0, self.scenario.name)
        self.refresh()

    def delete(self):
        if self.plant_name and self.saved.get():
            delete_scenario(self.app.session, self.plant_name, self.saved.get())
            self.saved.set("")
            self.saved["values"] = list_scenarios(self.app.session, self.plant_name)
