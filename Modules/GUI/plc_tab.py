import tkinter as tk
from tkinter import messagebox, ttk

# =====================================================================
# PLC tab: the PLC layer as the plant sees it. It only listens to the Run tab (set "PLC link" there to
# Loopback or ADS), and draws what a PlcMonitor (Modules/PLC/monitor.py) says:
#   - every mapped tag with its direction, live value and how long since it changed
#     (blue = command from the PLC, green = measurement/feedback to the PLC, red = involved in a problem)
#   - the problems an operator would see: broken mappings, frozen sensors, commands not followed, faults, lost link
#   - on a Loopback link you play the PLC yourself: pick a command tag and write a value.
# =====================================================================

COMMAND_BG, FEEDBACK_BG, PROBLEM_BG = "#dcebff", "#e1f5e4", "#ffd0d0"
REFRESH_EVERY = 4       # ticks between table refreshes, so a big tag list does not slow the simulation
IDLE_TEXT = "No PLC link. Set 'PLC link' on the Run tab to Loopback or ADS and press Start."


class PlcTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.cycle = None
        self.ticks = 0
        self.status = tk.StringVar(value=IDLE_TEXT)
        self.value = tk.StringVar()
        self.only_problems = tk.BooleanVar(value=False)
        self._build()

    def _build(self):
        ttk.Label(self, textvariable=self.status, font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=6, pady=4)
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True)
        columns = ("symbol", "dir", "type", "value", "unit", "age")
        self.table = ttk.Treeview(left, columns=columns, show="headings", height=22)
        for name, width in zip(columns, (260, 60, 50, 90, 50, 70)):
            self.table.heading(name, text=name)
            self.table.column(name, width=width, anchor="w")
        self.table.tag_configure("cmd", background=COMMAND_BG)
        self.table.tag_configure("fb", background=FEEDBACK_BG)
        self.table.tag_configure("bad", background=PROBLEM_BG)
        scroll = ttk.Scrollbar(left, command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        self.table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")
        side = ttk.Frame(body)
        side.pack(side="left", fill="y", padx=6)
        ttk.Label(side, text="Problems").pack(anchor="w")
        self.problems = tk.Text(side, width=58, height=16, font=("Consolas", 9), background="#fff8f8")
        self.problems.tag_configure("error", foreground="#b00020")
        self.problems.tag_configure("warn", foreground="#9a6700")
        self.problems.pack()
        ttk.Checkbutton(side, text="Show only tags with problems", variable=self.only_problems).pack(anchor="w", pady=4)
        ttk.Label(side, text="Write a command (Loopback link only)").pack(anchor="w")
        row = ttk.Frame(side)
        row.pack(fill="x")
        ttk.Entry(row, textvariable=self.value, width=14).pack(side="left")
        ttk.Button(row, text="Write to selected tag", command=self.write).pack(side="left", padx=4)
        ttk.Label(side, text="Values: 1/0 or true/false for BOOL, numbers for INT/REAL.",
                  foreground="#666666").pack(anchor="w")

    # ------------------------------------------------------------ hook called by the Run tab
    def on_tick(self, cycle):
        self.cycle = cycle
        if cycle is None:
            self.status.set(IDLE_TEXT)
            return
        mon = cycle.monitor
        state = "connected" if cycle.connected and mon.link_ok else "NOT CONNECTED"
        self.status.set(f"{type(cycle.link).__name__}: {state}   scans {cycle.scans}   "
                        f"scan {cycle.scan_ms:.2f} ms   sim t = {mon.time:,.0f} s")
        self.ticks += 1
        if self.ticks % REFRESH_EVERY == 0:
            self.refresh()

    def refresh(self):
        mon = self.cycle.monitor
        problems = mon.problems()
        bad = {e for _, e, _ in problems}
        self.problems.delete("1.0", "end")
        for severity, entity, text in problems:
            self.problems.insert("end", f"{severity.upper():5} {entity}: {text}\n", severity)
        if not problems:
            self.problems.insert("end", "no problems\n")
        selected = self.table.selection()
        self.table.delete(*self.table.get_children())
        for r in mon.rows():
            trouble = r["entity"] in bad
            if self.only_problems.get() and not trouble:
                continue
            value = r["value"]
            value = f"{value:.4g}" if isinstance(value, float) else value
            tag = "bad" if trouble else ("cmd" if r["direction"] == "from_plc" else "fb")
            self.table.insert("", "end", iid=r["symbol"], tags=(tag,), values=(
                r["symbol"], "PLC>" if r["direction"] == "from_plc" else ">PLC", r["type"], value, r["unit"],
                f"{r['age']:.0f}s"))
        keep = [s for s in selected if self.table.exists(s)]
        if keep:
            self.table.selection_set(keep)

    def write(self):
        sel = self.table.selection()
        if self.cycle is None or not sel:
            return
        text = self.value.get().strip().lower()
        row = next(r for r in self.cycle.monitor.rows() if r["symbol"] == sel[0])
        try:
            if row["type"] == "BOOL":
                value = text in ("1", "true", "yes", "on")
            elif row["type"] == "INT":
                value = int(float(text))
            else:
                value = float(text)
            self.cycle.set_command(sel[0], value)
        except ValueError as err:
            messagebox.showerror("PLC", str(err))
