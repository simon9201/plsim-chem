// =====================================================================
// MAIN_example.st : starter PLC program for the test plant
// Paste into a TwinCAT PLC program called MAIN. Needs the GVL_Sim variables from GVL_Sim.st.
//
// What it does
//  - Keeps each feed tank between 70 % and 90 % by opening its inlet valve (hysteresis)
//  - Pumps feed from the tanks to the reactor while the reactor is below 70 %
//  - A pump only runs when its tank is not empty and its outlet valve is confirmed open
//  - Cooling water valve follows the reactor temperature (proportional control)
//  - Latching safety trip on high temperature or high reactor level
//
// Set bRun to TRUE (online, or from the HMI) to start. Set bReset to TRUE to clear a trip or a pump fault.
// Variables that the simulation writes (..Value, ..Raw, ..OpenFb, ..ClosedFb, ..RunningFb, ..Fault, ..Position, ..Speed)
// must only be read here. Variables ending in ..OpenCmd, ..RunCmd, ..ResetCmd, ..SpeedSP, ..PositionSP are yours to write.
// =====================================================================

PROGRAM MAIN
VAR
    bRun            : BOOL;             // operator: start the sequence
    bReset          : BOOL;             // operator: reset trip and pump faults
    bTrip           : BOOL;             // latched safety trip
    bTransfer       : BOOL;             // feed transfer to the reactor is allowed
    bFill1          : BOOL;
    bFill2          : BOOL;
    bFill3          : BOOL;

    rTempSP         : REAL := 40.0;     // reactor temperature setpoint, C
    rTempTrip       : REAL := 70.0;     // trip above this temperature, C
    rLevelMax       : REAL := 70.0;     // stop transfer at this reactor level, %
    rLevelTrip      : REAL := 90.0;     // trip at this reactor level, %
    rCoolingGain    : REAL := 6.0;      // % valve opening per C above setpoint
    rCooling        : REAL;
END_VAR

// ---- Safety trip ----------------------------------------------------
IF GVL_Sim.TT201_Value > rTempTrip OR GVL_Sim.LT201_Value > rLevelTrip THEN
    bTrip := TRUE;
END_IF
IF bReset AND GVL_Sim.TT201_Value < rTempSP + 10.0 AND GVL_Sim.LT201_Value < rLevelMax THEN
    bTrip := FALSE;
END_IF

// ---- Feed tank filling (hysteresis) ---------------------------------
IF GVL_Sim.LT101_Value < 70.0 THEN bFill1 := TRUE; ELSIF GVL_Sim.LT101_Value > 90.0 THEN bFill1 := FALSE; END_IF
IF GVL_Sim.LT102_Value < 70.0 THEN bFill2 := TRUE; ELSIF GVL_Sim.LT102_Value > 90.0 THEN bFill2 := FALSE; END_IF
IF GVL_Sim.LT103_Value < 70.0 THEN bFill3 := TRUE; ELSIF GVL_Sim.LT103_Value > 90.0 THEN bFill3 := FALSE; END_IF

GVL_Sim.XV101_OpenCmd := bRun AND NOT bTrip AND bFill1;
GVL_Sim.XV102_OpenCmd := bRun AND NOT bTrip AND bFill2;
GVL_Sim.XV103_OpenCmd := bRun AND NOT bTrip AND bFill3;

// ---- Transfer to the reactor (hysteresis on reactor level) ----------
IF NOT bRun OR bTrip OR GVL_Sim.LT201_Value >= rLevelMax THEN
    bTransfer := FALSE;
ELSIF GVL_Sim.LT201_Value <= rLevelMax - 10.0 THEN
    bTransfer := TRUE;
END_IF

GVL_Sim.XV111_OpenCmd := bTransfer;
GVL_Sim.XV112_OpenCmd := bTransfer;
GVL_Sim.XV113_OpenCmd := bTransfer;

GVL_Sim.P101_RunCmd := bTransfer AND GVL_Sim.XV111_OpenFb AND GVL_Sim.LT101_Value > 10.0 AND NOT GVL_Sim.P101_Fault;
GVL_Sim.P102_RunCmd := bTransfer AND GVL_Sim.XV112_OpenFb AND GVL_Sim.LT102_Value > 10.0 AND NOT GVL_Sim.P102_Fault;
GVL_Sim.P103_RunCmd := bTransfer AND GVL_Sim.XV113_OpenFb AND GVL_Sim.LT103_Value > 10.0 AND NOT GVL_Sim.P103_Fault;
GVL_Sim.P103_SpeedSP := 40.0;       // water pump is variable speed

GVL_Sim.P101_ResetCmd := bReset;
GVL_Sim.P102_ResetCmd := bReset;
GVL_Sim.P103_ResetCmd := bReset;

// ---- Reactor cooling (proportional) and outlet -----------------------
rCooling := 20.0 + rCoolingGain * (GVL_Sim.TT201_Value - rTempSP);
GVL_Sim.XV210_PositionSP := LIMIT(0.0, rCooling, 100.0);

IF bRun AND NOT bTrip AND GVL_Sim.LT201_Value > 30.0 THEN
    GVL_Sim.XV201_PositionSP := 60.0;
ELSE
    GVL_Sim.XV201_PositionSP := 0.0;
END_IF
