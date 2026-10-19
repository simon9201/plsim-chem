import os
import queue
import signal
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sqlalchemy.orm import Session

from Data.database import get_engine
from Data.exchange import (delete_snapshot, export_design, export_snapshot, import_design, import_snapshot,
                           read_json, write_json)
from Data.snapshots import list_snapshots
from Modules.Editor.document import PlantDocument
from Modules.GUI.app import App

# =====================================================================
# Launcher: the front door of plsim-chem. One window that
#   - picks the plant to work on (stored in the database)
#   - saves/loads designs as JSON files (layout only) and manages snapshots (saved running states)
#   - starts the other programs as separate processes: the editor window, the plant runtime (offline loopback or
#     TwinCAT over ADS), the AI watchdog (train and live) and the tests, and shows their output
# Run it with:  python launcher.py
#
# Each program stays an ordinary script in Runtimes/ with its own command line; the launcher only builds that
# command line (see the *_command functions) and watches the process, so nothing here is needed to run them.
# A runtime started here restores the chosen snapshot on start and saves one when it stops. Stop (rather than
# closing the process) lets it finish cleanly, which is what writes the snapshot and recordings.
# =====================================================================

PYTHON = sys.executable
RUNTIME = os.path.join(ROOT, "Runtimes")
STOP_GRACE_MS = 6000        # a process that ignores the stop request is killed after this long


def plant_command(plant, offline, ams_id, port, speed, duration, load_snapshot, save_snapshot, record):
    cmd = [PYTHON, "-u", os.path.join(RUNTIME, "run_plant.py"), "--plant", plant, "--speed", str(speed)]
    cmd += ["--offline"] if offline else ["--ams-net-id", ams_id, "--port", str(port)]
    if duration:
        cmd += ["--duration", str(duration)]
    if load_snapshot:
        cmd += ["--load-snapshot", load_snapshot]
    if save_snapshot:
        cmd += ["--save-snapshot", save_snapshot]
    if record:
        cmd += ["--record", record]
    return cmd


def train_command(episodes, learner):
    return [PYTHON, "-u", os.path.join(RUNTIME, "train_watchdog.py"), "--episodes", str(episodes), "--model", learner]


def watchdog_command(plant, ams_id, port):
    return [PYTHON, "-u", os.path.join(RUNTIME, "run_watchdog.py"), "--plant", plant,
            "--ams-net-id", ams_id, "--port", str(port)]


def gui_command():
    return [PYTHON, os.path.join(RUNTIME, "run_gui.py")]


def tests_command():
    return [PYTHON, "-u", "-m", "unittest", "discover", "-s", "tests"]


def test_plant_command():
    return [PYTHON, "-u", os.path.join(ROOT, "Data", "build_test_plant.py")]


def demo_plant_command():
    return [PYTHON, "-u", os.path.join(ROOT, "Data", "build_demo_plant.py")]


def training_plant_command():
    return [PYTHON, "-u", os.path.join(ROOT, "Data", "build_training_plant.py")]


class ProcessManager:
    """Child processes by label, with their output lines collected on a queue (read from the Tk thread)."""

    def __init__(self):
        self.processes = {}
        self.output = queue.Queue()

    def running(self, label):
        p = self.processes.get(label)
        return p is not None and p.poll() is None

    def start(self, label, cmd):
        if self.running(label):
            raise RuntimeError(f"'{label}' is already running")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        p = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             encoding="utf-8", errors="replace", creationflags=flags)
        self.processes[label] = p
        threading.Thread(target=self._pump, args=(label, p), daemon=True).start()

    def _pump(self, label, p):
        for line in p.stdout:
            self.output.put((label, line.rstrip()))
        p.stdout.close()
        self.output.put((label, f"[exited with code {p.wait()}]"))

    def stop(self, label):
        """Ask for a clean stop (Ctrl+Break on Windows, SIGINT elsewhere). Returns the process or None."""
        p = self.processes.get(label)
        if p is None or p.poll() is not None:
            return None
        p.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
        return p

    def kill_all(self):
        for p in self.processes.values():
            if p.poll() is None:
                p.kill()


class Launcher(App):
    """The one window: a Launcher tab (plants, snapshots, external programs) followed by the Plant, Scenario,
    Run, AI health and PLC tabs of App, all sharing one plant selection."""

    def __init__(self, engine=None):
        super().__init__(engine)
        self.geometry("1280x800")
        self.procs = ProcessManager()
        self.plant = tk.StringVar()
        self.ams_id = tk.StringVar(value="127.0.0.1.1.1")
        self.port = tk.StringVar(value="851")
        self.speed = tk.StringVar(value="1")
        self.duration = tk.StringVar(value="")
        self.save_name = tk.StringVar(value="")
        self.record = tk.BooleanVar(value=False)
        self.episodes = tk.StringVar(value="40")
        self.learner = tk.StringVar(value="boosted")
        self._snap_names = []
        self.panel = ttk.Frame(self.notebook)
        self.notebook.insert(0, self.panel, text="Launcher")
        self.notebook.select(self.panel)
        self._build()
        self.refresh_plants()
        self.after(100, self.poll)

    def plant_changed(self, name=None):
        """Keep the launcher's plant choice in step with the one open in the Plant tab."""
        super().plant_changed(name)
        if not hasattr(self, "panel"):
            return
        doc = self.plant_tab.doc
        if doc is not None and self.plant.get() != doc.name:
            self.refresh_plants(select=doc.name)

    def choose_plant(self):
        """The launcher's plant picker changed: open that plant in every tab."""
        self.plant_tab.open(self.plant.get())
        self.refresh_snapshots()

    # ------------------------------------------------------------ layout
    def _build(self):
        top = ttk.Frame(self.panel)
        top.pack(fill="x", padx=8, pady=8)
        ttk.Label(top, text="Plant:").pack(side="left")
        self.plant_box = ttk.Combobox(top, textvariable=self.plant, state="readonly", width=28)
        self.plant_box.pack(side="left", padx=4)
        self.plant_box.bind("<<ComboboxSelected>>", lambda e: self.choose_plant())
        ttk.Button(top, text="Refresh", command=self.refresh_plants).pack(side="left", padx=2)
        self.status = ttk.Label(top, text="")
        self.status.pack(side="left", padx=12)

        cols = ttk.Frame(self.panel)
        cols.pack(fill="x", padx=8)
        self._designs(cols).pack(side="left", fill="y", padx=(0, 6))
        self._snapshots(cols).pack(side="left", fill="y", padx=6)
        self._programs(cols).pack(side="left", fill="both", expand=True, padx=(6, 0))

        bottom = ttk.LabelFrame(self.panel, text="Output of external programs")
        bottom.pack(fill="both", expand=True, padx=8, pady=8)
        self.console = tk.Text(bottom, font=("Consolas", 9), height=14, state="disabled")
        scroll = ttk.Scrollbar(bottom, command=self.console.yview)
        self.console.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.console.pack(fill="both", expand=True)

    def _designs(self, parent):
        box = ttk.LabelFrame(parent, text="Designs (plant layout)")
        for text, cmd in (("Export design to file...", self.export_design),
                          ("Import design from file...", self.import_design),
                          ("Build demo plant (guided tour)",
                           lambda: self.start("Demo plant", demo_plant_command(),
                                              after=lambda: self.reload_plants(select="demo_plant"))),
                          ("Build reactor training plant (CSTR + PFR)",
                           lambda: self.start("Training plant", training_plant_command(),
                                              after=lambda: self.reload_plants(select="reactor_training_plant"))),
                          ("Build example plant", lambda: self.start("Example plant", test_plant_command(),
                                                                     after=self.reload_plants))):
            ttk.Button(box, text=text, command=cmd).pack(fill="x", padx=6, pady=3)
        ttk.Label(box, text="Design in the Plant editor tab (Save writes it to the database).",
                  foreground="#556").pack(padx=6, pady=(6, 6))
        return box

    def _snapshots(self, parent):
        box = ttk.LabelFrame(parent, text="Snapshots (saved plant state)")
        self.snap_list = tk.Listbox(box, height=6, width=34, exportselection=False)
        self.snap_list.pack(padx=6, pady=4)
        row = ttk.Frame(box)
        row.pack(padx=6)
        for text, cmd in (("Export...", self.export_snapshot), ("Import...", self.import_snapshot),
                          ("Delete", self.delete_snapshot)):
            ttk.Button(row, text=text, command=cmd).pack(side="left", padx=2)
        ttk.Label(box, text="Selected = restored when a runtime starts.", foreground="#556").pack(padx=6, pady=(4, 0))
        line = ttk.Frame(box)
        line.pack(fill="x", padx=6, pady=(2, 6))
        ttk.Label(line, text="Save on stop as:").pack(side="left")
        ttk.Entry(line, textvariable=self.save_name, width=14).pack(side="left", padx=4)
        return box

    def _programs(self, parent):
        box = ttk.LabelFrame(parent, text="Programs")
        ttk.Label(box, text="Open in this window:").pack(anchor="w", padx=6, pady=(6, 0))
        tabs = ttk.Frame(box)
        tabs.pack(fill="x", padx=6, pady=(0, 4))
        for text, tab in (("Plant editor", self.plant_tab), ("Scenario", self.scenario_tab), ("Run", self.run_tab),
                          ("AI health", self.health_tab), ("PLC", self.plc_tab)):
            ttk.Button(tabs, text=text, command=lambda t=tab: self.notebook.select(t)).pack(
                side="left", padx=2, expand=True, fill="x")
        ttk.Label(box, text="External programs (separate processes; they read the saved database):").pack(
            anchor="w", padx=6)
        grid = ttk.Frame(box)
        grid.pack(fill="x", padx=6, pady=4)
        fields = (("AMS net id", self.ams_id, 14), ("ADS port", self.port, 6), ("Speed x", self.speed, 5),
                  ("Duration s", self.duration, 7))
        for i, (label, var, width) in enumerate(fields):
            ttk.Label(grid, text=label).grid(row=0, column=2 * i, padx=(6, 2), sticky="w")
            ttk.Entry(grid, textvariable=var, width=width).grid(row=0, column=2 * i + 1)
        ttk.Checkbutton(box, text="Record PLC-visible signals to CSV on stop", variable=self.record).pack(
            anchor="w", padx=6)
        rows = ttk.Frame(box)
        rows.pack(fill="x", padx=6, pady=4)
        buttons = (
            ("Runtime: offline", lambda: self.start_runtime(True)),
            ("Runtime: TwinCAT (ADS)", lambda: self.start_runtime(False)),
            ("Run tests", lambda: self.start("Tests", tests_command())),
            ("Train AI watchdog", self.start_training),
            ("Run AI watchdog (live)", self.start_watchdog),
        )
        for i, (text, cmd) in enumerate(buttons):
            ttk.Button(rows, text=text, command=cmd).grid(row=i // 3, column=i % 3, sticky="ew", padx=2, pady=2)
            rows.columnconfigure(i % 3, weight=1)
        ai = ttk.Frame(box)
        ai.pack(fill="x", padx=6)
        ttk.Label(ai, text="Training episodes").pack(side="left")
        ttk.Entry(ai, textvariable=self.episodes, width=5).pack(side="left", padx=4)
        ttk.Combobox(ai, textvariable=self.learner, values=("boosted", "logistic"), width=9,
                     state="readonly").pack(side="left")
        ttk.Button(ai, text="Stop all", command=self.stop_all).pack(side="right")
        ttk.Button(ai, text="Stop runtime/watchdog", command=self.stop_selected).pack(side="right", padx=4)
        self.running_label = ttk.Label(box, text="Nothing running", foreground="#556")
        self.running_label.pack(anchor="w", padx=6, pady=(4, 6))
        return box

    # ------------------------------------------------------------ plants and snapshots
    def say(self, text):
        self.status.config(text=text)

    def log(self, text):
        self.console.config(state="normal")
        self.console.insert("end", text + "\n")
        self.console.see("end")
        self.console.config(state="disabled")

    def refresh_plants(self, select=None):
        names = PlantDocument.list_plants(self.session)
        self.plant_box["values"] = names
        if select in names:
            self.plant.set(select)
        elif self.plant.get() not in names:
            self.plant.set(names[0] if names else "")
        self.refresh_snapshots()

    def reload_plants(self, select=None):
        """The database changed behind the tabs (import, example build): refresh them and open the plant."""
        self.plant_tab.doc = None
        self.plant_tab.refresh_list()
        self.refresh_plants(select=select)
        if select and select in self.plant_box["values"]:
            self.plant_tab.open(select)

    def refresh_snapshots(self):
        self.snap_list.delete(0, "end")
        self._snap_names = []
        if not self.plant.get():
            return
        rows = list_snapshots(self.session, self.plant.get())
        for name, sim_time, created in rows:
            self.snap_list.insert("end", f"{name}  (t={sim_time:,.0f} s, {created:%Y-%m-%d %H:%M})")
        self._snap_names = [n for n, _, _ in rows]

    def selected_snapshot(self):
        sel = self.snap_list.curselection()
        return self._snap_names[sel[0]] if sel else None

    def need_plant(self):
        if not self.plant.get():
            messagebox.showinfo("plsim-chem", "Create or import a plant first (Build example plant).")
            return None
        return self.plant.get()

    def export_design(self):
        plant = self.need_plant()
        if not plant:
            return
        path = filedialog.asksaveasfilename(defaultextension=".json", initialfile=f"{plant}.design.json",
                                            filetypes=[("Design", "*.json")])
        if path:
            with Session(get_engine()) as session:
                write_json(path, export_design(session, plant))
            self.say(f"Exported '{plant}'")

    def import_design(self):
        path = filedialog.askopenfilename(filetypes=[("Design", "*.json")])
        if not path:
            return
        try:
            data = read_json(path)
            name = simpledialog.askstring("Import design", "Plant name:", initialvalue=data.get("name", ""), parent=self)
            if not name:
                return
            with Session(get_engine()) as session:
                import_design(session, data, name)
        except (OSError, ValueError, KeyError, TypeError) as err:
            messagebox.showerror("Import design", f"{type(err).__name__}: {err}")
            return
        self.reload_plants(select=name)
        self.say(f"Imported '{name}'")

    def export_snapshot(self):
        plant, snap = self.need_plant(), self.selected_snapshot()
        if not plant or not snap:
            return messagebox.showinfo("Snapshots", "Select a snapshot first.")
        path = filedialog.asksaveasfilename(defaultextension=".json", initialfile=f"{plant}.{snap}.snapshot.json",
                                            filetypes=[("Snapshot", "*.json")])
        if path:
            with Session(get_engine()) as session:
                write_json(path, export_snapshot(session, plant, snap))
            self.say(f"Exported snapshot '{snap}'")

    def import_snapshot(self):
        plant = self.need_plant()
        path = plant and filedialog.askopenfilename(filetypes=[("Snapshot", "*.json")])
        if not path:
            return
        try:
            data = read_json(path)
            name = simpledialog.askstring("Import snapshot", "Snapshot name:", initialvalue=data.get("name", ""),
                                          parent=self)
            if not name:
                return
            with Session(get_engine()) as session:
                if name in [n for n, _, _ in list_snapshots(session, plant)] and not messagebox.askyesno(
                        "Import snapshot", f"'{name}' exists. Overwrite it?"):
                    return
                import_snapshot(session, plant, data, name, overwrite=True)
        except (OSError, ValueError, KeyError, TypeError) as err:
            messagebox.showerror("Import snapshot", f"{type(err).__name__}: {err}")
            return
        self.refresh_snapshots()
        self.say(f"Imported snapshot '{name}'")

    def delete_snapshot(self):
        plant, snap = self.need_plant(), self.selected_snapshot()
        if plant and snap and messagebox.askyesno("Delete snapshot", f"Delete '{snap}'?"):
            with Session(get_engine()) as session:
                delete_snapshot(session, plant, snap)
            self.refresh_snapshots()

    # ------------------------------------------------------------ programs
    def start(self, label, cmd, after=None):
        try:
            self.procs.start(label, cmd)
        except (RuntimeError, OSError) as err:
            messagebox.showerror(label, str(err))
            return
        self._after = getattr(self, "_after", {})
        if after:
            self._after[label] = after
        self.log(f"--- {label}: {' '.join(os.path.basename(c) if os.path.isabs(c) else c for c in cmd[1:])}")

    def _numbers(self):
        try:
            port = int(self.port.get())
            speed = float(self.speed.get())
            duration = float(self.duration.get()) if self.duration.get().strip() else None
        except ValueError:
            messagebox.showerror("Settings", "Port, speed and duration must be numbers.")
            return None
        return port, speed, duration

    def start_runtime(self, offline):
        plant, numbers = self.need_plant(), self._numbers()
        if not plant or numbers is None:
            return
        port, speed, duration = numbers
        record = None
        if self.record.get():
            record = filedialog.asksaveasfilename(defaultextension=".csv", initialfile=f"{plant}.csv",
                                                  filetypes=[("CSV", "*.csv")])
            if not record:
                return
        cmd = plant_command(plant, offline, self.ams_id.get().strip(), port, speed, duration,
                            self.selected_snapshot(), self.save_name.get().strip() or None, record)
        self.start("Runtime", cmd, after=self.refresh_snapshots)

    def start_training(self):
        try:
            episodes = int(self.episodes.get())
        except ValueError:
            return messagebox.showerror("Settings", "Episodes must be a whole number.")
        self.start("Training", train_command(episodes, self.learner.get()))

    def start_watchdog(self):
        plant, numbers = self.need_plant(), self._numbers()
        if plant and numbers is not None:
            self.start("Watchdog", watchdog_command(plant, self.ams_id.get().strip(), numbers[0]))

    def stop_label(self, label):
        p = self.procs.stop(label)
        if p is not None:
            self.log(f"--- stopping {label} ...")
            self.after(STOP_GRACE_MS, lambda: p.poll() is None and p.kill())

    def stop_selected(self):
        for label in ("Runtime", "Watchdog"):
            self.stop_label(label)

    def stop_all(self):
        for label in list(self.procs.processes):
            self.stop_label(label)

    def poll(self):
        try:
            while True:
                label, line = self.procs.output.get_nowait()
                self.log(f"[{label}] {line}")
                if line.startswith("[exited") and label in getattr(self, "_after", {}):
                    self._after.pop(label)()
        except queue.Empty:
            pass
        active = [label for label in self.procs.processes if self.procs.running(label)]
        self.running_label.config(text="Running: " + ", ".join(active) if active else "Nothing running")
        self.after(100, self.poll)

    def close(self):
        active = [label for label in self.procs.processes if self.procs.running(label)]
        if active and not messagebox.askyesno("plsim-chem", f"Stop {', '.join(active)} and exit?"):
            return
        self.procs.kill_all()
        super().close()


def main():
    Launcher().mainloop()


if __name__ == "__main__":
    main()
