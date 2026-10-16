import math

# =====================================================================
# Environment: the outdoor air temperature, with a daily and a seasonal cycle.
#
#   T = mean + annual_amp * seasonal + daily_amp * diurnal + offset
#
# Coldest around 04:00 and warmest around 16:00; coldest day mid-January.
# `offset` is for weather events (cold snap = -15, heat wave = +10).
# Time is the plant's slow clock (PlantModel.slow_time), so the cycles can be
# sped up with PlantModel.slow_time_scale without changing the hydraulics.
# The defaults give a constant 20 C.
# =====================================================================


class Environment:
    def __init__(self, mean_c=20.0, daily_amp=0.0, annual_amp=0.0, day_length=86400.0,
                 start_day=180.0, start_hour=8.0):
        self.mean_c = mean_c
        self.daily_amp = daily_amp          # C, half the day/night swing
        self.annual_amp = annual_amp        # C, half the summer/winter swing
        self.day_length = day_length        # slow-clock seconds per day
        self.start_day = start_day          # day of year at t = 0 (0 = 1 Jan)
        self.start_hour = start_hour
        self.offset = 0.0

    def temperature(self, t):
        days = t / self.day_length
        hour = (self.start_hour + 24.0 * days) % 24.0
        day_of_year = self.start_day + days + self.start_hour / 24.0
        seasonal = -math.cos(2.0 * math.pi * (day_of_year - 15.0) / 365.0)
        diurnal = math.sin(2.0 * math.pi * (hour - 10.0) / 24.0)
        return self.mean_c + self.annual_amp * seasonal + self.daily_amp * diurnal + self.offset

    def get_state(self):
        return {
            "mean_c": float(self.mean_c),
            "daily_amp": float(self.daily_amp),
            "annual_amp": float(self.annual_amp),
            "day_length": float(self.day_length),
            "start_day": float(self.start_day),
            "start_hour": float(self.start_hour),
            "offset": float(self.offset),
        }

    def set_state(self, state):
        self.mean_c = state.get("mean_c", self.mean_c)
        self.daily_amp = state.get("daily_amp", self.daily_amp)
        self.annual_amp = state.get("annual_amp", self.annual_amp)
        self.day_length = state.get("day_length", self.day_length)
        self.start_day = state.get("start_day", self.start_day)
        self.start_hour = state.get("start_hour", self.start_hour)
        self.offset = state.get("offset", 0.0)
