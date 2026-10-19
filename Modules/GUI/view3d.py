import math
import tkinter as tk
from tkinter import ttk

from Modules.Editor.document import METERS_PER_UNIT
from Modules.GUI import theme
from Modules.GUI.graph_canvas import GraphCanvas
from Modules.GUI.projection import Camera

# =====================================================================
# 3D view of a plant: simplified vector drawings (cylinders for vessels, bow-ties for valves, ...) placed at
# the equipment's position: x, y from the canvas (METERS_PER_UNIT metres per unit) and z = elevation in metres.
# Scene3D is a GraphCanvas, so every tab can use it the same way. ViewCanvas holds the 2D and 3D canvases
# behind one object with a "3D view" switch; tabs create a ViewCanvas where they used to create a GraphCanvas.
#   drag background: rotate    mouse wheel: zoom    drag equipment: move on its floor level
# =====================================================================

TANKS = ("storage_tank", "cstr", "flash_drum", "gas_volume", "distillation_column", "dryer", "evaporator")
VALVES = ("valve", "check_valve", "utility_valve", "gas_valve", "gas_check_valve")
PUMPS = ("pump", "compressor")
PIPES = ("pipe", "elbow", "gas_pipe", "heat_exchanger")
GAS_FILL, LIQUID_FILL = "#d6ecff", "#cfe8cf"


def dimensions(type_name, params):
    """(shape, radius or half-width m, height m) from the equipment parameters."""
    if type_name in TANKS:
        height = float(params.get("height", 3.0)) or 3.0
        litres = float(params.get("max_capacity", params.get("volume", 1000.0)))
        return "cylinder", max(0.3, math.sqrt(litres / 1000.0 / (math.pi * height))), height
    if type_name in VALVES:
        return "valve", 0.4, 0.8
    if type_name in PUMPS:
        return "pump", 0.5, 0.7
    if type_name in PIPES:
        return "pipe", 0.25, 0.5
    return "box", 0.4, 0.8


class Scene3D(GraphCanvas):
    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self.shapes = {}                    # tag -> (type, z, params)
        self.camera = Camera()
        self._fitted = False
        self._turn = None
        self.bind("<MouseWheel>", lambda e: self._wheel(e.delta))
        self.bind("<Configure>", self._resized)

    def set_graph(self, nodes, edges, probes=(), shapes=None):
        self.shapes = dict(shapes or {})
        self.nodes, self.edges, self.probes = dict(nodes), list(edges), list(probes)
        if not self._fitted and self.nodes:
            self.fit()
        self.redraw()

    def _info(self, tag):
        type_name, _, params = self.shapes.get(tag, ("", 0.0, {}))
        return type_name, params

    def world(self, tag):
        x, y, _ = self.nodes[tag]
        return x * METERS_PER_UNIT, y * METERS_PER_UNIT, self.shapes.get(tag, ("", 0.0, {}))[1]

    def _size(self):
        w, h = self.winfo_width(), self.winfo_height()
        return (w, h) if w > 50 and h > 50 else (float(self.cget("width")), float(self.cget("height")))

    def fit(self):
        """Centre the camera on the plant and zoom to show all of it."""
        if not self.nodes:
            return
        pts = [self.world(t) for t in self.nodes]
        xs, ys, zs = [p[0] for p in pts], [p[1] for p in pts], [p[2] for p in pts]
        extent = max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs), 6.0)
        w, h = self._size()
        self.camera.centre = ((max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2, (max(zs) + min(zs)) / 2)
        self.camera.scale = max(4.0, min(40.0, 0.9 * min(w, h) / extent))
        self.camera.origin = (w / 2, h / 2)
        self._fitted = True
        self.redraw()

    def _resized(self, event):
        self.camera.origin = (event.width / 2, event.height / 2)
        self.redraw()

    def _centre(self, tag):
        x, y, z = self.world(tag)
        kind, _, h = dimensions(*self._info(tag))
        return x, y, z + (h / 2 if kind == "cylinder" else 0.3)

    def redraw(self):
        self.delete("all")
        cam = self.camera
        self._ground()
        for a, b in self.edges:
            if a in self.nodes and b in self.nodes:
                ax, ay, _ = cam.project(*self._centre(a))
                bx, by, _ = cam.project(*self._centre(b))
                colour, width = self.edge_style.get((a, b), ("#555555", 2))
                self.create_line(ax, ay, bx, by, arrow=tk.LAST, width=width, fill=colour)
        for tag in sorted(self.nodes, key=lambda t: cam.project(*self.world(t))[2]):
            self._draw_node(tag)
        slots = {}
        for tag, target in self.probes:
            if target not in self.nodes:
                continue
            k = slots.get(target, 0)
            slots[target] = k + 1
            x, y, z = self.world(target)
            _, _, h = dimensions(*self._info(target))
            sx, sy, _ = cam.project(x, y, z + h)
            bx, by = sx - 14 + k * 32, sy - 14
            self.create_rectangle(bx - 15, by - 7, bx + 15, by + 7, fill="#fff2b3", outline="#997a00", tags=(f"p:{tag}",))
            self.create_text(bx, by, text=tag, font=("Segoe UI", 7), tags=(f"p:{tag}",))

    def _ground(self):
        if not self.nodes:
            return
        cam = self.camera
        pts = [self.world(t) for t in self.nodes]
        lo_x, hi_x = min(p[0] for p in pts) - 10, max(p[0] for p in pts) + 10
        lo_y, hi_y = min(p[1] for p in pts) - 10, max(p[1] for p in pts) + 10
        step = 5.0
        x = math.floor(lo_x / step) * step
        while x <= hi_x:
            a, b = cam.project(x, lo_y, 0.0), cam.project(x, hi_y, 0.0)
            self.create_line(a[0], a[1], b[0], b[1], fill="#e6e6e6")
            x += step
        y = math.floor(lo_y / step) * step
        while y <= hi_y:
            a, b = cam.project(lo_x, y, 0.0), cam.project(hi_x, y, 0.0)
            self.create_line(a[0], a[1], b[0], b[1], fill="#e6e6e6")
            y += step

    def _draw_node(self, tag):
        cam = self.camera
        type_name, params = self._info(tag)
        kind, r, h = dimensions(type_name, params)
        x, y, z = self.world(tag)
        gas = "gas" in type_name or type_name == "compressor"
        colour = self.fill.get(tag, theme.type_colour(type_name, "gas" if gas else "liquid"))
        outline, width = (theme.SELECTED, 3) if tag == self.selected else ("#444444", 1)
        tags = ("node", f"n:{tag}")
        bx, by, _ = cam.project(x, y, z)
        if z != 0:      # dashed drop line to the floor shows the elevation
            gx, gy, _ = cam.project(x, y, 0.0)
            self.create_line(bx, by, gx, gy, dash=(3, 3), fill="#999999")
            self.create_oval(gx - 3, gy - 2, gx + 3, gy + 2, fill="#999999", outline="")
        s = cam.scale
        if kind == "cylinder":
            tx, ty, _ = cam.project(x, y, z + h)
            rx, ry = r * s, max(2.0, r * s * math.sin(cam.pitch))
            self.create_oval(bx - rx, by - ry, bx + rx, by + ry, fill=colour, outline=outline, width=width, tags=tags)
            self.create_rectangle(bx - rx, ty, bx + rx, by, fill=colour, outline="", tags=tags)
            self.create_line(bx - rx, ty, bx - rx, by, fill=outline, width=width, tags=tags)
            self.create_line(bx + rx, ty, bx + rx, by, fill=outline, width=width, tags=tags)
            self.create_oval(tx - rx, ty - ry, tx + rx, ty + ry, fill=colour, outline=outline, width=width, tags=tags)
            if tag in self.levels:      # liquid inside the shell: a coloured column up to the level
                fraction, liquid = self.levels[tag]
                ly = by - (by - ty) * max(0.0, min(1.0, fraction))
                self.create_rectangle(bx - rx + 2, ly, bx + rx - 2, by, fill=liquid, outline="", tags=tags)
                self.create_oval(bx - rx + 2, ly - ry, bx + rx - 2, ly + ry, fill=liquid, outline="", tags=tags)
            label_y = by + ry + 8
        elif kind == "valve":
            cx, cy, _ = cam.project(x, y, z + 0.3)
            a = max(6.0, 0.4 * s)
            self.create_polygon(cx - a, cy - a * 0.6, cx + a, cy + a * 0.6, cx + a, cy - a * 0.6, cx - a, cy + a * 0.6,
                                fill=colour, outline=outline, width=width, tags=tags)
            self.create_line(cx, cy, cx, cy - a * 1.3, fill=outline, width=width, tags=tags)
            self.create_line(cx - a * 0.5, cy - a * 1.3, cx + a * 0.5, cy - a * 1.3, fill=outline, width=width + 1, tags=tags)
            label_y = cy + a + 8
        elif kind == "pump":
            cx, cy, _ = cam.project(x, y, z + 0.4)
            a = max(7.0, 0.5 * s)
            self.create_oval(cx - a, cy - a, cx + a, cy + a, fill=colour, outline=outline, width=width, tags=tags)
            self.create_polygon(cx - a * 0.5, cy - a * 0.6, cx - a * 0.5, cy + a * 0.6, cx + a * 0.7, cy,
                                fill=colour, outline=outline, width=width, tags=tags)
            self.create_rectangle(cx - a, cy + a, cx + a, cy + a * 1.35, fill="#bbbbbb", outline=outline, tags=tags)
            label_y = cy + a * 1.35 + 8
        elif kind == "pipe":
            cx, cy, _ = cam.project(x, y, z + 0.25)
            a = max(6.0, 0.5 * s)
            self.create_rectangle(cx - a, cy - a * 0.3, cx + a, cy + a * 0.3, fill=colour, outline=outline,
                                  width=width, tags=tags)
            label_y = cy + a * 0.3 + 8
        else:
            self._box(x, y, z, r, h, colour, outline, width, tags)
            label_y = by + r * s + 8
        self.create_text(bx, label_y, text=tag, font=("Segoe UI", 8, "bold"), tags=tags)
        note = self.notes.get(tag)
        if note:
            self.create_text(bx, label_y + 11, text=note, font=("Segoe UI", 7), fill="#333333", tags=tags)
        if z != 0:
            self.create_text(bx + 6, by, text=f"z={z:g} m", anchor="w", font=("Segoe UI", 7), fill="#666666", tags=tags)

    def _box(self, x, y, z, r, h, colour, outline, width, tags):
        cam = self.camera
        v = [(x + sx * r, y + sy * r, z + zz) for zz in (0, h) for sy in (-1, 1) for sx in (-1, 1)]
        faces = ((0, 1, 3, 2), (4, 5, 7, 6), (0, 1, 5, 4), (2, 3, 7, 6), (0, 2, 6, 4), (1, 3, 7, 5))
        scored = []
        for f in faces:
            pts = [cam.project(*v[i]) for i in f]
            scored.append((sum(p[2] for p in pts) / 4, f, pts))
        for _, f, pts in sorted(scored, key=lambda s: s[0]):
            shade = "#ffffff" if f[0] == 4 else colour
            self.create_polygon([c for p in pts for c in p[:2]], fill=shade, outline=outline, width=width, tags=tags)

    # ------------------------------------------------------------ mouse
    def _wheel(self, delta):
        self.camera.zoom(1.1 if delta > 0 else 1 / 1.1)
        self.redraw()

    def _press(self, event):
        tag = self.tag_at(self.canvasx(event.x), self.canvasy(event.y))
        if tag is None:
            self._turn = (event.x, event.y, False)
            return
        self._drag = (tag, False)
        if self.on_click:
            self.on_click(tag, 1)

    def _motion(self, event):
        if self._drag and self.draggable:
            tag = self._drag[0]
            gx, gy = self.camera.to_ground(event.x, event.y, self.world(tag)[2])
            self.nodes[tag] = (gx / METERS_PER_UNIT, gy / METERS_PER_UNIT, self.nodes[tag][2])
            self._drag = (tag, True)
            self.redraw()
        elif self._turn:
            x0, y0, _ = self._turn
            self.camera.rotate((event.x - x0) * 0.01, (event.y - y0) * 0.006)
            self._turn = (event.x, event.y, True)
            self.redraw()

    def _release(self, event):
        if self._turn and not self._turn[2] and self.on_background:
            gx, gy = self.camera.to_ground(event.x, event.y, 0.0)
            self.on_background(gx / METERS_PER_UNIT, gy / METERS_PER_UNIT)
        self._turn = None
        super()._release(event)


class ViewCanvas(ttk.Frame):
    """A 2D GraphCanvas and a Scene3D behind one object with a '3D view' switch. Tabs treat it like a
    GraphCanvas: the shared attributes (fill, notes, selected, callbacks) are mirrored to both canvases."""
    SHARED = ("fill", "notes", "edge_style", "levels", "selected", "on_click", "on_move", "on_background", "draggable")

    def __init__(self, master, **kw):
        super().__init__(master)
        object.__setattr__(self, "_ready", False)
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        self.is_3d = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="3D view", variable=self.is_3d, command=self._switch).pack(side="left")
        self._c2 = GraphCanvas(self, **kw)
        self._c3 = Scene3D(self, **kw)
        self._c3.notes, self._c3.fill = self._c2.notes, self._c2.fill
        self._c3.edge_style, self._c3.levels = self._c2.edge_style, self._c2.levels
        self._fit = ttk.Button(bar, text="Fit view", command=lambda: self._c3.fit())
        self._hint = ttk.Label(bar, text="drag: rotate   wheel: zoom   drag equipment: move on its level",
                               foreground="#666666")
        self._active = self._c2
        self._c2.pack(fill="both", expand=True)
        object.__setattr__(self, "_ready", True)

    def _switch(self):
        self._active.pack_forget()
        self._active = self._c3 if self.is_3d.get() else self._c2
        self._fit.pack_forget()
        self._hint.pack_forget()
        if self._active is self._c3:
            self._fit.pack(side="left", padx=6)
            self._hint.pack(side="left")
        self._active.pack(fill="both", expand=True)
        self._active.redraw()

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._active, name)

    def __setattr__(self, name, value):
        if name in self.SHARED and self.__dict__.get("_ready"):
            setattr(self._c2, name, value)
            setattr(self._c3, name, value)
        else:
            super().__setattr__(name, value)

    def set_graph(self, nodes, edges, probes=(), shapes=None):
        self._c2.set_graph(nodes, edges, probes)
        self._c3.set_graph(nodes, edges, probes, shapes)

    def redraw(self):
        self._active.redraw()

    def select(self, tag):
        self.selected = tag
        self.redraw()
