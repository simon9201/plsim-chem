# Modules/Vessel/Reactor/cstr/cstr.py
import numpy as np
from Modules.Entity.Process_equipment.Vessel.Reactor.Reactor import Reactor

class ContinuousStirredTankReactor(Reactor):
    """En kontinuerlig omrörningsreaktor (CSTR) med exoterm dynamik."""
    def __init__(self, name, max_capacity, initial_volume, initial_temp=25.0):
        super().__init__(name, max_capacity, initial_volume, initial_temp)
        
    def update(self, dt, flow_in, flow_out, feed_conc_A, feed_temp, cooling_valve):
        # 1. Uppdatera totalmassa/volym via Vessel-motorn
        self.update_mass_balance(dt, flow_in, flow_out)
        
        # 2. Räkna ut CSTR-specifik reaktionshastighet (Arrhenius kinetik)
        # Ju varmare det är, desto snabbare reagerar ämne A och blir ämne B (Exoterm risk!)
        if self.volume > 0:
            k = 0.1 * np.exp(0.05 * (self.temperature - 20.0)) # Kinetisk konstant
            reaction_rate = k * self.concentration_A
            
            # Massbalans för komponent A: In - Ut - Reagerat
            dC_A_dt = (flow_in / self.volume) * (feed_conc_A - self.concentration_A) - reaction_rate
            self.concentration_A += dC_A_dt * dt
            
            # Energibalans (Temperaturförändring): Inströmning + Reaktionsvärme - Kylning
            # Kylningen beror linjärt på hur mycket PLC:n öppnar kylventilen (0.0 till 1.0)
            heat_generation = 50.0 * reaction_rate  # Exoterm värme
            heat_removal = 15.0 * cooling_valve * (self.temperature - 15.0) # 15°C kylvatten
            
            dT_dt = (flow_in / self.volume) * (feed_temp - self.temperature) + heat_generation - heat_removal
            self.temperature += dT_dt * dt