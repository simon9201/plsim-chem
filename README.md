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
stays faulted until `ResetCmd`. Alarms and interlocks are the PLC's job: the simulation has none.

The mapping from signal to PLC variable is the `plc_tag` table. Edit `plc_symbol` there to use your own
names, and the runtime follows.

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
| `tests` | `python -m unittest discover tests` |

## The model

- **Flow is pressure driven** (bar, L/s). Liquid moves from high to low pressure; a pump adds head, a valve/pipe/elbow loses pressure.
  Vessel outlets sit at the bottom (gravity head), inlets at the top. Manifolds and tees are junctions whose pressure is solved so flow in equals flow out.
  Only liquids are modelled (no gas, boiling or compressors yet).
- **Chemistry** lives in the database (component, eaction tables, seeded from Modules/Flow/chemistry.py). A reaction names its stoichiometry, rate constant,
  activation energy and either a heat of reaction or the formation enthalpies it is derived from. A reactor lists the reactions that happen in it. The values for A, B and C are placeholders.
- **Equipment types** for the equipment.type column: storage_tank, cstr, pump, alve, check_valve, utility_valve, pipe, elbow, manifold, 	ee, source, sink.
  **Instrument types**: low_sensor, level_sensor, pressure_sensor, differential_pressure_sensor, 	emperature_sensor, nalyzer.
- **Snapshots** store the running state of every piece of equipment under a name (Data/snapshots.py).
  Rebuilding the test plant keeps your edited PLC symbols and your snapshots, but a snapshot of an older layout will not load into a changed one.
