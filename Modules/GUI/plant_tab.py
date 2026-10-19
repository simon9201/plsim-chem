import json
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from Modules.Editor.catalog import EQUIPMENT, INSTRUMENTS
from Modules.Editor.document import PlantDocument
from Modules.GUI.theme import type_colour
from Modules.GUI.view3d import ViewCanvas

# =====================================================================
# Plant tab: design a plant as a node graph. Every action is one call on PlantDocument, so this file is
# only layout and mouse handling. Nothing reaches the database until you press Save.
#   Select  click a node to edit its parameters, drag to move it
#   Add     pick a type, click empty space (equipment) or a node (instrument)
#   Connect click the source node, then the destination node
#   Delete  click a node
# =====================================================================


def graph_data(doc):
    """Plain data for a GraphCanvas from a PlantDocument (shared with the scenario and run tabs)."""
    nodes = {n.tag: (n.x, n.y, EQUIPMENT[n.type].label if n.type in EQUIPMENT else n.type) for n in doc.nodes()}
    edges = sorted({(e.from_tag, e.to_tag) for e in doc.edges()})
    probes = [(p.tag, p.target) for p in doc.probes()]
    shapes = {n.tag: (n.type, n.z, n.params) for n in doc.nodes()}      # used by the 3D view only
    return nodes, edges, probes, shapes


def node_colours(doc):
    """Base colour of every node by its equipment type."""
    return {n.tag: type_colour(n.type, EQUIPMENT[n.type].domain if n.type in EQUIPMENT else "liquid")
            for n in doc.nodes()}


class PlantTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.doc = None
        self.mode = tk.StringVar(value="select")
        self.kind = tk.StringVar()
        self.pending = None                         # source tag while connecting
        self._build()

    def _build(self):
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=4, pady=4)
        ttk.Label(bar, text="Plant:").pack(side="left")
        self.plants = ttk.Combobox(bar, state="readonly", width=24)
        self.plants.pack(side="left", padx=4)
        self.plants.bind("<<ComboboxSelected>>", lambda e: self.open(self.plants.get()))
        for text, cmd in (("New", self.new), ("Save", self.save), ("Revert", self.revert), ("Validate", self.validate),
                           ("Auto layout", self.auto_layout)):
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=2)
        self.status = ttk.Label(bar, text="")
        self.status.pack(side="left", padx=10)

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        side = ttk.Frame(body)
        side.pack(side="left", fill="y", padx=4)
        for m in ("select", "add", "connect", "delete"):
            ttk.Radiobutton(side, text=m.capitalize(), value=m, variable=self.mode, command=self._mode_changed).pack(anchor="w")
        ttk.Label(side, text="Type to add:").pack(anchor="w", pady=(8, 0))
        types = [f"{t.type}" for t in EQUIPMENT.values()] + [f"{t.type}" for t in INSTRUMENTS.values()]
        self.kind_box = ttk.Combobox(side, state="readonly", values=types, textvariable=self.kind, width=26)
        self.kind_box.pack()
        self.kind_box.current(0)
        ttk.Label(side, text="Parameters (JSON):").pack(anchor="w", pady=(12, 0))
        self.params = tk.Text(side, width=34, height=14, font=("Consolas", 9))
        self.params.pack()
        ttk.Button(side, text="Apply parameters", command=self.apply_params).pack(pady=4)
        ttk.Button(side, text="Add port", command=self.add_port).pack()
        ttk.Label(side, text="Interface of selected (PLC):").pack(anchor="w", pady=(10, 0))
        self.interface = tk.Text(side, width=34, height=10, font=("Consolas", 9), state="disabled")
        self.interface.pack()
        ttk.Label(side, text="Elevation z (m) of selected:").pack(anchor="w", pady=(12, 0))
        row = ttk.Frame(side)
        row.pack(anchor="w")
        self.elevation = tk.StringVar(value="0")
        ttk.Entry(row, textvariable=self.elevation, width=8).pack(side="left")
        ttk.Button(row, text="Set", command=self.set_elevation).pack(side="left", padx=4)

        self.canvas = ViewCanvas(body, on_click=self.clicked, on_move=self.moved, on_background=self.background,
                                 width=900, height=600)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.refresh_list()

    # ------------------------------------------------------------ plant list
    def refresh_list(self):
        names = PlantDocument.list_plants(self.app.session)
        self.plants["values"] = names
        if self.doc is None and names:
            self.plants.set(names[0])
            self.open(names[0])

    def open(self, name):
        self.app.session.rollback()
        self.doc = PlantDocument(self.app.session, name)
        self.pending = None
        self.plants.set(name)
        if self.doc.needs_layout():             # plants built by a script have no positions: place them by flow
            self.doc.auto_layout()
            self.doc.save()
            self.say("Positions were missing: arranged by flow")
        self.redraw()
        self.app.plant_changed(name)

    def auto_layout(self):
        if self.doc:
            self.doc.auto_layout()
            self.redraw()
            self.say("Arranged by flow (Save to keep)")
            self.app.plant_changed(self.doc.name)

    def new(self):
        name = simpledialog.askstring("New plant", "Name:", parent=self)
        if not name:
            return
        try:
            self.doc = PlantDocument.create(self.app.session, name)
            self.app.session.commit()
        except ValueError as err:
            messagebox.showerror("New plant", str(err))
            return
        self.refresh_list()
        self.open(name)

    def save(self):
        if self.doc:
            self.doc.save()
            self.say("Saved")
            self.app.plant_changed(self.doc.name)

    def revert(self):
        if self.doc:
            self.doc.revert()
            self.redraw()
            self.say("Reverted to the saved version")

    def validate(self):
        if not self.doc:
            return
        problems = self.doc.validate()
        if problems:
            messagebox.showwarning("Validate", "\n".join(problems))
        else:
            self.say("Plant builds and runs a step: OK")

    def say(self, text):
        self.status.config(text=text)

    # ------------------------------------------------------------ drawing
    def redraw(self):
        if not self.doc:
            return
        self.canvas.fill = node_colours(self.doc)
        self.canvas.set_graph(*graph_data(self.doc))
        self.show_params(self.canvas.selected)

    def show_interface(self, tag):
        """List what the PLC can read (data) and write (controls) for the selected node and its sensors.
        Built from a throwaway model of the plant, so a half-finished plant just shows why it cannot be built."""
        from Data.loader import load_plant
        from Modules.Entity.Entity import FROM_PLC
        lines = []
        if tag and self.doc and any(n.tag == tag for n in self.doc.nodes()):
            try:
                self.app.session.flush()
                model = load_plant(self.app.session, self.doc.name)
                ents = [model.equipment[tag]] if tag in model.equipment else []
                ents += [i for i in model.instruments.values()
                         if getattr(getattr(i, "target", None), "name", None) == tag or i.name == tag]
                for title, controls in (("CONTROLS (PLC writes)", True), ("DATA (PLC reads)", False)):
                    lines.append(title)
                    for ent in ents:
                        for s in ent.signals():
                            if (s.direction == FROM_PLC) == controls:
                                lines.append(f"  {ent.name}.{s.name} [{s.data_type}]")
            except Exception as err:        # an unfinished plant may not build; say so instead of failing
                lines = [f"Not available: {type(err).__name__}: {err}"]
        self.interface.config(state="normal")
        self.interface.delete("1.0", "end")
        self.interface.insert("1.0", "\n".join(lines))
        self.interface.config(state="disabled")

    def show_params(self, tag):
        self.show_interface(tag)
        self.params.delete("1.0", "end")
        if tag and self.doc and any(n.tag == tag for n in self.doc.nodes()):
            node = self.doc.node(tag)
            self.params.insert("1.0", json.dumps({k: v for k, v in node.params.items() if not k.startswith("_")}, indent=2))

    def _mode_changed(self):
        self.pending = None
        self.say("")

    # ------------------------------------------------------------ mouse actions
    def attempt(self, action):
        """Run an editor call; show the editor's own error message instead of crashing the window."""
        try:
            action()
        except (ValueError, KeyError) as err:
            messagebox.showerror("Editor", str(err))
        self.redraw()

    def background(self, x, y):
        if self.doc and self.mode.get() == "add" and self.kind.get() in EQUIPMENT:
            self.attempt(lambda: self.doc.add_node(self.kind.get(), x, y))

    def clicked(self, tag, button):
        if not self.doc:
            return
        mode = self.mode.get()
        if mode == "select" or button == 3:
            self.canvas.selected = tag
            self.show_params(tag)
            if any(n.tag == tag for n in self.doc.nodes()):
                self.elevation.set(f"{self.doc.node(tag).z:g}")
            self.canvas.redraw()
        elif mode == "delete":
            self.attempt(lambda: self.doc.delete_node(tag))
        elif mode == "add" and self.kind.get() in INSTRUMENTS:
            self.attempt(lambda: self.doc.add_probe(self.kind.get(), tag))
        elif mode == "connect":
            if self.pending is None:
                self.pending = tag
                self.say(f"Connect {tag} to... (click the destination)")
            else:
                source, self.pending = self.pending, None
                self.say("")
                self.attempt(lambda: self.doc.connect_auto(source, tag))

    def moved(self, tag, x, y):
        if self.doc:
            self.doc.move_node(tag, x, y)

    def set_elevation(self):
        tag = self.canvas.selected
        if not (self.doc and tag):
            return
        try:
            z = float(self.elevation.get())
        except ValueError:
            messagebox.showerror("Elevation", "Enter a number in metres")
            return
        node = self.doc.node(tag)
        self.doc.move_node(tag, node.x, node.y, z)
        self.redraw()
        self.say(f"{tag} elevation set to {z:g} m (not saved yet)")

    def apply_params(self):
        tag = self.canvas.selected
        if not (self.doc and tag):
            return
        try:
            params = json.loads(self.params.get("1.0", "end"))
            if not isinstance(params, dict):
                raise ValueError("Parameters must be a JSON object")
        except ValueError as err:
            messagebox.showerror("Parameters", str(err))
            return
        self.doc.set_params(tag, params)
        self.say(f"Parameters of {tag} updated (not saved yet)")

    def add_port(self):
        tag = self.canvas.selected
        if not (self.doc and tag):
            return
        direction = simpledialog.askstring("Add port", "Direction (in/out):", parent=self)
        if direction in ("in", "out"):
            self.attempt(lambda: self.doc.add_port(tag, direction))
