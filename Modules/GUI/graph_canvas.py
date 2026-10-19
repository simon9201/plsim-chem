import tkinter as tk

# =====================================================================
# GraphCanvas: draws a plant as boxes (nodes), arrows (connections) and little badges (instruments).
# It knows nothing about databases or physics. You give it plain data with set_graph() and it calls
# back when something is clicked or dragged. The three tabs of the app all reuse it.
# =====================================================================

from Modules.GUI import theme

NODE_W, NODE_H = 96, 40
GAS_COLOUR = theme.GAS
LIQUID_COLOUR = theme.DEFAULT_COLOUR


class GraphCanvas(tk.Canvas):
    def __init__(self, master, on_click=None, on_move=None, on_background=None, draggable=True, **kw):
        kw.setdefault("background", theme.PANEL)
        super().__init__(master, **kw)
        self.on_click = on_click                # f(tag, button)
        self.on_move = on_move                  # f(tag, x, y) once a drag ends
        self.on_background = on_background      # f(x, y)
        self.draggable = draggable
        self.nodes, self.edges, self.probes = {}, [], []
        self.fill = {}                          # tag -> fill colour override (e.g. fault = red)
        self.notes = {}                         # tag -> extra line of text under the label
        self.edge_style = {}                    # (from_tag, to_tag) -> (colour, width), e.g. from the flow
        self.levels = {}                        # tag -> (fraction 0..1, colour) liquid level drawn inside a vessel
        self.selected = None
        self._drag = None
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._motion)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Button-3>", self._right)

    # ------------------------------------------------------------ data in
    def set_graph(self, nodes, edges, probes=()):
        """nodes: {tag: (x, y, label)}, edges: [(from_tag, to_tag)], probes: [(tag, target_tag)]."""
        self.nodes, self.edges, self.probes = dict(nodes), list(edges), list(probes)
        self.redraw()

    def select(self, tag):
        self.selected = tag
        self.redraw()

    def redraw(self):
        self.delete("all")
        for a, b in self.edges:
            if a in self.nodes and b in self.nodes:
                self._arrow(a, b)
        for tag, (x, y, label) in self.nodes.items():
            colour = self.fill.get(tag, LIQUID_COLOUR)
            outline, width = (theme.SELECTED, 3) if tag == self.selected else ("#444444", 1)
            self.create_rectangle(x - NODE_W / 2, y - NODE_H / 2, x + NODE_W / 2, y + NODE_H / 2,
                                  fill=colour, outline=outline, width=width, tags=("node", f"n:{tag}"))
            if tag in self.levels:      # liquid level as a coloured band from the bottom of the box
                fraction, liquid = self.levels[tag]
                top = y + NODE_H / 2 - NODE_H * max(0.0, min(1.0, fraction))
                self.create_rectangle(x - NODE_W / 2 + 1, top, x + NODE_W / 2 - 1, y + NODE_H / 2 - 1,
                                      fill=liquid, outline="", stipple="gray50", tags=(f"n:{tag}",))
            self.create_text(x, y - 6, text=tag, font=("Segoe UI", 9, "bold"), tags=(f"n:{tag}",))
            self.create_text(x, y + 9, text=self.notes.get(tag, label), font=("Segoe UI", 8), fill="#333333",
                             tags=(f"n:{tag}",))
        slots = {}
        for tag, target in self.probes:
            if target not in self.nodes:
                continue
            k = slots.get(target, 0)
            slots[target] = k + 1
            x, y, _ = self.nodes[target]
            bx, by = x - NODE_W / 2 + 6 + k * 34, y - NODE_H / 2 - 9
            self.create_rectangle(bx - 15, by - 7, bx + 15, by + 7, fill="#fff2b3", outline="#997a00",
                                  tags=(f"p:{tag}",))
            self.create_text(bx, by, text=tag, font=("Segoe UI", 7), tags=(f"p:{tag}",))

    def _arrow(self, a, b):
        (ax, ay, _), (bx, by, _) = self.nodes[a], self.nodes[b]
        colour, width = self.edge_style.get((a, b), ("#555555", 2))
        self.create_line(*self._edge_point(ax, ay, bx, by), *self._edge_point(bx, by, ax, ay),
                         arrow=tk.LAST, width=width, fill=colour)

    @staticmethod
    def _edge_point(x, y, tx, ty):
        """Where the line from the box centre (x, y) towards (tx, ty) leaves the box."""
        dx, dy = tx - x, ty - y
        if dx == 0 and dy == 0:
            return x, y
        scale = min((NODE_W / 2) / abs(dx) if dx else float("inf"), (NODE_H / 2) / abs(dy) if dy else float("inf"))
        return x + dx * scale, y + dy * scale

    # ------------------------------------------------------------ mouse
    def tag_at(self, x, y):
        for item in reversed(self.find_overlapping(x, y, x, y)):
            for t in self.gettags(item):
                if t.startswith("n:"):
                    return t[2:]
        return None

    def _press(self, event):
        x, y = self.canvasx(event.x), self.canvasy(event.y)
        tag = self.tag_at(x, y)
        if tag is None:
            if self.on_background:
                self.on_background(x, y)
            return
        self._drag = (tag, False)
        if self.on_click:
            self.on_click(tag, 1)

    def _right(self, event):
        tag = self.tag_at(self.canvasx(event.x), self.canvasy(event.y))
        if tag and self.on_click:
            self.on_click(tag, 3)

    def _motion(self, event):
        if not (self.draggable and self._drag):
            return
        tag = self._drag[0]
        _, _, label = self.nodes[tag]
        self.nodes[tag] = (self.canvasx(event.x), self.canvasy(event.y), label)
        self._drag = (tag, True)
        self.redraw()

    def _release(self, event):
        if self._drag and self._drag[1] and self.on_move:
            tag = self._drag[0]
            x, y, _ = self.nodes[tag]
            self.on_move(tag, x, y)
        self._drag = None
