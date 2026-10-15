from Modules.Entity.Entity import FROM_PLC, TO_PLC

# =====================================================================
# The link between the simulation and the PLC.
#
#   PlcLink    - how values travel (AdsLink for TwinCAT, LoopbackLink for tests)
#   PlcBridge  - which signal goes to which PLC variable (from the plc_tag table)
#
# One cycle: read_commands() -> plant.step(dt) -> write_feedback()
# =====================================================================


class LinkError(Exception):
    """The PLC cannot be reached or a variable is missing."""


class PlcLink:
    def connect(self):
        pass

    def close(self):
        pass

    def read(self, symbols):
        raise NotImplementedError

    def write(self, values):
        raise NotImplementedError


class LoopbackLink(PlcLink):
    """In-memory stand-in for a PLC: whoever holds .memory plays the PLC. Used for tests and offline runs."""
    def __init__(self):
        self.memory = {}

    def read(self, symbols):
        return {s: self.memory.get(s, 0) for s in symbols}

    def write(self, values):
        self.memory.update(values)


class AdsLink(PlcLink):
    """TwinCAT over ADS (pyads). Defaults to the runtime on this machine, PLC port 851."""
    def __init__(self, ams_net_id="127.0.0.1.1.1", port=851):
        self.ams_net_id = ams_net_id
        self.port = port
        self._plc = None

    def connect(self):
        import pyads    # imported here so offline runs work without the TwinCAT ADS DLL
        self.close()
        try:
            plc = pyads.Connection(self.ams_net_id, self.port)
            plc.open()
            state = plc.read_state()[0]
        except (pyads.ADSError, OSError) as err:
            raise LinkError(f"Cannot reach the PLC at {self.ams_net_id}:{self.port}: {err}") from err
        if state != pyads.ADSSTATE_RUN:
            plc.close()
            raise LinkError(f"The PLC runtime is not in RUN state (state {state})")
        self._plc = plc

    def close(self):
        if self._plc is not None:
            try:
                self._plc.close()
            except Exception:
                pass
            self._plc = None

    def _connection(self):
        if self._plc is None:
            raise LinkError("Not connected")
        return self._plc

    def read(self, symbols):
        import pyads
        try:
            result = self._connection().read_list_by_name(list(symbols))
        except (pyads.ADSError, OSError) as err:
            raise LinkError(str(err)) from err
        bad = {s: v for s, v in result.items() if isinstance(v, str)}
        if bad:
            raise LinkError(f"Could not read: {bad}")
        return result

    def write(self, values):
        import pyads
        try:
            result = self._connection().write_list_by_name(dict(values))
        except (pyads.ADSError, OSError) as err:
            raise LinkError(str(err)) from err
        bad = {s: str(v) for s, v in result.items() if str(v) != "no error"}
        if bad:
            raise LinkError(f"Could not write: {bad}")


class PlcBridge:
    def __init__(self, plant, tags, link):
        self.link = link
        table = plant.signal_table()
        self._commands = []     # (plc symbol, signal)
        self._feedback = []
        for tag in tags:
            signal = table.get((tag.entity_tag, tag.signal))
            if signal is None:
                raise ValueError(f"Tag {tag.plc_symbol}: {tag.entity_tag} has no signal '{tag.signal}'")
            if signal.direction != tag.direction:
                raise ValueError(f"Tag {tag.plc_symbol}: direction does not match the signal. "
                                 "Run Data/build_test_plant.py to refresh the tag table.")
            (self._commands if signal.direction == FROM_PLC else self._feedback).append((tag.plc_symbol, signal))

    def read_commands(self):
        """Fetch commands from the PLC and apply them to the plant."""
        if not self._commands:
            return
        values = self.link.read([symbol for symbol, _ in self._commands])
        for symbol, signal in self._commands:
            signal.write(values[symbol])

    def write_feedback(self):
        """Send the latest measurements and equipment feedback to the PLC."""
        if not self._feedback:
            return
        self.link.write({symbol: signal.read() for symbol, signal in self._feedback})
