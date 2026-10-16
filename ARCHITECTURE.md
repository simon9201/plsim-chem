# plsim-chem architecture

The liquid and gas networks are separate solvers. A flash drum is a liquid vessel whose `gas*` / `vapor*`
ports also connect to the gas network.

```mermaid
flowchart LR
    subgraph Config["Plant definition"]
        DB[(SQLite plant database)]
        Loader["Data.loader<br/>equipment and instrument registry"]
        Chemistry["Data.chemistry<br/>component and reaction data"]
        DB --> Loader
        Chemistry --> Loader
    end

    subgraph Runtime["Runtime and PLC integration"]
        Runner["Runtimes.run_plant"]
        Bridge["PlcBridge"]
        Link["AdsLink / LoopbackLink"]
        PLC["TwinCAT PLC"]
        Runner --> Bridge
        Bridge <--> Link
        Link <--> PLC
    end

    subgraph Model["Simulation model"]
        Plant["PlantModel<br/>time step, connections, state"]
        Liquid["Liquid network<br/>pressure and L/s flow"]
        GasNet["GasNetwork<br/>pressure and mol/s flow"]
        Equipment["Process equipment<br/>vessels, pumps, valves, pipes"]
        GasEquipment["Gas equipment<br/>volumes, sources, sinks,<br/>pipes, valves, compressors"]
        Flash["FlashDrum<br/>liquid vessel + gas headspace"]
        Instruments["Instruments<br/>flow, pressure, level,<br/>temperature, analyzers"]
        Flow["Material / Gas<br/>component inventories"]
        Thermo["thermo.py<br/>vapor pressure and VLE"]
        Environment["Environment<br/>ambient temperature cycles"]

        Plant --> Liquid
        Plant --> GasNet
        Plant --> Equipment
        Plant --> GasEquipment
        Plant --> Instruments
        Plant --> Environment
        Liquid --> Equipment
        GasNet --> GasEquipment
        Equipment --> Flow
        GasEquipment --> Flow
        Flash --> Flow
        Flash --> Thermo
        Equipment --> Thermo
    end

    Loader --> Plant
    Runner --> Plant
    Bridge <--> Plant
    PLC -. commands and feedback .-> Bridge
```

`PlantModel` advances equipment and instruments each simulation step. `PlcBridge` maps the equipment's
signals to PLC tags; the database loader constructs the model from stored equipment, ports and connections.
Thermodynamic property functions are independent of the plant solver, which leaves room to reuse them in
future separation equipment such as distillation stages.
