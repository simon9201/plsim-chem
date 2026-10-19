# plsim-chem

A process-simulation and PLC-integration prototype. The plant is simulated in Python, stored in a
database, and interfaced with a TwinCAT PLC over ADS: the PLC reads simulated sensors and commands
simulated equipment through the same signal boundary used by the runtime. The project also includes a
plant editor with a 3D view, scenario authoring (leaks, grid disturbances, snow, ...), and an AI
condition-monitoring prototype that learns from operator-visible data.

```
Python simulation  <--ADS-->  TwinCAT PLC runtime  <--ADS-->  TwinCAT HMI
```

This is a **product prototype**, not yet a validated process-design, operational deployment, or safety
instrument. See `ARCHITECTURE.md` for the code map and "Model scope" below for current model limitations.

## Contents

1. [Quick start: connect the PLC layer](#quick-start-connect-the-plc-layer) and the [Demo plant tour](#demo-plant-tour)
2. [Setup](#setup) and [Run it against TwinCAT](#run-it-against-twincat)
3. [Signals](#signals) - the PLC variables
4. [Model scope](#model-scope) - what is simulated
5. Physics topics: reactions and lab, gas/VLE/solids/repair, elevation and 3D, process units, weather, electrical
6. [Plant editor, scenarios and GUI](#plant-editor-scenarios-and-gui), [PLC layer in the GUI](#plc-layer-in-the-gui)
7. [AI watchdog](#ai-watchdog-current-capability-and-validation)
8. [Extending the simulator](#extending-the-simulator) and [Layout](#layout)

## Quick start: connect the PLC layer

| I want to ... | Do this |
|---|---|
| Take the guided tour | Launcher: **Build demo plant (guided tour)**, then follow [Demo plant tour](#demo-plant-tour) |
| Explore the reactor and slow-degradation physics | Launcher: **Build reactor training plant (CSTR + PFR)**: a stirred and a tubular reactor line, each with a long cold product pipe. Scenarios: *Normal operation*, *Cooling loss*, *Wall scaling* (product plates out on the cold wall: flow sags, pressure falls after the line) and *Snow day* |
| Start everything from one window | `python launcher.py`: one window with a **Launcher** tab plus the Plant, Scenario, Run, AI health and PLC tabs, all on the same plant. The Launcher tab also manages designs and snapshots and starts external programs (offline/TwinCAT runtime, AI training, live watchdog, tests) |
| Back up or share a plant design | Launcher: **Export design to file...** / **Import design from file...** (JSON, layout only) |
| Save or resume a plant's running state | Launcher: select a snapshot (restored when a runtime starts) and fill *Save on stop as*; or **Save/Load snapshot** on the Run tab; snapshots can be exported/imported as JSON |
| Inspect the model without a PLC | `python Runtimes\run_gui.py`, open the **Run** tab, press **Start** |
| Design my own plant | **Plant** tab: Add equipment, Connect nodes, set parameters, set elevation (z), **Save** |
| Look at it in 3D | tick **3D view** on any graph (drag = rotate, wheel = zoom) |
| Simulate an upset | **Scenario** tab: select a node, add a fault/weather/electrical event or preset, **Save**, then **Run** |
| Connect the TwinCAT PLC layer | `python Data\build_test_plant.py`, paste `PLC\GVL_Sim.st` into TwinCAT, then `python Runtimes\run_plant.py` |
| Watch the PLC interface | **Run** tab: PLC link = Loopback (play the PLC by hand) or ADS; open the **PLC** tab |
| See what the AI thinks is wearing out | **AI health** tab while Run is active; **Repair selected** like a mechanic would |
| Train the stoppage predictor | `python Runtimes\train_watchdog.py`, then `python Runtimes\run_watchdog.py` |
| Check that nothing broke | `python -m unittest discover -s tests` |

### Typical workflows

**A. Build a plant and a scenario (no PLC).** Plant tab: *New*, add a `source`, `pipe`, `pump`, `storage_tank`,
`sink`; Connect them in flow order; add a `flow_sensor` by choosing it and clicking a pipe; **Validate**, **Save**.
Scenario tab: pick the plant, click the pipe, press the *leak* preset, **Save**. Run tab: **Start**. The pipe turns
red, the AI health tab shows the risk rising from the flow imbalance.

**B. Drive it from a PLC.** Build the plant (the test plant is created by `Data\build_test_plant.py`), run
`sync_tags` (done by the builder and the GUI) so every signal has a `GVL_Sim.<tag>_<signal>` variable, paste
`PLC\GVL_Sim.st` into TwinCAT, start the PLC runtime, then run `Runtimes\run_plant.py`. Use the PLC tab to see
which commands the plant follows and which it does not (a pump told to run that is tripped, a valve stuck).

**C. Exercise PLC and monitoring responses.** Mix solids in too fast (`vessel.add_solids`, scenario `charge`), stop
an agitator (`mix_rate` = 0), run a pump dry or throttled, lower the supply voltage, or drop the outdoor temperature.
Use the scenario to generate operating data, then inspect PLC feedback, alarms, and sensor-based health estimates.

**D. Long-term degradation.** Set `slow_time_scale` (Run tab or `--slow-time-scale`) to compress weather,
freeze-thaw, fouling and motor ageing. Repair with `plant.repair(tag)` or the AI health tab.

## Demo plant tour

`demo_plant` is a small ethanol/water slice laid out by hand (not auto-arranged) that uses most of the modules.
Create it with **Build demo plant (guided tour)** in the Launcher (or `python Data\build_demo_plant.py`); it is
selected and opened in the Plant tab automatically. Its TwinCAT variables go to `PLC\GVL_Sim_demo.st`.

| Lane | Nodes | What it shows |
|---|---|---|
| 1 (top) | `S301` feed -> `XV301` -> `TK301` (raised 2 m) -> `P301` -> `HX301` pre-heater -> `C301` column | elevation head on the pump suction, heat exchange, distillation; the column's distillate goes to `TK302` -> `P302` -> `SK302` (product), its bottoms through `XV302` to `SK301` (waste) |
| 2 (middle) | `S302` water -> `P303` -> `EV301` evaporator -> `SK303` | phase change; the steam leaves as gas through `GP302` (4 m up) to `GSK302` |
| 3 (bottom) | `GS301` N2 -> `CP301` compressor -> `GP301` -> `GV301` buffer -> `GVV301` -> `GSK301` | gas pressure build-up and flow |

1. **Plant tab, 2D** (3D unchecked): the connections. Click a node: the *Interface of selected* panel lists its
   CONTROLS (what a PLC writes) and DATA (what it reads); the parameters are editable below. Sensors `LT301`, `FT301`,
   `AT302` (ethanol analyser) and so on hang off their targets.
2. **Plant tab, tick 3D view**: the same plant in 3D. `TK301` and the steam line `GP302`/`GSK302` stand above the
   others (z is in metres). Drag to rotate, wheel to zoom.
3. **Scenario tab**: pick *Normal operation* (it starts every pump, valve, heater and the compressor with
   `command` events, playing the PLC's role) and look at the other saved ones: *Leak on TK301*, *Snow day*, *Demo upset*.
   Each already contains the same start-up, so any of them runs on a live plant.
4. **Run tab**: choose the scenario, leave PLC link = *Off*, **Start**. Watch `TK302` fill, the column's top/bottom
   temperatures separate, `GV301` pressure rise towards 6 bar, and the product pump `P302` start after 15 minutes.
   Use *slow time scale* to speed weather. With PLC link = Loopback or ADS the PLC's commands override the scenario's
   start-up events every cycle.
5. **AI health tab** (while Run is active): the watchdog sees only operator-visible tags. Run *Demo upset* or the
   leak and see whether it flags the affected equipment (it needs a trained model, see the AI watchdog section, and
   its recall is still limited); **Repair selected** clears the cause like a mechanic would.
6. **PLC tab**: shows each command the plant is following. **Run tab: Lab sample of selected** on `TK302` gives the
   ethanol assay a plant operator would get from the lab.

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
| Pump | `RunCmd`, `ResetCmd`, `SpeedSP` (variable speed only) | `RunningFb`, `Fault`, `Speed` (variable speed only), `Current` (A), `WindingTemp` (degC), `Voltage` (V), `Vibration` (mm/s) |
| Sensor | | `Value` (engineering units), `Raw` (0..32767 over the range) |

`OpenCmd` and `PositionSP` set the same valve position; use one of them in your PLC program (the one that changes last wins). A `SpeedSP` of 0 that was never written is ignored, so a pump without speed control runs at 100 %.

Valves and pumps move gradually (travel time, ramp). A pump trips if it runs with an empty tank and
stays faulted until `ResetCmd`. A pump also cavitates when its suction pressure falls below the liquid's
vapor pressure plus `npsh_required` (closed/throttled suction valve, low level, hot liquid): head drops and
it trips after `cavitation_trip` severity-seconds. Alarms and interlocks are the PLC's job: the simulation has none.

Fault injection for modeled operating upsets: `plant.set_fault(tag, name, value)`. `leak_rate` (L/s) works on every
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

This is an **engineering prototype**, not a validated design, operations, or safety-validation tool. Its current
scope is liquid transfer, tank inventory, simple reactions, pump/valve operation, sensors, and integration
testing of a TwinCAT PLC layer.

- **Hydraulics:** pressure-driven, incompressible liquid; bar and L/s. Pumps add a simplified quadratic
  head curve; pipes, valves and fittings add quadratic resistance. Tank liquid height and static head are
  approximated from volume and a constant tank cross-section. Junction pressure is solved iteratively.
- **Chemistry and heat:** ideal mixing and component-weighted properties. All reactions of a reactor advance
  together (`Modules/Flow/kinetics.py`, adaptive RK4), so parallel, consecutive and reversible reactions are
  correct. See "Reactions, batches and the lab" below. CSTR cooling is represented by a UA term.
- **Faults and ageing:** leaks, pipe blockage/fouling/freezing, pump cavitation and wear are qualitative
  behaviours; rates and thresholds are illustrative, not calibrated equipment data.
- **Gas, VLE and solids (basic):** a separate gas network, ideal-gas headspace, Antoine/Clausius-Clapeyron vapor
  pressures and a two-phase flash drum (see the sections below). Solids precipitate inside vessels; freezing is an error state.
- **Electrical:** supply sag/noise/dips, motor current, winding heat and insulation ageing for pumps and compressors
  (illustrative numbers; no phase imbalance, harmonics or starting transients).
- **Not modelled:** line inertia / water hammer (hydraulics are quasi-steady), thermal mass of pipes' contents, heat of
  mixing, PFR, batch, filter, crystallizer and absorber units, rigorous column dynamics (tray hydraulics, feed
  enthalpy), non-constant latent heat, choked gas flow, Joule-Thomson, condensation inside gas lines, gas
  junctions (use a `gas_volume` as manifold), viscosity-dependent hydraulics, validated property packages.
  Add those before using the simulator for design or relief sizing.

Equipment types for `equipment.type`: `storage_tank`, `cstr`, `pump`, `valve`, `check_valve`,
`utility_valve`, `pipe`, `elbow`, `manifold`, `tee`, `source`, `sink`, `heat_exchanger`, `evaporator`, `dryer`,
`distillation_column`; gas side: `flash_drum`, `gas_volume`, `gas_source`, `gas_sink`, `gas_pipe`, `gas_valve`, `gas_check_valve`, `compressor`. Instrument types:
`flow_sensor`, `level_sensor`, `pressure_sensor`, `differential_pressure_sensor`,
`temperature_sensor`, `analyzer`.

### Performance and a Rust/C rewrite

Measured on this machine (Python 3.11): the 23-unit test plant steps in 0.44 ms (about 226x real time at dt = 0.1 s),
and a distillation column costs about 7 ms per step (its bubble-point loop). The Python engine stays: a PLC scan
needs ~100 ms. We are approaching the edge only for columns. Move to native code when: a plant has several columns
or more than ~500 nodes, dt must drop far below 10 ms (water hammer), or a rigorous column needs a Newton solve.
Order of attack: numpy-vectorise the tray bubble points, then numba/a small Rust extension for that one routine.
Known physics limits: quasi-steady incompressible hydraulics, Raoult-only VLE by default (NRTL available), ideal
liquid mixing, simplified gas.

## Solids that have not dissolved

A vessel can hold suspended (undissolved) solid in `slurry`: charge it with `vessel.add_solids("salt", mol)`, or it
appears when material is added faster than it can go into solution. It dissolves at `DISSOLUTION_RATE * mix_rate`
(set `mix_rate = 0` for a stopped agitator, and a cold liquid with a low `solubility_25c` dissolves less), and what
cannot dissolve settles (`SETTLING_RATE`) into `solids`. Suspended solid leaves with the outflow, so sending a
tank on too early carries slurry into the line. A pipe sees undissolved load (`slurry_load`, mol/L above saturation)
and packs up (`fouling`, rate `SLURRY_CLOG_RATE`); once it is more than 20 % closed the restriction creates pressure
hotspots that add `fatigue` (`HOTSPOT_RATE`), which eventually makes the pipe weep. `repair` resets both.

## PLC layer in the GUI

On the Run tab choose **PLC link**: Off (simulation alone), Loopback (you play the PLC by hand) or ADS (TwinCAT;
enter the AMS Net Id). The **PLC** tab then lists every mapped tag with its direction, live value and age, and
flags what an operator would see: a tag mapped to a missing signal, a frozen sensor, a NaN, a pump or valve not
following its command (`FOLLOW_PAIRS` in `Modules/PLC/monitor.py`), equipment fault flags, and a lost link
(the plant then runs on fail-safe commands). The logic is GUI-free (`PlcMonitor`, `PlcCycle`) and does not model the
PLC program, only the interface.

## Colours

Nodes are coloured by equipment type, vessels show their liquid level and temperature (blue cold, red hot), pipe
lines get thicker and bluer with flow, and states override: red fault or trip, ice blue frozen, orange solids or
slurry building up. All colours live in `Modules/GUI/theme.py`.

## Reactions, batches and the lab

**Reactions.** A `Reaction` has Arrhenius rate constants, reaction orders (inhibitors and catalysts are just extra
orders), and an optional reversible side: `reversible=True` with `k_equilibrium_ref` (Kc at 25 C). Kc follows
van 't Hoff from the heat of reaction and the reverse rate is `k / Kc`, so the reaction relaxes to equilibrium
(exothermic ones favour product when cold). The heat of reaction is taken at the real temperature (Kirchhoff:
`Reaction.enthalpy_at`) and goes into the energy balance of the reactor. Default chemistry adds the impurity `I`,
the off-spec product `X` and the dimer `D` with the reactions `A_I_to_X` (side reaction) and
`C_dimerization` (2 C <-> D, reversible). Reactors only run the reactions listed in `reactions=[...]`.
Old databases are migrated automatically (new `reaction` columns); re-run `Data\build_test_plant.py` for a fresh plant.

**Batches of varied quality.** `Modules/Flow/batch.py`: `make_lot(base, assay, impurities)` and `random_lot`.
`Source.set_lot(composition, lot_id)` changes what a source delivers. Scenario actions:
`feed_lot` (value = assay, `data` = `{"impurities": {"I": 0.02}, "lot": "L7"}`, reverts after its duration),
`charge` (add a batch into a vessel: `data` = `{"composition": {...}, "volume": L, "temperature": C}`) and
`sample` (send a lab sample). The scenario tab has a JSON "Data" field for them.

**Lab.** `plant.lab` (`Modules/Lab/lab.py`). `take_sample(tag)` really removes liquid from a vessel (or catches a
stream from a source for incoming lot checks). The result arrives after `turnaround` seconds, with relative noise and a
detection limit: `wt.<component>` (mass %), `density`, `temperature`. `lab.specs[tag] = {"wt.A": (low, high)}` flags
out-of-spec results. `with_lab(sampler, lab, tags)` adds `LAB.<tag>.<analyte>` and `LAB.<tag>.age` channels (held
until the next result) so the recorder and the watchdog models can use lab data together with PLC signals.
The Run tab has a "Lab sample of selected" button and a results list. The lab queue is not part of plant snapshots.

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

**Compressor** - signals RunCmd, SpeedSP, ResetCmd, RunningFb, Fault, Surge, Speed, Power (kW), DischargeTemp, plus the
motor signals Current, WindingTemp, Voltage, Vibration (see the electrical section).
Pressure ratio falls with flow from `max_ratio` at shutoff. It needs a downstream volume; a stopped compressor
blocks the line. Running below `surge_fraction` of its (speed-scaled) `max_flow` is surge: it wears the machine
and trips after `surge_trip` s. It also trips above `max_discharge_temp`. Trips clear with ResetCmd.

**Flash drum (`flash_drum`)** - a vessel with a headspace. Ports named `gas*` / `vapor*` join the gas network, the
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

## Elevation and the 3D editor

Every piece of equipment has a `location` (x, y, z in metres). Each pipe run adds static head
`rho*g*dz` from the elevation difference, so pumping uphill is harder than downhill, and a high pump
can cavitate. In the editor, set a node's z in the plant tab ("Elevation z (m)" + Set). The "3D view"
checkbox switches the graph canvases (plant, scenario, run, health) to an orthographic 3D view: drag the
background to rotate, wheel to zoom, drag a node to move it on its floor plane. Tanks, valves and pumps
are drawn as simplified vector shapes. Pipe `length` is not yet derived from the 3D geometry.

## Process units (oil, paper, plastics, pharma)

| Type | Signals / faults |
|---|---|
| `heat_exchanger` | DutyCmd, Duty, OutletTemp; epsilon-NTU against `utility_temp`; `scaling` fault |
| `evaporator` | EvapRate; heater `scaling` |
| `dryer` | AirCmd, Moisture; hot-air drying, `thermal_damage` state |
| `distillation_column` | HeaterCmd, RefluxCmd, TopTemp, BottomTemp, Boilup, DrumLevel, ColumnDp, Flooding; ports `feed`, `bottoms`, `distillate`; faults flooding, weeping, tray `fouling`, reboiler `scaling` |
| `pfr` | Jacketed tubular reactor (a CSTR subclass with a small holdup); same cooling signals as the CSTR; jacket `fouling` fault. Lumped: no axial profile or hot spot |

The column uses equilibrium stages with Murphree efficiency, constant molar overflow, a reboiler and a
total condenser. For realistic ethanol-water use `thermo.set_activity_model(thermo.nrtl_model({...}))`
(Raoult is the default). Simplifications: no feed enthalpy balance, constant efficiency, instant liquid
travel between trays, total condenser only. Not built yet: a spatially resolved plug-flow reactor, filter, crystallizer,
absorber. Hooks for new units: `Transfer.condition_liquid` (change a stream passing an edge element) and
`ProcessEquipment.draw_at/receive_at/max_outflow_at/max_inflow_at` (per-port behaviour).

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

## Electrical supply and motors

`plant.power` is one shared `PowerSupply` (Modules/Plant/power_supply.py). Its `voltage_factor` is
`1 + offset + wander - active dip`: `offset` is a steady sag/surge, `noise` / `noise_tau` give seeded random
wander, and `add_event(start, duration, depth)` schedules a dip. The default is a perfect supply, which leaves the
rest of the model unchanged. Every pump and compressor owns a `Motor` (Modules/Entity/Process_equipment/Motor)
attached to the supply:

- Current rises as voltage falls (constant power) and with worn insulation; winding temperature follows
  `ambient + 80 K * (I/I_rated)^2` with a 10 min lag.
- Speed is lost only below 80 % voltage. A moderate sag therefore keeps the load and overheats the motor.
- Trips: `undervoltage` (< 70 %), `overtemperature` (> 140 degC) and `burnout` (insulation used up).
  Insulation ages by the 10-degree rule in the slow loop, so `slow_time_scale` accelerates it.
- Undervoltage and overtemperature clear with ResetCmd; burnout does not, only `plant.repair(tag)` does.
- Vibration grows with pump wear, cavitation, deadheading and compressor surge.

Example: `plant.power.noise = 0.02; plant.power.offset = -0.12; plant.power.add_event(600, 5, 0.3)`.
Re-run `python Data\build_test_plant.py` so the new signals reach `GVL_Sim` and the tag table.

## Plant editor, scenarios and GUI

```powershell
python Runtimes\run_gui.py
```

A Tk window (no extra packages) with five tabs: Plant, Scenario, Run, AI health and PLC. It is a thin layer: all logic lives in GUI-free modules that are tested on their own.

- **Plant**: design a plant as a node graph. Modes: Select (edit a node's parameters as JSON, drag to move), Add (equipment on empty space, instruments on a node), Connect (click source, then destination), Delete. Nothing is written until **Save**; **Revert** drops changes; **Validate** builds the plant and takes a step. Backed by `Modules/Editor/document.py` (`PlantDocument`), which only reads and writes the database. Equipment types and their default ports and parameters are in `Modules/Editor/catalog.py`; register a new equipment class there when you create one. Node positions are stored as `_ui` in the node's params (ignored by the loader).
- **Scenario**: click a node to target it, then add events: a fault (`leak_rate`, `blockage`, `fouling`, `fatigue`, `wear`, `stuck`), a numeric parameter change (any attribute, which is how you tune degradation), a supply dip or surge, supply offset or noise, an ambient temperature offset, or a repair. Events can last a limited time and can ramp in. Presets: leak, lightning day, snow day, heat wave, brownout. Scenarios are stored per plant in the database (`Data/scenarios.py`; `Modules/Scenario/scenario.py` has `Scenario` and `ScenarioPlayer`, which scripts can use too). Saving a scenario commits the session, including unsaved plant edits.
- **Run**: simulates the plant offline with the current scenario and shows live faults (red nodes) and the signals of the selected node. Times are plant seconds, so with a large *slow time scale* use shorter durations.
- **AI health**: while the **offline Run tab** is simulating, estimates equipment health from measured sensor values and inferred flow-balance / resistance residuals. The graph colours monitored nodes by estimated risk; selecting one shows contributing channels, a risk history, and a rough time-to-alarm extrapolation. “Repair selected” applies the plant's maintenance reset and starts a new baseline for that equipment; repairs from scenarios are also detected. “Accept as new normal” resets the baseline without counting maintenance. Nodes without measured signals are marked unmonitored. This live view is a sensor/lab-driven anomaly and degradation estimate, not a trained stoppage probability; the separately trained watchdog predicts stoppages from labeled history. The GUI health tab currently observes the simulator, not a PLC connection.

Any piece of equipment can leak (`leak_rate`), and leaks now show up downstream: a flow sensor after a leaking pipe reads less than one before it.

### Freeze-thaw fatigue

Pipes accumulate `fatigue` each time the wall freezes and then thaws (`freeze_thaw_cycles` counts them; a thaw needs the wall 2 K above freezing). Damage per cycle is `fatigue_per_cycle * (0.5 + peak_ice)`. Above 0.5 fatigue the pipe starts to leak, up to `fatigue_leak_max`. Tune the degradation per pipe through database parameters or a `param` scenario event: `fatigue_per_cycle`, `fatigue_leak_max`, `ice_rate_scale` and `fouling_rate_scale`. `repair` resets it. Fatigue is deliberately not a PLC signal; the watchdog only sees it through sensors.

## AI watchdog: current capability and validation

**What the AI may see (the product boundary).** Inference uses only PLC feedback, measured sensor data, lab
results once reported, plant drawings, and work-order records. PLC commands are operator-visible and available
to `OperatorView`, but the supervised predictor is trained and run on feedback channels only. Hidden condition
(leak size, fouling, blockage, fatigue, scaling, wear, insulation used, stuck actuators) is not a model input;
it can only be inferred from its effects on operator-visible data. This separation is implemented and tested:

- All AI input goes through `OperatorView` (`Modules/Watchdog/access.py`), which holds no public reference to
  equipment. `health`, `recorder` and the Health tab read through it.
- `audit_hidden_state(plant)` perturbs hidden condition variables and checks that no exposed signal changes
  directly. Physical effects after the plant advances are legitimate evidence for the AI.
- `Modules/Watchdog/evaluation.py` (`GroundTruth`) is reserved for scoring and the Health tab's optional
  ground-truth display. The inference modules cannot import it; `tests/test_ai_access.py` scans the sources.
- Rule for new equipment: never add a `Signal` that returns a degradation variable. Run the audit test.

This is a code-level data boundary, **not an in-process security sandbox**: a Python process with access to
the simulator can introspect its objects. A deployed product needs process/OS isolation, credential separation,
read-only PLC permissions and independent security review. `run_watchdog.py` only reads PLC feedback and never
writes to the PLC.

The supervised model predicts an observable target transition within a time horizon. Pipeline: record feedback
-> rolling features (last, mean, standard deviation, trend) -> label a target's rising edge within the horizon
-> `RiskModel` -> `Watchdog`. The live watchdog requires two consecutive above-threshold model updates to raise
an alarm and three below-clear-threshold updates to clear it, reducing chatter. Model scores rank risk; they are
not calibrated probabilities.

```powershell
python Runtimes\train_watchdog.py
python Runtimes\run_watchdog.py                         # live, read-only over ADS
python Runtimes\run_plant.py --duration 7200 --record Data\train.csv
python Runtimes\train_watchdog.py --csv Data\train.csv --validation-csv Data\validation.csv --test-csv Data\test.csv --target P101.Fault --plant test_plant
```

The CSV workflow is a chronological/episode-level split: training data fits the model, validation data selects the
alert threshold under `--max-false-alarms-per-hour` (default 0.1), and test data is reported once as held-out
evaluation. Use separate runs/dates for the three sets; do not split adjacent windows from one run across sets.
Each recording must contain the target status and the operator-visible feedback signals. `run_plant --record`
records PLC feedback (including status bits), not PLC commands. If a plant is supplied, its flow-balance and
resistance residual channels are appended consistently to all sets.

The artifact stores a format/model version, training timestamp, channel and data fingerprints, source file hashes
or synthetic episode seeds, evaluation metrics, and the selected operating threshold. Evaluation reports ROC AUC,
event recall, alert precision, false alarms per operating hour, and median warning lead time overall and by
synthetic failure mode. If validation cannot find an alerting threshold within the false-alarm budget, the model
is marked as having no acceptable alert threshold and defaults to no alert rather than hiding the trade-off.
Validation and test results are required before treating any threshold as operational.

`RiskModel` is gradient-boosted trees by default (`Modules/Watchdog/trees.py`, numpy only); use `--model logistic`
for the linear baseline. The window and sampling period are controlled by `--window` and `--period`.

**Synthetic training data.** `Modules/Watchdog/scenarios.py` generates reproducible "bad days" and
`Modules/Watchdog/faults.py` shapes each fault in time. Variety is deliberate so the model learns faults, not
scenarios:

- *Plant families:* `loop` (tank, pump, valve, line), `transfer` (two tanks with a gravity return), `heated`
  (the loop plus a heat exchanger under temperature control), `reactor` (feed tank, pump, valve, a jacketed CSTR
  running the exothermic `A + B -> C` reaction, drain) and `tubular` (the same with a small-holdup jacketed PFR that
  heats up in seconds). The reactor families have a PI cooling-water loop (`XV210`, reactor temperature `TT201`)
  and a high-high temperature interlock that trips the feed pump. The jacket is auto-sized to the feed so a
  healthy plant sits at a 30-60 % open cooling valve. The valve may be on the suction or discharge side.
- *Failure modes (17):* suction-valve, hot-liquid and intermittent cavitation, grid-voltage drift, line fouling, line
  blockage, tank leak, pump wear, a seized valve, heater runaway, **loss of reactor cooling** (cooling valve
  seizes), **coolant-side fouling** (hidden `fouling` on the reactor jacket) and **warm cooling water**, plus
  two-fault compounds. A continuous reactor has no true thermal runaway (depletion and throughput limit it), so
  the reactor modes show up as slow temperature rises that reach the interlock only sometimes; the rest are
  realistic non-stop data. Fault-free days
  (`benign_transients`: operator speed/setpoint changes and brownouts; `sensor_nuisance`: spikes, freezes and
  calibration offsets) and clean `baseline` days must **not** raise alarms and count as false alarms if they do.
- *Fault shape:* each fault draws a ramp, accelerating, sudden step, saw-tooth (cleaned then regrows) or sporadic
  burst profile with a random onset, speed and size, so "the fault starts at minute X" cannot be learned.
- *Instrument and control realism:* per-episode noise, drift, lag, calibration offset, outlier spikes, frozen
  readings and valve stiction (`OperatorView` applies them), plus PI flow and temperature loops and operator actions.
- *Labels:* the stop label is the PLC-visible `P101.Fault`. `TRUTH.FaultActive` marks the fault onset for evaluation
  only and is never a model input (`dataset.py`/`training.py` drop `TRUTH.*` and `EVENT.*`).

**Normal-only detector and the campaign plant.** Failure data is scarce, healthy data is not, so training also fits
a `SubspaceDetector` on fault-free episodes only (`model.py`; saved next to the model as `<name>.anomaly.npz`).
Its score is 1.0 at the edge of normal; a validation-selected threshold under the same false-alarm budget is
stored in the model metadata (`selected_anomaly_threshold`) and `run_watchdog.py` prints an *advisory* when the
plant looks unlike normal operation, which can flag failure types never seen in training. Training reports its
held-out numbers as `anomaly_test_*` beside the supervised `test_*`. Plants differ in their sensors, so the model
uses the union of channels; a channel a plant lacks reads 0.0 (`optional_channels` in the model).

`scenarios.run_campaign(seed, hours)` is the "training plant": one plant living through several incidents at
random times, each with its own shape and severity, under a day/night and seasonal ambient cycle. After a trip
(or when an incident ends) a simulated operator repairs everything, refills the feed tank and restarts the pump
once a hot reactor has cooled. `python Runtimes\train_watchdog.py --campaigns 24 --workers 8` adds these long
runs to training (and a third as many to validation and test).

**Claim, stated plainly.** All training and test data here is synthetic. The measurable claim is "detects
seeded faults X minutes before a trip at Y false alarms per hour on held-out simulated plants", not "predicts
real plant accidents". Public sets (Tennessee Eastman, SWaT) have different tags, so they serve as an external
benchmark of the method through the CSV path, not as training data for this plant.

Unannounced scheduled grid dips stay out of the stop-prediction labels: an unannounced future event has no
pre-event signal to learn from. They remain useful for PLC alarm/recovery validation.

**Large training sets.** `Modules/Watchdog/corpus.py` generates episodes once, in parallel, into a folder of `.npz`
files (one per seed; episodes from another generator version, length or sample period are regenerated, so stale
files are never mixed in). Use it from the trainer:

```powershell
python Runtimes\train_watchdog.py --episodes 400 --validation-episodes 100 --test-episodes 100 --corpus Data\corpus --workers 8
```

The default in-memory run (40/20/20 episodes) takes about 7 minutes on one core; `--corpus` with `--workers` makes
repeat and larger runs practical. `Data/corpus/` is gitignored. Bump `GENERATOR_VERSION` in `scenarios.py` whenever
the generator's behaviour changes.

Product-readiness limits remain material: the synthetic dataset is a small reference pump-loop study, not
evidence of generalization across real plants; lab channels are used by the GUI health monitor when results are
released, but are not yet part of the trained/live ADS watchdog data pipeline; and the AI health tab is a
sensor/lab-driven anomaly estimate, not the supervised predictor. Validate with independent historian data,
asset/failure-specific targets, and operator-reviewed alarm policies before any operational use.

**Slow degradation (generator v5).** The reactor and tubular families now end in a long, poorly insulated product
line (`L201`, with `PT201`/`PT202` before and after it). The new `wall_scaling` mode makes the product C less soluble
when cold (set per episode, restored afterwards) and ramps the line's deposition rate from zero: the real
`Pipe.slow_update` chemistry then plates C onto the cold wall. The result is slow: flow sags over tens of
minutes, the pressure after the line falls, and about half the episodes eventually trip on low flow. No
degradation variable is visible to the AI; it sees only the transmitters.

**Current v5 result (generator v5: 18 failure modes, five plant families, 340 training / 68 validation / 68 test
episodes plus 32 eight-hour training campaigns and held-out campaigns; 196 held-out operating hours):** the
supervised model reaches test ROC AUC 0.85 and detected 12 of 36 trips (33 % event recall, median lead 128 s) at
0.041 false alarms/hour (budget 0.1). The normal-only detector caught 1 of 36 (AUC 0.76, 0.031 false alarms/hour),
so it remains an advisory only. Slow modes are still the weakest; counts are small, so treat these as direction
only and re-measure after any generator, feature or training change.

**Previous v4 result (generator v4: five plant families, 17 failure modes, plus 24 six-hour campaigns in training;
255 training / 51 validation / 51 test episodes plus 8 test campaigns, 126 held-out operating hours):** the
supervised model reaches test ROC AUC 0.77 and, at its validation-selected threshold (0.89), detected 10 of 45
trips (22 % event recall, median lead 232 s) at 0.103 false alarms/hour (budget 0.1; precision 0.44). The
normal-only detector has a similar AUC (about 0.74 on the episode-only test set) but at a threshold that keeps
false alarms within budget (0.024/hour) it caught 1 of 45 trips, so as built it is an advisory, not a predictor.
The supervised model's catches concentrate on cavitation, heater-runaway and some cooling-loss/leak trips; blockage,
fouling, grid drift, pump wear and the slow reactor modes were mostly missed. This is lower than the v3 figure
(5 of 8) because the test distribution is now far broader (reactor families, campaigns), not because the model
got worse; the two are not comparable. Counts are small, so treat them as direction only. Next levers: longer
horizons or trend features for slow degradation, per-family thresholds, and training the detector per family.
Re-measure with a larger corpus after any generator, feature or training change.

**Leaks and blockages from sensor differences.** `Modules/Watchdog/residuals.py` finds flow sensors on the same unbranched line and differential-pressure/flow pairs in a stored plant and adds `RES.*.balance` (flow in minus flow out) and `RES.*.resistance` (dP against flow squared) channels. `ResidualMonitor` is fitted on healthy data only and returns a z-score (above 5 = alarm) with the worst channel. Pass `--plant NAME` together with `--csv` to add these channels to recordings.

The generator and evaluation code live in `Modules/Watchdog/scenarios.py` and `training.py`. AUC alone is
insufficient: use event recall, false alarms per operating hour and warning lead time, especially for each
failure mode. Abrupt unannounced grid dips are not predictable from pre-event signals. The model only supports
the channel names it was trained on, so a plant or instrumentation change requires retraining and independent
validation. Upgrade paths behind the `fit` / `predict_proba` / `save` / `load` interface include scikit-learn,
river for controlled online learning, or PyTorch, but the current data and validation limitations need to be
resolved before a more complex learner is justified.

## Extending the simulator

- **New equipment:** subclass `ProcessEquipment` (or `Vessel`, `Transfer`), declare `FAULTS`, implement `signals()`,
  `get_state`/`set_state`, and `repair`. Register the class in `Data/loader.py` (`EQUIPMENT_TYPES`), add a
  `_t(...)` entry in `Modules/Editor/catalog.py`, and (optionally) a shape in `Modules/GUI/view3d.py` and a colour in
  `Modules/GUI/theme.py`. Look at `Modules/Entity/Process_equipment/HeatExchanger` for the smallest example.
- **New component or reaction:** add rows in the database (`Data/chemistry.py` defaults) with the optional attributes
  listed under "Gas, vapor-liquid equilibrium, solids and repair".
- **New fault or degradation:** add the name to `FAULTS`, apply it in the equipment's `update`/`slow_update`, clear
  it in `repair`; the scenario tab and `set_fault` pick it up automatically.
- **New PLC check:** add a row to `FOLLOW_PAIRS` or a rule to `PlcMonitor.problems` in `Modules/PLC/monitor.py`.

## Layout

| Folder | Contents |
|---|---|
| `Modules/Entity` | Equipment and instruments (the physics and the signals) |
| `Modules/Flow` | `chemistry.py` (components, reactions), `kinetics.py` (coupled reaction solver), `thermo.py` (VLE, NRTL), `batch.py` (lots) and `flow.py` (`Material`, `Gas`) |
| `Modules/Lab` | `Laboratory`: simulated lab samples, and `with_lab` for the AI channels |
| `Modules/Plant` | `PlantModel` (liquid network), `GasNetwork`, `Environment` (weather), `PowerSupply` |
| `Modules/PLC` | ADS link, the bridge between signals and PLC variables, and `PlcCycle`/`PlcMonitor` for the PLC tab |
| `Data` | Database models, loader, chemistry and snapshot storage, tag table, the test plant builder |
| `Modules/Watchdog` | AI watchdog: recorder, features, dataset, logistic and boosted models, residual monitor, health monitor, live watchdog, training scenarios |
| `Modules/Editor` | `PlantDocument` (database editing API) and the equipment catalog |
| `Modules/Scenario` | Scenario events, player and presets |
| `Modules/GUI` | Tk window: Plant, Scenario, Run, AI health and PLC tabs; `view3d.py`, `projection.py` and `theme.py` |
| `Runtimes` | `run_plant.py` (`--record`), `run_gui.py`, `train_watchdog.py` (`--model`, `--plant`), `run_watchdog.py`; `launcher.py` in the project root starts them and `Data/exchange.py` reads/writes design and snapshot JSON files |
| `PLC` | Generated variable list and an example PLC program |
| `tests` | `python -m unittest discover -s tests` (about 190 tests) |