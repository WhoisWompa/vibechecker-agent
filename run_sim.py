"""Manual testing harness: run the cafe for a number of days and inspect output."""

from simulation.env import Cafe
from simulation.settings import env_Settings


def run_simulation(days=3, settings=None, seed=0, **cafe_knobs):
    """Run the cafe for `days` days, printing each day's summary as it goes.

    settings: either an env_Settings used every minute, or a function
              (t, day) -> env_Settings for per-minute control. None = a
              plain fixed guess (24 degC, 400 lux, volume 50, fan 0).
    cafe_knobs: passed straight to Cafe (e.g. severity=1.0, capacity=20).

    Returns (cafe, last_day_log) where last_day_log is the list of Sensors
    for the final day played (each earlier day's log is discarded once its
    summary is printed).
    """
    if settings is None:
        settings = env_Settings(temperature=24, light=400, volume=50, fan=0)
    pick = settings if callable(settings) else (lambda t, day: settings)
    cafe = Cafe(seed=seed, **cafe_knobs)
    log = []
    for day in range(days):
        log = [cafe.step(pick(t, day)) for t in range(cafe.n)]
        entered = sum(s.entered for s in log)
        peak = max(s.occupancy for s in log)
        print(f"day {day + 1}: entered {entered}, peak occupancy {peak}, "
              f"bank avg {sum(cafe.banks) / len(cafe.banks):+.2f}")
        cafe.next_day()
    return cafe, log


if __name__ == "__main__":
    cafe, last_day_log = run_simulation(days=3)
