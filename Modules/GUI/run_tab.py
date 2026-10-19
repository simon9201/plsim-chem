import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from Data.loader import load_plant
from Data.snapshots import list_snapshots, load_snapshot, save_snapshot
from Data.tags import load_tags, sync_tags
from Modules.Editor.document import PlantDocument
from Modules.GUI import theme
from Modules.GUI.view3d import ViewCanvas
from Modules.GUI.plant_tab import graph_data, node_colours
from Modules.PLC.bridge import AdsLink, LoopbackLink
from Modules.PLC.monitor import PlcCycle
from Modules.Scenario.scenario import Scenario, ScenarioPlayer

# =====================================================================
# Run tab: simulate the plant being edited, offline (no PLC), and watch it. Useful for trying a scenario
# before it is used with the PLC. The simulation advances from Tk's timer, `steps_per_tick` steps of `dt`
# every 50 ms. Nodes turn red while one of their faults is active. Click a node to see its signals.
# =====================================================================

TICK_MS = 50


class RunTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.plant = None
        self.player = None
        self.running = False
        self.dt = tk.StringVar(value="0.5")
        self.steps = tk.StringVar(value="10")
        self.slow = tk.StringVar(value="1")
        self.clock = tk.StringVar(value="not started")
        self.paused = False
        self.cycle = None
        self.link_mode = tk.StringVar(value="Off")
        self.ams_id = tk.StringVar(value="127.0.0.1.1.1")
        self._build()

    def _build(self):
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=4, pady=4)
        ttk.Button(bar, text="Start / restart", command=self.start).pack(side="left", padx=2)
        self.toggle = ttk.Button(bar, text="Pause", command=self.pause)
        self.toggle.pack(side="left", padx=2)
        for text, var in (("dt (s)", self.dt), ("steps per tick", self.steps), ("slow time scale", self.slow)):
            ttk.Label(bar, text=text).pack(side="left", padx=(10, 2))
            ttk.Entry(bar, textvariable=var, width=6).pack(side="left")
        ttk.Button(bar, text="Lab sample of selected", command=self.take_sample).pack(side="left", padx=(14, 2))
        ttk.Button(bar, text="Save snapshot", command=self.save_snapshot).pack(side="left", padx=(14, 2))
        ttk.Button(bar, text="Load snapshot", command=self.load_snapshot).pack(side="left", padx=2)
        ttk.Label(bar, text="PLC link").pack(side="left", padx=(10, 2))
        ttk.Combobox(bar, textvariable=self.link_mode, values=("Off", "Loopback", "ADS"), width=9,
                     state="readonly").pack(side="left")
        ttk.Entry(bar, textvariable=self.ams_id, width=15).pack(side="left", padx=2)
        ttk.Label(bar, textvariable=self.clock).pack(side="left", padx=14)

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        self.canvas = ViewCanvas(body, on_click=self.clicked, draggable=False, width=700, height=600)
        self.canvas.pack(side="left", fill="both", expand=True)
        side = ttk.Frame(body)
        side.pack(side="left", fill="y", padx=4)
        ttk.Label(side, text="Signals of the selected node").pack(anchor="w")
        self.readout = tk.Text(side, width=44, height=22, font=("Consolas", 9))
        self.readout.pack()
        ttk.Label(side, text="Scenario log").pack(anchor="w")
        self.log = tk.Text(side, width=44, height=6, font=("Consolas", 9))
        self.log.pack()
        ttk.Label(side, text="Lab results (delayed, noisy)").pack(anchor="w")
        self.lab_view = tk.Text(side, width=44, height=7, font=("Consolas", 9))
        self.lab_view.pack()

    # ------------------------------------------------------------ control
    def start(self):
        name = self.app.plant_tab.doc.name if self.app.plant_tab.doc else None
        if not name:
            return
        try:
            self.dt_value = float(self.dt.get())
            self.steps_value = max(1, int(self.steps.get()))
            slow = float(self.slow.get())
            self.plant = load_plant(self.app.session, name)
            self.plant.slow_time_scale = slow
            self.cycle = self.make_cycle(name)
        except Exception as err:        # a plant that does not build is reported, not fatal to the window
            messagebox.showerror("Run", f"{type(err).__name__}: {err}")
            return
        scenario = Scenario.from_dict(self.app.scenario_tab.scenario.to_dict())
        self.player = ScenarioPlayer(self.plant, scenario)
        self.log.delete("1.0", "end")
        self.logged = 0
        self.doc = PlantDocument(self.app.session, name)
        self.canvas.fill = node_colours(self.doc)
        self.canvas.set_graph(*graph_data(self.doc))
        self.app.health_tab.on_start(self.plant, self.doc)
        if not self.running:
            self.running = True
            self.after(TICK_MS, self.tick)
        self.toggle.config(text="Pause")
        self.paused = False

    def make_cycle(self, name):
        """The PLC link chosen in the toolbar: None (the simulation runs alone), a loopback you play by hand
        from the PLC tab, or TwinCAT over ADS."""
        if self.cycle is not None:
            self.cycle.close()
        mode = self.link_mode.get()
        if mode == "Off":
            return None
        sync_tags(self.app.session, name, self.plant)
        tags = load_tags(self.app.session, name)
        link = LoopbackLink() if mode == "Loopback" else AdsLink(self.ams_id.get().strip() or "127.0.0.1.1.1")
        return PlcCycle(self.plant, tags, link)

    def pause(self):
        if self.plant is None:
            return
        self.paused = not self.paused
        self.toggle.config(text="Resume" if self.paused else "Pause")

    def tick(self):
        if not self.running:
            return
        if self.plant is not None and not self.paused:
            try:
                for _ in range(self.steps_value):
                    self.player.update()
                    if self.cycle:
                        self.cycle.step(self.dt_value)
                    else:
                        self.plant.step(self.dt_value)
            except Exception as err:
                self.paused = True
                messagebox.showerror("Simulation stopped", f"{type(err).__name__}: {err}")
            self.update_view()
        self.after(TICK_MS, self.tick)

    def take_sample(self):
        tag = self.canvas.selected
        if self.plant is None or tag is None:
            return
        try:
            self.plant.lab.take_sample(tag)
        except (KeyError, ValueError) as err:
            messagebox.showerror("Lab sample", str(err))

    def save_snapshot(self):
        """Store the running state under a name. Note: this commits the session, so unsaved Plant-tab edits are saved too."""
        if self.plant is None:
            return messagebox.showinfo("Snapshot", "Start the simulation first.")
        name = simpledialog.askstring("Save snapshot", "Snapshot name:", parent=self)
        if not name:
            return
        plant_name = self.doc.name
        overwrite = name in [n for n, _, _ in list_snapshots(self.app.session, plant_name)]
        if overwrite and not messagebox.askyesno("Save snapshot", f"'{name}' exists. Overwrite it?"):
            return
        save_snapshot(self.app.session, plant_name, self.plant, name, overwrite=overwrite)
        self.clock.set(f"saved snapshot '{name}'")

    def load_snapshot(self):
        """Restore a saved state into the running plant (the same plant design must be running)."""
        if self.plant is None:
            return messagebox.showinfo("Snapshot", "Start the simulation first.")
        plant_name = self.doc.name
        names = [n for n, _, _ in list_snapshots(self.app.session, plant_name)]
        if not names:
            return messagebox.showinfo("Snapshot", "This plant has no saved snapshots.")
        name = simpledialog.askstring("Load snapshot", "Snapshot name:\n" + ", ".join(names), parent=self)
        if not name:
            return
        try:
            load_snapshot(self.app.session, plant_name, self.plant, name)
        except (ValueError, KeyError) as err:
            return messagebox.showerror("Load snapshot", str(err))
        self.update_view()

    def stop(self):
        self.running = False
        if self.cycle is not None:
            self.cycle.close()

    # ------------------------------------------------------------ display
    def update_view(self):
        plant = self.plant
        self.clock.set(f"t = {plant.time:,.0f} s    outdoors {plant.ambient_temperature:.1f} C")
        plc_trouble = self.cycle.monitor.problem_entities() if self.cycle else set()
        for tag, eq in plant.equipment.items():
            active = [n for n in getattr(eq, "FAULTS", ()) if getattr(eq, n, 0)]
            self.canvas.fill[tag] = self.live_colour(tag, eq, bool(active))
            if hasattr(eq, "contents") and hasattr(eq, "max_capacity"):
                self.canvas.levels[tag] = (eq.level_percent / 100.0, theme.heat_colour(eq.temperature))
            label = self.canvas.nodes.get(tag, (0, 0, ""))[2]
            self.canvas.notes[tag] = ", ".join(active)[:18] if active else ("PLC problem" if tag in plc_trouble else label)
        self.paint_flows()
        self.canvas.redraw()
        for t, text in self.player.log[self.logged:]:
            self.log.insert("end", f"{t:9.0f}s  {text}\n")
        self.logged = len(self.player.log)
        self.show_lab()
        self.show_signals(self.canvas.selected)
        self.app.health_tab.on_tick(plant)
        self.app.plc_tab.on_tick(self.cycle)

    def live_colour(self, tag, eq, faulted):
        """Red for a fault, ice blue when frozen, orange while solids build up, else the type colour."""
        if faulted or getattr(eq, "tripped", False):
            return theme.FAULT
        if getattr(eq, "frozen", False):
            return theme.FROZEN
        if getattr(eq, "solids", None) or getattr(eq, "slurry", None):
            return theme.WARNING
        return self.base_colour(tag)

    def paint_flows(self):
        """Colour and thickness of each line from the flow through it (red when a line is closed by blockage)."""
        eq = self.plant.equipment
        flows = {}
        for a, b in self.canvas.edges:
            q = max((abs(getattr(eq[t], "flow", 0.0)) for t in (a, b) if t in eq), default=0.0)
            if q == 0.0:
                q = next((abs(e.q) for e in self.plant._edges if e.src.name == a and e.dst.name == b), 0.0)
            blocked = any(getattr(eq.get(t), "total_blockage", 0.0) > 0.9 for t in (a, b))
            flows[(a, b)] = (q, blocked)
        q_max = max((q for q, _ in flows.values()), default=1.0)
        self.canvas.edge_style.clear()
        self.canvas.edge_style.update({k: theme.flow_style(q, q_max, blocked) for k, (q, blocked) in flows.items()})

    def show_lab(self):
        lab = self.plant.lab
        self.lab_view.delete("1.0", "end")
        for r in lab.results[-6:][::-1]:
            main = ", ".join(f"{k[3:]} {v:.1f}%" for k, v in r.values.items() if k.startswith("wt.") and v > 0)
            flag = "  OUT OF SPEC: " + ",".join(r.out_of_spec) if r.out_of_spec else ""
            self.lab_view.insert("end", f"#{r.sample_id} {r.tag} t={r.taken_at:.0f}s  {main}{flag}\n")
        if lab.pending:
            self.lab_view.insert("end", f"{len(lab.pending)} sample(s) in the lab\n")

    def base_colour(self, tag):
        return node_colours(self.doc).get(tag, theme.DEFAULT_COLOUR)

    def clicked(self, tag, button):
        self.canvas.select(tag)
        self.show_signals(tag)

    def show_signals(self, tag):
        self.readout.delete("1.0", "end")
        if not (self.plant and tag in self.plant.equipment):
            return
        probes = [i for i in self.plant.instruments.values() if getattr(i, "target", None) is self.plant.equipment[tag]
                  or getattr(getattr(i, "target", None), "name", None) == tag]
        for ent in [self.plant.equipment[tag], *probes]:
            self.readout.insert("end", f"[{ent.name}]\n")
            for s in ent.signals():
                try:
                    value = s.read()
                except Exception:       # a signal that cannot be read right now should not break the display
                    continue
                value = f"{value:.4g}" if isinstance(value, float) else value
                self.readout.insert("end", f"  {s.name:<22}{value}\n")
