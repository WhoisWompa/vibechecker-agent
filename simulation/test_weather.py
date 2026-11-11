"""Self-check for the weather layer. Run: python -m simulation.test_weather"""

import datetime

import pandas as pd

from .env import Cafe
from .settings import env_Settings
from . import weather as weather_mod


def settings(n, temperature=24):
    return pd.DataFrame({
        "light": [400] * n,
        "temperature": [temperature] * n,
        "volume": [55] * n,
        "fan": [0] * n,
    })


# (a) weather() never returns rows with update_timestamp beyond current sim time.
cafe = Cafe(seed=1, lam=0.0)
cafe.set_schedule(settings(300))
for _ in range(300):
    cafe.tick(1)
    w = cafe.weather()
    wall_clock = pd.Timestamp(
        datetime.datetime(cafe.date.year, cafe.date.month, cafe.date.day, 8, 0)
        + datetime.timedelta(minutes=cafe.t)
    ).tz_localize(weather_mod.SGT)
    assert (w["forecast_24h"]["update_timestamp"] <= wall_clock).all()
    assert (w["forecast_2h"]["update_timestamp"] <= wall_clock).all()

# (b) outside_temperature stays within [20, 36]; varies across a rainy vs fair day.
# 2024-01-20 (day 20) is a heavy-rain day in Bishan; 2024-02-05 (day 36) is fair.
# (Kept close to day 20 -- weather() is called every simulated minute and the
# per-minute forecast lookup is expensive, so each simulated day costs
# several seconds; a far-off fair day like day 179 makes this check take
# 10+ minutes.)
def mean_outside_temp(day_index):
    cafe = Cafe(seed=2, lam=0.0)
    for _ in range(day_index - 1):
        cafe.set_schedule(settings(cafe.n))
        cafe.tick(cafe.n)
        cafe.next_day()
    cafe.set_schedule(settings(cafe.n))
    cafe.tick(cafe.n)
    df = cafe.day_df()
    vals = df["outside_temperature"].dropna()
    assert (vals >= 20).all() and (vals <= 36).all()
    return vals.mean(), cafe.date

rainy_mean, rainy_date = mean_outside_temp(20)
fair_mean, fair_date = mean_outside_temp(36)
assert rainy_date == datetime.date(2024, 1, 20)
assert fair_date == datetime.date(2024, 2, 5)
assert abs(rainy_mean - fair_mean) > 0.5, "rainy vs fair days should read differently"

# (c) day 1 maps to 2024-01-01 and weekday multipliers apply.
cafe = Cafe(seed=3, lam=None)
assert cafe.date == datetime.date(2024, 1, 1)
assert cafe.weekday == "Monday"

# Weekday typical level (before wobble) should differ Mon vs Sat, and the
# per-day multiplier actually used should reflect that level (wobble is only
# +/-10%, so a lower Monday draw should rarely beat a higher Saturday draw
# across several seeds).
mon_mults = []
sat_mults = []
for seed in range(20, 30):
    cafe = Cafe(seed=seed, lam=None)
    cafe.set_start_date("2024-01-08")  # a plain Monday, no holiday/eve nearby
    mon_mults.append(cafe._weekday_mult)
    for _ in range(5):
        cafe.set_schedule(settings(cafe.n))
        cafe.tick(cafe.n)
        cafe.next_day()
    assert cafe.weekday == "Saturday"
    sat_mults.append(cafe._weekday_mult)
assert sum(sat_mults) / len(sat_mults) > sum(mon_mults) / len(mon_mults), (
    "Saturday's typical level should scale arrivals up more than Monday's"
)

# The weekend arrival curve shape actually differs from the weekday shape
# (brunch bump replaces lunch bump, dinner bump shrinks).
from .env import base
assert base(240, weekend=True) > base(240, weekend=False)  # brunch peak, noon
assert base(660, weekend=True) < base(660, weekend=False)  # smaller dinner bump

# (d) same-weekday bank pays a random 0-80% draw on day n+7, sign preserved.
# Force a known balance into the weekday bank (single block, n=60, so its
# payout bell can't bleed into a neighbouring block) and confirm a partial
# payout fires exactly 7 days later, drawn from the balance at fire time.
# Freeze the deposit at zero (severity's z with an empty hour would otherwise
# add/subtract each day) by pinning z == empty_score, so the bank only moves
# via the forced starting balance and its single 0-80% draw on day n+7.
cafe = Cafe(n=60, seed=5, lam=0.0)
cafe.z = cafe.empty_score
deposit = 10.0
cafe.weekday_banks[0] = deposit
cafe.weekday_fires[0] = [cafe.day + 7]
fire_day_target = cafe.day + 7
for _ in range(7):
    cafe.set_schedule(settings(cafe.n))
    cafe.tick(cafe.n)
    before = cafe.day
    bal_before_draw = cafe.weekday_banks[0]
    cafe.next_day()
    if before + 1 == fire_day_target:
        pay = bal_before_draw - cafe.weekday_banks[0]  # amount drawn out
        assert 0 < pay <= 0.8 * deposit + 1e-6, (
            "payout should land on day n+7, magnitude between 0 and 80% of the deposit"
        )
        assert max(cafe._weekday_payouts) > 0, "payout bell should land on day n+7"

# Negative balance draws a negative payout, sign preserved.
cafe = Cafe(n=60, seed=7, lam=0.0)
cafe.z = cafe.empty_score
deposit = -10.0
cafe.weekday_banks[0] = deposit
cafe.weekday_fires[0] = [cafe.day + 7]
fire_day_target = cafe.day + 7
for _ in range(7):
    cafe.set_schedule(settings(cafe.n))
    cafe.tick(cafe.n)
    before = cafe.day
    bal_before_draw = cafe.weekday_banks[0]
    cafe.next_day()
    if before + 1 == fire_day_target:
        pay = bal_before_draw - cafe.weekday_banks[0]  # amount drawn out
        assert 0.8 * deposit - 1e-6 <= pay < 0, (
            "negative balance should draw a negative payout on day n+7"
        )
        assert min(cafe._weekday_payouts) < 0, "negative payout bell should land on day n+7"

# latest_forecast_24h(): at 8am returns the early-morning issue; after playing
# past a midday issue's update_timestamp it returns that newer one.
cafe = Cafe(seed=6, lam=0.0)
early = cafe.latest_forecast_24h()
assert len(early) == 1
early_ts = early.iloc[0]["update_timestamp"]

cafe.set_schedule(settings(300))
cafe.tick(300)  # 8am + 300min = 1pm, past the ~12:04pm issue
later = cafe.latest_forecast_24h()
assert len(later) == 1
later_ts = later.iloc[0]["update_timestamp"]
assert later_ts > early_ts, "later in the day should surface a newer issue"
assert later_ts <= pd.Timestamp(
    datetime.datetime(cafe.date.year, cafe.date.month, cafe.date.day, 8, 0)
    + datetime.timedelta(minutes=cafe.t)
).tz_localize(weather_mod.SGT), "no seeing the future"

# latest_forecast_24h() on day 1: when today's first issue lands after the
# 8am open, the previous calendar date's last issue is returned instead of
# an empty frame. 2024-01-21's first issue is at 09:17; 2024-01-20's last
# issue is at 23:30.
cafe = Cafe(seed=1, lam=0.0)
cafe.set_start_date("2024-01-21")
assert len(cafe.weather()["forecast_24h"]) == 0, "test setup: no issue visible at open"
fb = cafe.latest_forecast_24h()
assert len(fb) == 1, "day 1 should fall back to the previous date's last issue"
assert fb.iloc[0]["update_timestamp"].date() == datetime.date(2024, 1, 20)
assert "rain_chance" in fb.columns
# The 2h lookup gets the same fallback: minute 0 carries a real forecast text.
assert cafe._central_forecast_lookup[0][0] != ""

# 2024-01-01 has no previous date in the data: still works (own 05:34 issue).
cafe = Cafe(seed=1, lam=0.0)
cafe.set_start_date("2024-01-01")
assert len(cafe.latest_forecast_24h()) == 1

# 2024-01-09's first issue is at 17:20 and 2024-01-08 has no issues at all:
# the fallback finds nothing and the empty today frame comes back, no error.
cafe = Cafe(seed=1, lam=0.0)
cafe.set_start_date("2024-01-09")
assert len(cafe.latest_forecast_24h()) == 0

print("ok")
