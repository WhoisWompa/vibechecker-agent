"""Self-check for Cafe's day_df output format. Run: python -m simulation.test_run_df"""

import pandas as pd

from .env import Cafe

SENSOR_COLS = {"temperature_sensor", "humidity", "co2", "noise", "light_sensor",
               "entered", "left", "occupancy", "outside_temperature",
               "central_forecast_text", "central_forecast_code"}
SETTING_COLS = {"light_set", "temperature_set", "volume", "fan"}


def run_day(cafe: Cafe, settings: pd.DataFrame) -> pd.DataFrame:
    cafe.reset()
    cafe.set_schedule(settings)
    cafe.tick(len(settings))
    return cafe.day_df()


cafe = Cafe(n=60, seed=1, lam=0.3)
settings = pd.DataFrame({
    "light": [400] * cafe.n,
    "temperature": [24] * cafe.n,
    "volume": [55] * cafe.n,
    "fan": [2] * cafe.n,
})
out = run_day(cafe, settings)

# Shape and columns.
assert len(out) == cafe.n
assert set(out.columns) == SETTING_COLS | SENSOR_COLS
assert "t" not in out.columns
assert "light" not in out.columns and "temperature" not in out.columns

# Row 0's sensor columns are NA; row 1's are not.
for col in SENSOR_COLS:
    assert pd.isna(out.loc[0, col]), f"row 0 {col} should be NA"
    assert not pd.isna(out.loc[1, col]), f"row 1 {col} should not be NA"

# Settings round-trip exactly, row for row.
cafe = Cafe(n=60, seed=2, lam=0.0)
temps = [24 + 0.001 * t for t in range(cafe.n)]
settings = pd.DataFrame({
    "light": [400] * cafe.n,
    "temperature": temps,
    "volume": [50] * cafe.n,
    "fan": [0] * cafe.n,
})
out = run_day(cafe, settings)
assert list(out["temperature_set"]) == temps

# Plausible physical ranges on a non-NA row.
cafe = Cafe(n=60, seed=3, lam=0.3)
settings = pd.DataFrame({
    "light": [400] * cafe.n,
    "temperature": [24] * cafe.n,
    "volume": [55] * cafe.n,
    "fan": [2] * cafe.n,
})
out = run_day(cafe, settings)
row = out.loc[cafe.n - 1]
assert 0 <= row["temperature_sensor"] <= 70
assert 0 <= row["co2"] <= 2000
assert 0 <= row["humidity"] <= 100
assert 30 <= row["noise"] <= 130
assert 0 <= row["light_sensor"] <= 1500

print("ok")
