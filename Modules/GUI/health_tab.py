import tkinter as tk
from tkinter import ttk

from Modules.Editor.document import PlantDocument
from Modules.GUI.view3d import ViewCanvas
from Modules.GUI.plant_tab import graph_data
from Modules.Watchdog.access import OperatorView
from Modules.Watchdog.evaluation import GroundTruth
from Modules.Watchdog.health import ALARM, HealthMonitor, measurement_sampler
from Modules.Watchdog.residuals import discover_residuals, with_residuals

# =====================================================================
# Health tab: the AI's live picture of the plant. It only listens to the Run tab (the simulation) and
# draws what a HealthMonitor (Modules/Watchdog/health.py) says: node colour = estimated risk, with the
# reasons, a trend and a time-to-alarm forecast for the selected node.
#
# Data access: the monitor is fed only through an OperatorView (Modules/Watchdog/access.py): PLC signals and
# the work-order log. The Repair button is the mechanic (it acts on the plant, outside the AI); the optional
# "ground truth" line comes from evaluation.py and is for judging the AI, not for the AI.
#
# Maintenance: "Repair" calls plant.repair(tag) (what a mechanic would do) and the monitor starts a fresh
# baseline for that node. Repairs made any other way (scenario events, scripts) are picked up from
# plant.maintenance_log. "Accept as new normal" relearns without counting a repair, for deliberate changes
# of operating point. The tab never reads hidden state, except the optional "show true faults" line.
# =====================================================================

COLOURS = {"learning": "#c9d6ea", "unmonitored": "#f2f2f2"}


def risk_colour(risk):
    """Green (0) through yellow to red (1)."""
    risk = max(0.0, min(1.0, risk))
    r = int(120 + 135 * min(1.0, risk * 2))
    g = int(210 - 130 * max(0.0, risk * 2 - 1)) if risk > 0.5 else 210
    return f"#{r:02x}{g:02x}78"


class HealthTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.monitor = None
        self.plant = None
        self.doc = None
        self.seen_repairs = 0
        self.show_truth = tk.BooleanVar(value=False)
        self._build()

    def _build(self):
        ttk.Label(self, text="Live sensor-based health estimate while Run is active. Green = low risk; yellow/orange/red "
                             "show rising risk; blue = learning a baseline; grey = no usable sensor.").pack(
                                 anchor="w", padx=4, pady=2)
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        self.canvas = ViewCanvas(body, on_click=self.clicked, draggable=False, width=700, height=600)
        self.canvas.pack(side="left", fill="both", expand=True)
        side = ttk.Frame(body)
        side.pack(side="left", fill="y", padx=4)
        self.summary = tk.Text(side, width=46, height=14, font=("Consolas", 9))
        self.summary.pack()
        self.spark = tk.Canvas(side, width=330, height=90, background="white")
        self.spark.pack(pady=4)
        row = ttk.Frame(side)
        row.pack(fill="x")
        ttk.Button(row, text="Repair selected", command=self.repair).pack(side="left", padx=2)
        ttk.Button(row, text="Accept as new normal", command=self.relearn).pack(side="left", padx=2)
        ttk.Checkbutton(side, text="Show true faults (ground truth)", variable=self.show_truth,
                        command=self.show_detail).pack(anchor="w")
        ttk.Label(side, text="Maintenance log").pack(anchor="w")
        self.log = tk.Text(side, width=46, height=6, font=("Consolas", 9))
        self.log.pack()

    # ------------------------------------------------------------ hooks called by the Run tab
    def on_start(self, plant, doc):
        specs = discover_residuals(doc.nodes(), doc.edges(), doc.probes())
        self.plant, self.doc = plant, doc       # plant: only for the mechanic's Repair button and the ground-truth line
        self.view = OperatorView(plant, lab=getattr(plant, "lab", None))  # reported labs and PLC feedback
        self.truth = GroundTruth(plant)
        self.sample = with_residuals(measurement_sampler(self.view), specs)
        self.monitor = HealthMonitor(doc.nodes(), doc.probes(), doc.edges(), specs)
        self.seen_repairs = 0
        self.log.delete("1.0", "end")
        self.canvas.set_graph(*graph_data(doc))
        self.canvas.selected = None

    def on_tick(self, plant):
        if self.monitor is None or plant is not self.plant:
            return
        self.sync_maintenance()
        self.monitor.update(self.sample(), self.view.time)
        self.paint()

    def sync_maintenance(self):
        """Tell the monitor about work orders logged since we last looked."""
        orders = self.view.work_orders()
        for t, tag in orders[self.seen_repairs:]:
            if tag in self.monitor.health:
                self.monitor.maintenance(tag, t)
                self.log.insert("end", f"{t:9.0f}s  repaired {tag}\n")
        self.seen_repairs = len(orders)

    # ------------------------------------------------------------ drawing
    def paint(self):
        for tag, h in self.monitor.health.items():
            status = h.status
            self.canvas.fill[tag] = COLOURS.get(status) or risk_colour(h.risk)
            self.canvas.notes[tag] = status if status in COLOURS else f"{h.risk:.0%} {status}"
        self.canvas.redraw()
        self.show_detail()

    def clicked(self, tag, button):
        self.canvas.select(tag)
        self.show_detail()

    def show_detail(self):
        tag = self.canvas.selected
        self.summary.delete("1.0", "end")
        self.spark.delete("all")
        if not (self.monitor and tag in self.monitor.health):
            return
        h = self.monitor.health[tag]
        eta = self.monitor.forecast(tag)
        lines = [f"{tag}: {h.status}", f"risk {h.risk:.0%}  (sudden {h.instant:.0%}, accumulated wear {h.wear:.0%})",
                 f"repairs logged: {h.repairs}", "estimate from measured signals; not a failure probability"]
        if eta == 0.0:
            lines.append("forecast: at alarm level now")
        elif eta is not None:
            lines.append(f"forecast: alarm in about {eta / 3600:.1f} h at the current trend")
        lines += [""] + (self.monitor.reasons(tag) or ["no unusual readings"])
        if self.show_truth.get() and tag in self.plant.equipment:
            active = self.truth.condition(tag)
            lines += ["", f"ground truth (evaluation only, hidden from the AI): {active or 'no faults'}"]
        self.summary.insert("1.0", "\n".join(lines))
        pts = list(h.history)
        if len(pts) > 1:
            t0, t1 = pts[0][0], pts[-1][0]
            span = max(t1 - t0, 1e-9)
            xy = [c for t, r in pts for c in (4 + 322 * (t - t0) / span, 84 - 78 * min(1.0, r))]
            self.spark.create_line(4, 84 - 78 * ALARM, 326, 84 - 78 * ALARM, fill="#cc4444", dash=(3, 3))
            self.spark.create_line(*xy, fill="#0050c8", width=2)

    # ------------------------------------------------------------ operator actions
    def repair(self):
        tag = self.canvas.selected
        if self.plant is not None and tag in self.plant.equipment:
            self.plant.repair(tag)
            self.sync_maintenance()
            self.paint()

    def relearn(self):
        tag = self.canvas.selected
        if self.monitor and tag in self.monitor.health:
            self.monitor.maintenance(tag, self.plant.time, relearn_only=True)
            self.log.insert("end", f"{self.plant.time:9.0f}s  new normal for {tag}\n")
            self.paint()
