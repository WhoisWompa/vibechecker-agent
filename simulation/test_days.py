"""Self-check for fan physics and the multi-day h loop. Run: python -m simulation.test_days"""

from .env import Cafe
from .hidden import crowd_comfort
from .settings import env_Settings

# crowd_comfort: 1 below the knee, 0 at full, decreasing above the knee.
assert crowd_comfort(0, 30, 0.6) == 1.0
assert crowd_comfort(18, 30, 0.6) == 1.0
assert crowd_comfort(30, 30, 0.6) == 0.0
assert 0 < crowd_comfort(27, 30, 0.6) < crowd_comfort(21, 30, 0.6) < 1


def settings(fan):
    return env_Settings(light=400, temperature=24, volume=55, fan=fan)


# Fan flushes CO2 faster: run a busy room to steady state, then empty it and
# watch the excess decay with fan off vs full blast.
def co2_after(fan, minutes=15):
    cafe = Cafe(seed=1, lam=0.3)
    for _ in range(200):
        cafe.step(settings(0))
    cafe.lam = 0.0
    cafe.parties, cafe.takeaway = [], []
    start = cafe.state["co2"]
    for _ in range(minutes):
        cafe.step(settings(fan))
    return start, cafe.state["co2"]

s0, off = co2_after(0)
s1, on = co2_after(5)
assert s0 > 500  # crowd actually pushed CO2 up
assert on < off, "full fan should clear CO2 faster than leak alone"

# Fan drags temperature toward outside (now weather-driven, not a flat
# constant) against a 24 degC setpoint. Pin a setpoint below the day's warm
# outside temperature so the fan's pull has a clear, testable direction.
def temp_at(fan):
    cafe = Cafe(seed=2, lam=0.0)
    for _ in range(300):
        cafe.step(settings(fan))
    return cafe.state["temperature"], cafe.weather_state.outside_temp

t_off, outside = temp_at(0)
t_on, outside2 = temp_at(5)
assert outside > 24 and outside2 > 24, "test setup needs a warm outside reading"
assert t_on > t_off, "full fan should pull the room toward outside"
assert abs(t_on - 24) < 2.5, "thermostat should dominate even at full fan"

# Multi-day loop: banks move, stay clamped, and the multiplier stays in range.
cafe = Cafe(n=120, seed=3, lam=0.2)
assert cafe.blocks == 2 and cafe.banks == [0.0, 0.0]
assert 0.45 * 30 <= cafe.crowd_ideal <= 0.55 * 30
for day in range(5):
    for _ in range(120):
        cafe.step(settings(0))
    scores = cafe._block_scores()
    assert all(s is None or 0 <= s <= 1 for s in scores)
    cafe.next_day()
assert cafe.day == 5
assert all(abs(b) <= cafe.bank_cap for b in cafe.banks)
assert any(b != 0 for b in cafe.banks), "5 busy days should move some bank"
assert all(1 - cafe.kappa <= m <= 1 + cafe.kappa for m in cafe.lam_mult)

# Dwell target tracks p_min: perfect 60-min stay = full grade at default p_min.
cafe = Cafe(n=60, seed=4, lam=0.0)
cafe.stays = [(0, 60)]
cafe.seated_by_min = [round(cafe.crowd_ideal)] * 60
(score,) = cafe._block_scores()
assert score is not None and score > 0.95

# reset() starts a fresh run.
cafe.reset()
assert cafe.day == 0 and all(b == 0 for b in cafe.banks) and cafe.t == 0

# Severity knob: 0.5 reproduces the settled flip point; extremes map correctly.
cafe = Cafe(n=60, seed=5)
assert abs(cafe.z - 0.35) < 1e-9 and abs(cafe.mult_floor - 0.25) < 1e-9
soft, hard = Cafe(n=60, seed=5, severity=0.0), Cafe(n=60, seed=5, severity=1.0)
assert abs(soft.z - 0.20) < 1e-9 and abs(soft.mult_floor - 0.50) < 1e-9
assert abs(soft.empty_score - soft.z) < 1e-9  # forgiving: emptiness is free
assert abs(hard.z - 0.45) < 1e-9 and abs(hard.mult_floor - 0.05) < 1e-9
# Empty blocks now take a deposit (severity > 0), and the multiplier floor holds.
cafe = Cafe(n=120, seed=6, lam=0.0)  # nobody ever arrives
for _ in range(120):
    cafe.step(settings(0))
cafe.next_day()
assert all(b < 0 for b in cafe.banks), "empty blocks should bleed reputation"
assert all(m >= cafe.mult_floor for m in cafe.lam_mult)

print("ok")
