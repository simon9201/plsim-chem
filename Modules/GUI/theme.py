from tkinter import ttk

# =====================================================================
# Colours for the whole GUI in one place (no physics, no database).
#   type_colour   - a base colour per kind of equipment, so a plant is readable at a glance
#   heat_colour   - liquid temperature, blue (cold) through green and yellow to red (hot)
#   flow_style    - colour and width of a pipe line from its flow
#   apply         - the ttk look of the window
# =====================================================================

BACKGROUND = "#e9eff6"
PANEL = "#f6f8fb"
ACCENT = "#1f6feb"
IDLE_LINE = "#8e9aa8"
FAULT = "#ff7b7b"
WARNING = "#ffb454"
FROZEN = "#bfe9ff"
GAS = "#bfe0ff"
SELECTED = "#0050c8"

TYPE_COLOURS = {
    "storage_tank": "#a9d8c8", "cstr": "#f5c38b", "pfr": "#f0a96b", "flash_drum": "#c8c3f0", "gas_volume": GAS,
    "distillation_column": "#d6b3f0", "dryer": "#f7b6c9", "evaporator": "#f9d29a",
    "heat_exchanger": "#f4a58a", "pump": "#9fdca8", "compressor": "#8fd1d6",
    "valve": "#ffe08a", "check_valve": "#ffe08a", "utility_valve": "#ffe08a",
    "gas_valve": "#ffe08a", "gas_check_valve": "#ffe08a",
    "source": "#b8e6a0", "sink": "#c9ccd1", "gas_source": "#b8e6a0", "gas_sink": "#c9ccd1",
    "pipe": "#d9dee5", "elbow": "#d9dee5", "gas_pipe": "#d6e9fb",
    "junction": "#e4d9c6", "manifold": "#e4d9c6", "tee": "#e4d9c6",
}
DEFAULT_COLOUR = "#e3e7ec"


def type_colour(type_name, domain="liquid"):
    return TYPE_COLOURS.get(type_name, GAS if domain == "gas" else DEFAULT_COLOUR)


def _lerp(a, b, f):
    return tuple(round(x + (y - x) * f) for x, y in zip(a, b))


HEAT_STOPS = ((-10.0, (70, 130, 230)), (20.0, (70, 200, 200)), (50.0, (120, 210, 110)),
              (90.0, (245, 210, 80)), (140.0, (235, 80, 60)))


def heat_colour(temperature_c):
    """Colour of a liquid at this temperature [C]."""
    t = temperature_c
    if t <= HEAT_STOPS[0][0]:
        rgb = HEAT_STOPS[0][1]
    elif t >= HEAT_STOPS[-1][0]:
        rgb = HEAT_STOPS[-1][1]
    else:
        for (t0, c0), (t1, c1) in zip(HEAT_STOPS, HEAT_STOPS[1:]):
            if t0 <= t <= t1:
                rgb = _lerp(c0, c1, (t - t0) / (t1 - t0))
                break
    return "#%02x%02x%02x" % rgb


def flow_style(q, q_max, blocked=False):
    """(colour, width) of a line carrying q (L/s) when the busiest line carries q_max."""
    if blocked:
        return FAULT, 3
    if abs(q) < 1e-6:
        return IDLE_LINE, 1
    f = min(1.0, abs(q) / max(q_max, 1e-9))
    r, g, b = _lerp((90, 190, 255), (20, 90, 230), f)
    return "#%02x%02x%02x" % (r, g, b), 2 + round(4 * f)


def apply(root):
    """Give the window a clean coloured look. Safe to call on any Tk root."""
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")
    root.configure(background=BACKGROUND)
    style.configure(".", background=BACKGROUND, font=("Segoe UI", 9))
    style.configure("TFrame", background=BACKGROUND)
    style.configure("TLabel", background=BACKGROUND)
    style.configure("TCheckbutton", background=BACKGROUND)
    style.configure("TNotebook", background=BACKGROUND, borderwidth=0)
    style.configure("TNotebook.Tab", padding=(14, 6), background="#cfd9e6", font=("Segoe UI", 10, "bold"))
    style.map("TNotebook.Tab", background=[("selected", ACCENT)], foreground=[("selected", "white")])
    style.configure("TButton", padding=(8, 3))
    style.configure("Treeview", rowheight=22, background="white", fieldbackground="white")
    style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"), background="#cfd9e6")
