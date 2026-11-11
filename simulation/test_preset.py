"""Self-check for preset knobs (hidden_ranges, weather_nudge_cap, fan_temp_factor).
Run: python -m simulation.test_preset"""

from .env import Cafe, FAN_TEMP_FACTOR
from .hidden import f
from .settings import PRESETS, WEATHER_ALPHA, WEATHER_TYPICAL_OUTSIDE, WEATHER_NUDGE_CAP

# Preset values land on the cafe.
demo = Cafe(seed=1, preset="demo")
assert demo.weather_nudge_cap == 1.0
assert demo.fan_temp_factor == 0.05
assert demo.hidden_ranges == PRESETS["demo"]["hidden_ranges"]

# Explicit argument wins over the preset.
assert Cafe(seed=1, preset="demo", fan_temp_factor=0.2).fan_temp_factor == 0.2
assert Cafe(seed=1, preset="demo", weather_nudge_cap=2.5).weather_nudge_cap == 2.5

# No preset: today's constants, no hidden ranges.
plain = Cafe(seed=1)
assert plain.weather_nudge_cap == WEATHER_NUDGE_CAP
assert plain.fan_temp_factor == FAN_TEMP_FACTOR
assert plain.hidden_ranges is None

# Demo answer key, plus the weather nudge as step() computes it, stays
# between 21 and 26 degC for at least 95% of minutes, seeds 1..12, start
# 2024-04-05. The weather state is stepped by hand; the day is not played.
for seed in range(1, 13):
    cafe = Cafe(seed=seed, preset="demo")
    cafe.set_start_date("2024-04-05")
    inside = 0
    for t in range(cafe.n):
        cafe.weather_state.step(t)
        nudge = WEATHER_ALPHA * (WEATHER_TYPICAL_OUTSIDE - cafe.weather_state.outside_temp)
        nudge = max(-cafe.weather_nudge_cap, min(cafe.weather_nudge_cap, nudge))
        ideal_temp = f(t, cafe.hidden)["temperature"] + nudge
        inside += 21 <= ideal_temp <= 26
    assert inside >= 0.95 * cafe.n, (seed, inside / cafe.n)

print("ok")
