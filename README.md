# plsim-chem

A virtual chemical plant to practise PLC programming against. The plant is simulated in Python,
stored in a database, and connected to a TwinCAT PLC over ADS. The PLC reads the sensors and
operates the pumps and valves, like it would in a real plant.

```
Python simulation  <--ADS-->  TwinCAT PLC runtime  <--ADS-->  TwinCAT HMI
```

## Setup

```
pip install sqlalchemy numpy pyads
```

## Run it against TwinCAT

1. **Build the plant and the PLC tag list** (re-run after changing the plant):
   ```
   python Data\build_test_plant.py
   ```
   This stores the test plant in `Data\plant.db` and writes the TwinCAT variable list to `PLC\GVL_Sim.st`.
2. **In TwinCAT XAE**: create a PLC project, add a global variable list named `GVL_Sim`, and paste in the
   contents of `PLC\GVL_Sim.st`. For a first program, paste `PLC\MAIN_example.st` into `MAIN`.
3. Build, activate the configuration, log in and start the PLC (the runtime must be in RUN).
4. **Start the simulation**:
   ```
   python Runtimes\run_plant.py
   ```
   Useful options: `--speed 2` (twice as fast), `--dt 0.1`, `--ams-net-id`, `--port`, `--offline`,
   `--ambient-mean`, `--daily-temperature-amplitude`, `--seasonal-temperature-amplitude`, `--slow-time-scale`,
   `--load-snapshot NAME`, `--save-snapshot NAME` (saved when the run ends, also on Ctrl+C), `--list-snapshots`.

Each cycle the simulation reads the PLC's commands, advances the plant one step (0.1 s by default), and
writes the measurements and feedback back. If the PLC connection is lost, actuators go to their
fail-safe position (valves closed, pumps stopped, cooling water open) and the simulation reconnects by itself.

## Signals

Every variable the PLC sees is a *signal* on a piece of equipment or instrument.
The names follow `GVL_Sim.<tag>_<signal>`, for example `GVL_Sim.P101_RunCmd`.

| Equipment | PLC writes | PLC reads |
|---|---|---|
| On/off valve | `OpenCmd` | `Position`, `OpenFb`, `ClosedFb` |
| Modulating valve | `PositionSP` (%) | `Position`, `OpenFb`, `ClosedFb` |
| Pump | `RunCmd`, `ResetCmd`, `SpeedSP` (variable speed only) | `RunningFb`, `Fault`, `Speed` (variable speed only) |
| Sensor | | `Value` (engineering units), `Raw` (0..32767 over the range) |

`OpenCmd` and `PositionSP` set the same valve position; use one of them in your PLC program (the one that changes last wins). A `SpeedSP` of 0 that was never written is ignored, so a pump without speed control runs at 100 %.

Valves and pumps move gradually (travel time, ramp). A pump trips if it runs with an empty tank and
stays faulted until `ResetCmd`. A pump also cavitates when its suction pressure falls below the liquid's
vapor pressure plus `npsh_required` (closed/throttled suction valve, low level, hot liquid): head drops and
it trips after `cavitation_trip` severity-seconds. Alarms and interlocks are the PLC's job: the simulation has none.

Faults for practising plant upsets: `plant.set_fault(tag, name, value)`. `leak_rate` (L/s) works on every
vessel, pipe, valve and pump (also settable as a `leak_rate` param in the database); `blockage` on pipes;
`stuck` on valves. Use 0/False to clear.

Gradual degradation and weather (all slow processes, run every `plant.slow_interval` = 10 s of plant time):
- `plant.environment = Environment(mean_c=5, daily_amp=4, annual_amp=12, start_day=0)` sets the outdoor temperature
  (day/night + seasons); `environment.offset` adds a cold snap or heat wave. Vessels exchange heat with it
  through the `ambient_ua` param (W/K, default 0 = insulated); pipes through `ambient_ua` (default 2) and `thermal_mass`.
- `plant.slow_time_scale = 1000` speeds up weather cycles and all degradation relative to the hydraulics.
- Pipes: `ice` forms when the wall is below the liquid's `freezing_point_c` (stagnant pipes follow the outdoor
  temperature, flowing ones follow the liquid) and melts when it warms. `fouling` builds when the liquid is
  over-saturated at the wall temperature (component attributes `solubility_25c`, `solubility_slope`) and stays until
  `set_fault(tag, "fouling", 0)`. Both, plus `blockage`, close the flow area.
- Pumps: `wear` (0..1) grows from cavitation, running dry and running against a closed discharge, and cuts head by up to 40 %.
  Restore with `plant.repair(tag)` (see below).
- Snapshots keep all of this. The plant builder adds `freezing_point_c: 0` to existing water chemistry rows when missing.

The mapping from signal to PLC variable is the `plc_tag` table. Edit `plc_symbol` there to use your own
names, and the runtime follows.

## Model scope

This is a **training simulator**, not a design-grade or safety-validation tool. Its useful prototype scope is
liquid transfer, tank inventory, simple reactions, pump/valve operation, sensors, and practicing PLC
alarms and interlocks.

- **Hydraulics:** pressure-driven, incompressible liquid; bar and L/s. Pumps add a simplified quadratic
  head curve; pipes, valves and fittings add quadratic resistance. Tank liquid height and static head are
  approximated from volume and a constant tank cross-section. Junction pressure is solved iteratively.
- **Chemistry and heat:** ideal mixing and component-weighted properties; reactions use configurable
  kinetic rate laws and a simplified heat balance. CSTR cooling is represented by a UA term.
- **Faults and ageing:** leaks, pipe blockage/fouling/freezing, pump cavitation and wear are useful
  qualitative training behaviours; their rates and thresholds are illustrative, not calibrated equipment data.
- **Gas, VLE and solids (basic):** a separate gas network, ideal-gas headspace, Antoine/Clausius-Clapeyron vapor
  pressures and a two-phase flash drum (see the sections below). Solids precipitate inside vessels; freezing is an error state.
- **Not modelled:** distillation columns (but the flash functions are ready for them), non-ideal liquids
  (activity-model hook only), non-constant latent heat, choked gas flow, Joule-Thomson, condensation inside gas
  lines, gas junctions (use a `gas_volume` as manifold), solids carried in streams, viscosity-dependent
  hydraulics, validated property packages. Add those before using the simulator for design or relief sizing.

Equipment types for `equipment.type`: `storage_tank`, `cstr`, `pump`, `valve`, `check_valve`,
`utility_valve`, `pipe`, `elbow`, `manifold`, `tee`, `source`, `sink`; gas side: `flash_drum`, `gas_volume`, `gas_source`, `gas_sink`, `gas_pipe`, `gas_valve`, `gas_check_valve`, `compressor`. Instrument types:
`flow_sensor`, `level_sensor`, `pressure_sensor`, `differential_pressure_sensor`,
`temperature_sensor`, `analyzer`.

## Gas, vapor-liquid equilibrium, solids and repair

**Thermo (Modules/Flow/thermo.py)** - pure functions in degC, bar, J. `psat` uses Antoine constants
(`antoine_a/b/c`, log10 bar, K) when a component has them, otherwise Clausius-Clapeyron through `boiling_point_c`
and `heat_of_vaporization` (default Trouton). Also `bubble/dew_pressure`, `bubble/dew_temperature`,
`flash_tp` (Rachford-Rice) and `flash_ph` (adiabatic). Raoult's law with gamma = 1; call
`thermo.set_activity_model(fn)` with `fn(x, T) -> {name: gamma}` to go non-ideal. Components marked
`noncondensable` (N2 is a default) never condense. Optional component keys: `antoine_a/b/c`,
`heat_of_vaporization` (J/mol), `cp_gas` (J/mol/K), `noncondensable`, `solid_density`. Existing databases get
missing keys merged into the built-in components automatically.

**Gas network** - separate from the liquid solver. Nodes (`gas_volume`, `gas_source`, `gas_sink`, and the
vapor ports of a `flash_drum`) hold gas; pressure is `nRT/V`. Lines of `gas_pipe`, `gas_valve`,
`gas_check_valve` and `compressor` between two nodes carry `P1^2 - P2^2 = R*Q^2` flow (mol/s, so flow
sensors need `range_max` set; `gas_valve.max_flow` is quoted at 5 -> 1 bar). A gas line must end at gas nodes at
both ends, may not branch or loop, and has at most one compressor. Gas and liquid ports cannot be connected.
`gas_volume` has `relief_pressure`, `leak_rate` (mol/s), `ambient_ua` and a `vented` tally.

**Compressor** - signals RunCmd, SpeedSP, ResetCmd, RunningFb, Fault, Surge, Speed, Power (kW), DischargeTemp.
Pressure ratio falls with flow from `max_ratio` at shutoff. It needs a downstream volume; a stopped compressor
blocks the line. Running below `surge_fraction` of its (speed-scaled) `max_flow` is surge: it wears the machine
and trips after `surge_trip` s. It also trips above `max_discharge_temp`. Trips clear with ResetCmd.

**Flash drum (lash_drum)** - a vessel with a headspace. Ports named `gas*` / `vapor*` join the gas network, the
others the liquid one. Volatile components relax toward `x*gamma*Psat` at `mass_transfer` (1/s); evaporation cools
the liquid, condensation heats it; liquid and vapor share one temperature. Pressure is emergent (closed drums
pressurise, vented ones boil near the bubble point). Signals HeaterCmd and CoolingCmd (0-100 %) drive `heater_power` (W)
and `cooling_ua` (W/K) against `coolant_temp`. `relief_pressure` vents excess.

**Solids and freezing (all vessels)** - a component with `solubility_25c` / `solubility_slope` precipitates when
over-saturated and redissolves when it is not; solids stay in the vessel (`vessel.solids`, `solids_percent`),
take up volume, and plug the outlet at 50 % of the volume (`plugged`). `vessel.frozen` is an error state set
below the mixture's freezing point and cleared 1 degC above it; a frozen vessel passes no flow. There are no PLC
signals for these, so add sensors on the `frozen` / `solids_percent` attributes if the PLC should see them.

**Repair** - `plant.repair(tag)` / `plant.repair_all()` reset condition: leaks, pipe blockage/fouling/ice,
pump and compressor wear, stuck valves, and settled solids. Commands, trips (cleared with ResetCmd) and the
material inside are untouched. It models maintenance, not replacement.

## Weather and long-term scenarios

The test plant's storage tanks exchange heat with ambient air (`ambient_ua` in W/K). Pipes have a separate
wall temperature, ambient heat exchange and thermal mass. Weather and degradation advance every
`plant.slow_interval` (10 simulated seconds by default), separately from the fast flow/PLC step.

For an offline scenario, for example:

```powershell
python Runtimes\run_plant.py --offline --duration 86400 --speed 1000 `
  --ambient-mean 5 --daily-temperature-amplitude 8 `
  --seasonal-temperature-amplitude 15 --slow-time-scale 3600
```

`--slow-time-scale` accelerates weather and degradation only; it does not speed up flow, reactions, or
PLC scans. Weather configuration, slow-clock progress and partial slow intervals are saved in snapshots.
Components can provide `boiling_point_c`, `freezing_point_c`, `solubility_25c` and
`solubility_slope` attributes to enable the corresponding approximate behaviours. The plant builder adds
a missing water `freezing_point_c` without overwriting other water attributes.

## Layout

| Folder | Contents |
|---|---|
| `Modules/Entity` | Equipment and instruments (the physics and the signals) |
| `Modules/Flow` | `chemistry.py` (components, reactions) and `flow.py` (`Material`: moles per component, temperature) |
| `Modules/Plant` | `PlantModel`: finds the lines between nodes, solves pressure and flow, moves the liquid |
| `Modules/PLC` | ADS link and the bridge between signals and PLC variables |
| `Data` | Database models, loader, chemistry and snapshot storage, tag table, the test plant builder |
| `Runtimes` | `run_plant.py` |
| `PLC` | Generated variable list and an example PLC program |
| `tests` | `python -m unittest discover -s tests` |