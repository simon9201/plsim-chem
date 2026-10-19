from dataclasses import dataclass, field

# =====================================================================
# What the plant editor can place. Pure data: the editor lists these, a new node gets the default
# ports and parameters, and the loader (Data/loader.py) turns the stored rows into physics.
# To make a new equipment class available: register it in Data/loader.py EQUIPMENT_TYPES and add a line here.
# domain: "liquid", "gas" or "mixed" (a flash drum has both kinds of ports); only used for colouring and port checks.
# =====================================================================


@dataclass
class TypeInfo:
    type: str
    label: str
    prefix: str                 # tag prefix for generated names, e.g. "TK" -> TK101
    inlets: list = field(default_factory=list)
    outlets: list = field(default_factory=list)
    params: dict = field(default_factory=dict)
    domain: str = "liquid"
    category: str = "equipment"


def _t(type, label, prefix, inlets=(), outlets=(), params=None, domain="liquid"):
    return TypeInfo(type, label, prefix, list(inlets), list(outlets), params or {}, domain)


EQUIPMENT = {t.type: t for t in [
    _t("source", "Source", "S", [], ["out"], {"composition": {"water": 1.0}, "temperature": 20.0, "supply_pressure": 2.0}),
    _t("sink", "Sink", "SK", ["in"], []),
    _t("storage_tank", "Storage tank", "TK", ["in1"], ["out1"],
       {"max_capacity": 2000.0, "height": 3.0, "initial_volume": 1000.0, "composition": {"water": 1.0},
        "temperature": 20.0, "ambient_ua": 50.0}),
    _t("cstr", "CSTR reactor", "R", ["in1"], ["out1"],
       {"max_capacity": 1000.0, "height": 3.0, "initial_volume": 300.0, "composition": {"water": 1.0},
        "temperature": 20.0, "cooling_ua": 500.0, "reactions": ["A_B_to_C"]}),
    _t("pfr", "PFR tubular reactor", "TR", ["in1"], ["out1"],
       {"tube_volume": 60.0, "temperature": 20.0, "cooling_ua": 3000.0, "reactions": ["A_B_to_C"]}),
    _t("pump", "Pump", "P", ["in"], ["out"], {"shutoff_head": 0.5, "max_flow": 2.4}),
    _t("valve", "Valve", "XV", ["in"], ["out"], {"rated_dp": 0.5, "max_flow": 4.0}),
    _t("check_valve", "Check valve", "CV", ["in"], ["out"]),
    _t("pipe", "Pipe", "PL", ["in"], ["out"], {"diameter": 0.05, "length": 8.0}),
    _t("elbow", "Elbow", "EL", ["in"], ["out"], {"diameter": 0.05, "length": 1.0}),
    _t("manifold", "Manifold", "MF", ["in1", "in2"], ["out1"]),
    _t("tee", "Tee", "TE", ["in1", "in2"], ["out1"]),
    _t("flash_drum", "Flash drum", "FD", ["in1"], ["out1", "gas_out"],
       {"max_capacity": 500.0, "height": 2.0, "composition": {"water": 1.0}, "temperature": 25.0}, "mixed"),
    _t("gas_source", "Gas source", "GS", [], ["out"], {"supply_pressure": 7.0, "composition": {"N2": 1.0}}, "gas"),
    _t("gas_sink", "Gas sink", "GK", ["in"], [], {}, "gas"),
    _t("gas_volume", "Gas volume", "GV", ["in1"], ["out1"], {"volume": 1000.0, "composition": {"N2": 1.0}}, "gas"),
    _t("gas_pipe", "Gas pipe", "GP", ["in"], ["out"], {"diameter": 0.05, "length": 5.0}, "gas"),
    _t("gas_valve", "Gas valve", "GXV", ["in"], ["out"], {"max_flow": 20.0}, "gas"),
    _t("gas_check_valve", "Gas check valve", "GCV", ["in"], ["out"], {}, "gas"),
    _t("compressor", "Compressor", "K", ["in"], ["out"], {"max_flow": 10.0, "max_ratio": 4.0}, "gas"),
    _t("heat_exchanger", "Heat exchanger", "HX", ["in"], ["out"], {"diameter": 0.05, "length": 4.0, "ua": 2000.0, "utility_temp": 120.0}),
    _t("evaporator", "Evaporator", "EV", ["in1"], ["out1", "vapor_out"],
       {"max_capacity": 1000.0, "height": 3.0, "initial_volume": 400.0, "composition": {"water": 1.0},
        "temperature": 25.0, "heater_power": 100000.0}, "mixed"),
    _t("dryer", "Dryer", "DR", ["in1"], ["out1", "vapor_out"],
       {"max_capacity": 500.0, "height": 2.0, "initial_volume": 100.0, "composition": {"water": 1.0},
        "temperature": 25.0, "air_temp": 120.0}, "mixed"),
    _t("distillation_column", "Distillation column", "DC", ["feed"], ["bottoms", "distillate"],
       {"max_capacity": 600.0, "height": 10.0, "initial_volume": 300.0, "composition": {"water": 0.8, "ethanol": 0.2},
        "temperature": 25.0, "trays": 10, "feed_tray": 5, "heater_power": 100000.0}),
    _t("utility_valve", "Utility valve", "UV", [], [], {"fail_position": 1.0}),
]}

INSTRUMENTS = {t.type: t for t in [
    _t("level_sensor", "Level sensor", "LT", params={"noise_std": 0.05}),
    _t("flow_sensor", "Flow sensor", "FT", params={"noise_std": 0.02}),
    _t("pressure_sensor", "Pressure sensor", "PT", params={"noise_std": 0.005}),
    _t("differential_pressure_sensor", "Differential pressure sensor", "PDT", params={"noise_std": 0.005}),
    _t("temperature_sensor", "Temperature sensor", "TT", params={"noise_std": 0.1}),
    _t("analyzer", "Analyzer", "AT", params={"component": "A", "noise_std": 0.01}),
]}

GAS_PORT_PREFIXES = ("gas", "vapor", "vapour")


def port_is_gas(equipment_type, port_name):
    """True if this port belongs to the gas network."""
    info = EQUIPMENT.get(equipment_type)
    if info is not None and info.domain == "gas":
        return True
    return port_name.lower().startswith(GAS_PORT_PREFIXES)


# Fault names that make sense per equipment type, for the scenario builder's drop-downs.
# (The authoritative list is each class's FAULTS; the GUI reads it from the running model when it can.)
DEFAULT_FAULTS = ("leak_rate", "blockage", "fouling", "fatigue", "wear", "stuck")
