"""The two vectors that cross the f/g boundary."""

from dataclasses import dataclass


@dataclass
class env_Settings:
    light: float  # lux
    temperature: float  # degC setpoint
    volume: float  # dB
    fan: int  # ventilation speed, 0 (off) to 5 (full blast)


GROUP_SIZES = {1: 45, 2: 38, 3: 10, 4: 5, 5: 2}

# Weather-shifted comfort (§10): ideal_temp += WEATHER_ALPHA * (WEATHER_TYPICAL_OUTSIDE - outside_temp_now),
# clamped to +/- WEATHER_NUDGE_CAP.
WEATHER_ALPHA = 0.3
WEATHER_TYPICAL_OUTSIDE = 29.5
WEATHER_NUDGE_CAP = 2.0

# Day-of-week arrival level, scales the whole base(t) curve. Tunable.
WEEKDAY_LEVEL = {
    "Monday": 0.7,
    "Tuesday": 0.75,
    "Wednesday": 0.8,
    "Thursday": 0.85,
    "Friday": 1.1,
    "Saturday": 1.25,
    "Sunday": 1.15,
}
WEEKDAY_WOBBLE_RANGE = (0.9, 1.1)  # one draw per day

# Global arrival scale, multiplies the whole lambda(t) curve (composes with
# weekday level, wobble, and the reputation multiplier). Tunable.
ARRIVAL_SCALE = 0.35

# Singapore 2024 public holidays: crowd level is replaced by HOLIDAY_LEVEL and
# the day uses the weekend rhythm (brunch bump instead of lunch), regardless
# of the actual weekday. The usual +/-10% wobble still applies on top.
PUBLIC_HOLIDAYS_2024 = {
    "2024-01-01", "2024-02-10", "2024-02-11", "2024-02-12", "2024-03-29",
    "2024-04-10", "2024-05-01", "2024-05-22", "2024-06-17", "2024-08-09",
    "2024-10-31", "2024-12-25",
}
HOLIDAY_LEVEL = 1.4

# Eve days: crowd level is replaced by EVE_LEVEL but the day keeps its normal
# weekday rhythm (no brunch swap).
EVE_DAYS_2024 = {"2024-12-24", "2024-12-31"}
EVE_LEVEL = 1.15

# Named presets for Cafe(preset=...). "demo": a room that can fill, for showing
# learning on camera. Wider comfort tolerances (2x), a gentler worst-case leave
# rate, more arrivals, no reputation punishment. The demo room's answer key
# stays inside what the levers can reach: temperature 21 to 26 C after the
# weather nudge, light above the daylight, music at most 70 dB. The fan's
# warming is halved. Explicit Cafe(...) arguments still win over a preset.
PRESETS = {
    "demo": {
        "weights": {
            "temperature": 1 / 3.0**2,
            "light": 1 / 200.0**2,
            "volume": 1 / 16.0**2,
            "co2": 1 / 1000.0**2,
            "humidity": 1 / 16.0**2,
        },
        "p_max": 1 / 15,
        "severity": 0.0,
        "arrival_scale": 0.6,
        "hidden_ranges": {
            "base_temp": (24.0, 25.5),
            "midday_temp_dip": (0.5, 1.5),
            "base_light": (500.0, 800.0),
            "midday_light_dip": (100.0, 300.0),
            "base_volume": (56.0, 64.0),
            "lunch_volume_lift": (3.0, 8.0),
            "evening_volume_lift": (4.0, 10.0),
        },
        "weather_nudge_cap": 1.0,
        "fan_temp_factor": 0.05,
    },
}
