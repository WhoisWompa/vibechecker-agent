"""Self-check for f, closeness, hazard. Run: python -m simulation.test_hidden"""

import random

from .hidden import HiddenParams, f, closeness, hazard

rng = random.Random(0)
p = HiddenParams.roll(rng)

# Every rolled parameter lands in its documented range.
assert 23.5 <= p.base_temp <= 26 and 1 <= p.midday_temp_dip <= 4
assert 450 <= p.base_light <= 800 and 250 <= p.midday_light_dip <= 600
assert 210 <= p.lunch_centre <= 300 and 660 <= p.evening_centre <= 780
assert 600 <= p.co2_comfort <= 800 and 75 <= p.humidity_comfort <= 80

# Midday (t=307 is solar noon's light valley) is dimmer and cooler than 8am.
open_, noon = f(0, p), f(307, p)
assert noon["light"] < open_["light"]
assert f(367, p)["temperature"] < f(0, p)["temperature"]  # temp valley lags 60 min
# Temperature valley sits after the light valley (heat lag) — checked on the
# sun-arc component alone, since a big meal dip can move the global minimum.
import dataclasses
p0 = dataclasses.replace(p, lunch_temp_dip=0, evening_temp_dip=0)
temps = [f(t, p0)["temperature"] for t in range(960)]
lights = [f(t, p0)["light"] for t in range(960)]
assert temps.index(min(temps)) == lights.index(min(lights)) + 60
# Volume peaks near the lunch or evening bump, not at baseline.
vols = [f(t, p)["volume"] for t in range(960)]
assert max(vols) > p.base_volume + 4

# co2/humidity comfort points are flat all day.
assert f(0, p)["co2"] == f(400, p)["co2"] == p.co2_comfort
assert f(0, p)["humidity"] == f(400, p)["humidity"] == p.humidity_comfort

# Closeness: 1 at the ideal, strictly smaller off it, weights matter.
ideal = f(300, p)
w = {"temperature": 1.0, "light": 0.001, "volume": 0.01}
assert closeness(ideal, ideal, w) == 1.0
off = dict(ideal, temperature=ideal["temperature"] + 2)
assert 0 < closeness(off, ideal, w) < 1
assert closeness(off, ideal, w) < closeness(
    dict(ideal, temperature=ideal["temperature"] + 1), ideal, w
)

# One-sided dims: below the comfort point is neutral, above is penalized.
w2 = {"co2": 1 / 500**2}
ideal2 = {"co2": 700.0}
assert closeness({"co2": 420.0}, ideal2, w2) == 1.0
assert closeness({"co2": 700.0}, ideal2, w2) == 1.0
assert closeness({"co2": 1200.0}, ideal2, w2) < 1.0
assert closeness({"co2": 1700.0}, ideal2, w2) < closeness({"co2": 1200.0}, ideal2, w2)

# Hazard interpolates the bounds.
assert hazard(1.0, 1 / 60, 1 / 8) == 1 / 60
assert hazard(0.0, 1 / 60, 1 / 8) == 1 / 8
assert 1 / 60 < hazard(0.5, 1 / 60, 1 / 8) < 1 / 8

# roll() with no ranges is the same as roll() with ranges=None, field by field.
a = HiddenParams.roll(random.Random(0))
b = HiddenParams.roll(random.Random(0), None)
assert all(getattr(a, fld.name) == getattr(b, fld.name) for fld in dataclasses.fields(a))

# A partial override narrows only the named field; every other field keeps
# the unranged value for the same seed (same draw order, same draw count).
c = HiddenParams.roll(random.Random(0), {"base_temp": (24, 25.5)})
assert 24 <= c.base_temp <= 25.5
for fld in dataclasses.fields(c):
    if fld.name != "base_temp":
        assert getattr(c, fld.name) == getattr(a, fld.name), fld.name

# The demo preset's hidden_ranges are all respected on seeds 1..50.
from .settings import PRESETS
demo_ranges = PRESETS["demo"]["hidden_ranges"]
for seed in range(1, 51):
    q = HiddenParams.roll(random.Random(seed), demo_ranges)
    for name, (lo, hi) in demo_ranges.items():
        assert lo <= getattr(q, name) <= hi, (seed, name, getattr(q, name))

print("ok")
