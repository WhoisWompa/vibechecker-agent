"""The cafe simulator: step(settings) -> sensors; next_day() rolls the h loop."""

from dataclasses import dataclass
import datetime
import math
import random

import pandas as pd

from .settings import (
    env_Settings, GROUP_SIZES, WEATHER_ALPHA, WEATHER_TYPICAL_OUTSIDE,
    WEATHER_NUDGE_CAP, WEEKDAY_LEVEL, WEEKDAY_WOBBLE_RANGE, ARRIVAL_SCALE,
    PUBLIC_HOLIDAYS_2024, HOLIDAY_LEVEL, EVE_DAYS_2024, EVE_LEVEL, PRESETS,
)
from .hidden import HiddenParams, bump, f, closeness, hazard, S
from . import weather as weather_mod


@dataclass
class Sensors:
    """What the agent observes at the end of a timestep."""

    t: int
    temperature: float
    humidity: float
    co2: float
    noise: float
    light: float
    entered: int
    left: int
    occupancy: int


@dataclass
class Physics:
    """Lumped first-order room model, one entry per sensed quantity.

    next = current + (dt / tau) * (target - current) + k * occupancy
    """

    tau: float  # timesteps to relax toward target
    k: float  # push per occupant per timestep
    outside: float  # ambient value the room sits at with no occupants
    sigma: float = 0.0  # sensor noise


DEFAULT_PHYSICS = {
    "temperature": Physics(tau=25, k=0.002, outside=30.0, sigma=0.1),
    "humidity": Physics(tau=25, k=0.01, outside=75.0, sigma=0.5),
    "co2": Physics(tau=15, k=3.0, outside=420.0, sigma=5.0),
    "noise": Physics(tau=1, k=0.0, outside=35.0, sigma=1.0),  # tau/k unused: dB combined directly in step
    "light": Physics(tau=1, k=0.0, outside=200.0, sigma=5.0),
}

PERSON_DB = 58.0  # sound level of one talking person

FAN_TEMP_FACTOR = 0.1  # fan tug on temperature; full blast settles ~+1.5 degC over setpoint


def _lerp3(s: float, a: float, b: float, c: float) -> float:
    """Piecewise-linear through anchors at s = 0, 0.5, 1."""
    return a + (b - a) * s * 2 if s <= 0.5 else b + (c - b) * (s - 0.5) * 2


def base(t: float, weekend: bool = False) -> float:
    """Default arrival curve, parties per minute; t in minutes since 8am.

    Weekends swap the lunch bump for a brunch bump (centred noon, ~11am-1pm)
    and shrink the dinner bump; weekdays keep the original shape.
    """
    if weekend:
        return (
            0.15
            + 0.30 * bump(t, 30, 40)  # breakfast, 8:30am
            + 0.75 * bump(t, 240, 60)  # brunch, noon
            + 0.40 * bump(t, 660, 75)  # dinner, 7pm (shrunk)
        )
    return (
        0.15
        + 0.30 * bump(t, 30, 40)  # breakfast, 8:30am
        + 0.75 * bump(t, 270, 60)  # lunch, 12:30pm
        + 0.60 * bump(t, 660, 75)  # dinner, 7pm
    )

# weight = 1 / tolerance², tolerances: 1.5 °C, 100 lux, 8 dB, 500 ppm, 8 %RH;
# co2/humidity are one-sided — see hidden.ONE_SIDED
DEFAULT_WEIGHTS = {
    "temperature": 1 / 1.5**2,
    "light": 1 / 100**2,
    "volume": 1 / 8**2,
    "co2": 1 / 500**2,
    "humidity": 1 / 8**2,
}


class Cafe:
    # Tuning knobs, what they do, and sensible ranges:
    #   n           length of a day in minutes. 840 = 8am-10pm. Range 480-960.
    #   capacity    seats (k). Every crowd quantity is a fraction of this, so
    #               changing it rescales the whole crowd game. Range 10-100.
    #   p_min       leave chance/min in a perfect room; mean stay = 1/p_min.
    #               Lower = happier ceiling. Range 1/90 - 1/30.
    #   p_max       leave chance/min in a terrible (or packed) room; mean stay
    #               = 1/p_max. Higher = harsher floor. Range 1/10 - 1/4.
    #   weights     per-condition weight = 1/tolerance^2. Widen a tolerance to
    #               make that condition forgiving. Tolerances: temperature
    #               1-2 degC, light 50-150 lux, volume 5-12 dB, co2 ~500 ppm,
    #               humidity ~8 %RH. co2 and humidity are one-sided: only air
    #               worse (higher) than the comfort point is penalized.
    #   crowd_shape exponent of the crowd grade curve. Higher = flatter near
    #               the ideal, steeper at empty/full. Range 2-8.
    #   severity    how punishing reputation is, 0-1. 0 = max forgiving (bad
    #               play barely hurts), 0.5 = balanced (ruin and recovery take
    #               about equally long), 1 = max punishing (2 bad days cost ~2
    #               weeks). Drives three internals: the deposit flip point z
    #               (0.20/0.35/0.45 at severity 0/0.5/1), the score an empty
    #               block gets (no penalty / 0.275 / 0.20), and the floor of
    #               the arrival multiplier (0.50 / 0.25 / 0.05).
    #   deposit_scale  deposit = deposit_scale * (score - z). Range 5-30.
    #   R           tanh divisor: payout ~R gives ~3/4 of the maximum arrival
    #               swing. Range 1-10.
    #   kappa       max arrival swing as a fraction of base(t). Range 0-1.
    #   bank_cap    balance clamp, +/-. Smaller = reputation fades faster.
    #               Range 5-40.
    #   draw        each morning a block spends U(0, draw * balance).
    #               Range 0.3-1.
    #   payout_width  minutes a payout spreads around its random minute.
    #               Range 5-40.
    #   leak        fraction of the air excess exiting per minute with the fan
    #               off (building leakiness). Range 0.02-0.05.
    #   fan_max     same fraction at fan speed 5. Range 0.1-0.2.
    #   lam         constant parties/min override for tests; None = base(t)
    #               daily curve. Range 0.05-0.4 if set.
    #   group_sizes party-size -> relative frequency. Skew larger to fill seats
    #               faster per arrival.
    #   physics     room inertia/ambient per sensed quantity (tau = minutes to
    #               relax toward the setpoint, k = push per occupant, outside =
    #               ambient level, sigma = sensor noise).
    #   hidden      the answer key; None = roll fresh from the seeded rng.
    #   seed        rng seed; fix it to replay an identical run.
    #   arrival_scale  multiplies the whole arrival curve; None = settings.ARRIVAL_SCALE.
    #   preset      name in settings.PRESETS ("demo"); fills weights, p_max,
    #               severity, arrival_scale, hidden_ranges, weather_nudge_cap
    #               and fan_temp_factor unless given explicitly.
    #   hidden_ranges  {name: (lo, hi)} narrowing the roll of named answer-key
    #               parameters; None = HiddenParams' default ranges. Ignored
    #               when `hidden` is given.
    #   weather_nudge_cap  clamp, +/- degC, on the weather shift of the ideal
    #               temperature; None = settings.WEATHER_NUDGE_CAP. Range 0-3.
    #   fan_temp_factor  fan tug on temperature per unit of extra air rate;
    #               None = FAN_TEMP_FACTOR. Range 0-0.2.
    def __init__(
        self,
        n: int = 840,
        hidden: HiddenParams | None = None,
        weights: dict[str, float] | None = None,
        p_min: float = 1 / 60,
        p_max: float | None = None,
        lam: float | None = None,
        capacity: int = 30,
        group_sizes: dict[int, float] | None = None,
        physics: dict | None = None,
        seed: int | None = None,
        crowd_shape: float = 4,
        severity: float | None = None,
        deposit_scale: float = 20,
        R: float = 1,
        kappa: float = 1.0,
        bank_cap: float = 15,
        draw: float = 0.8,
        payout_width: float = 25,
        leak: float = 0.04,
        fan_max: float = 0.17,
        arrival_scale: float | None = None,
        preset: str | None = None,
        bookings: list[tuple[int, int]] | None = None,
        hidden_ranges: dict[str, tuple[float, float]] | None = None,
        weather_nudge_cap: float | None = None,
        fan_temp_factor: float | None = None,
    ):
        chosen = PRESETS[preset] if preset else {}
        weights = weights or chosen.get("weights")
        p_max = p_max if p_max is not None else chosen.get("p_max", 1 / 8)
        severity = severity if severity is not None else chosen.get("severity", 0.5)
        self.arrival_scale = arrival_scale if arrival_scale is not None else chosen.get("arrival_scale", ARRIVAL_SCALE)
        self.hidden_ranges = hidden_ranges if hidden_ranges is not None else chosen.get("hidden_ranges")
        self.weather_nudge_cap = (weather_nudge_cap if weather_nudge_cap is not None
                                  else chosen.get("weather_nudge_cap", WEATHER_NUDGE_CAP))
        self.fan_temp_factor = (fan_temp_factor if fan_temp_factor is not None
                                else chosen.get("fan_temp_factor", FAN_TEMP_FACTOR))
        self.preset = preset
        self.n = n  # timesteps in a day
        self.rng = random.Random(seed)
        # day_df()'s outside-temperature noise has its own generator, so reading
        # the day never changes the crowd the rest of the day produces. The
        # per-minute sensor noise in step() stays on the main stream, unchanged.
        self.reading_rng = random.Random(seed + 7919 if seed is not None else None)
        # Booked parties the owner knows about: (minute since open, party size).
        # They arrive at that minute on every day of the run, on top of the walk-ins.
        self.bookings = list(bookings or [])
        self.hidden = hidden or HiddenParams.roll(self.rng, self.hidden_ranges)
        self.weights = weights or DEFAULT_WEIGHTS
        self.p_min = p_min
        self.p_max = p_max
        self.lam = lam
        self.capacity = capacity
        self.group_sizes = group_sizes or GROUP_SIZES
        self.physics = physics or DEFAULT_PHYSICS
        self.crowd_shape = crowd_shape
        self.deposit_scale = deposit_scale
        self.R = R
        self.kappa = kappa
        self.bank_cap = bank_cap
        self.draw = draw
        self.payout_width = payout_width
        self.leak = leak
        self.fan_max = fan_max
        self.severity = severity
        self.z = _lerp3(severity, 0.20, 0.35, 0.45)
        self.empty_score = _lerp3(severity, self.z, 0.275, 0.20)
        self.mult_floor = _lerp3(severity, 0.50, 0.25, 0.05)
        self.start_date = datetime.date(2024, 1, 1)
        self.reset()

    def set_start_date(self, date) -> None:
        """Make simulated day 1 play this real calendar date (day 2 the next
        date, etc). Accepts "YYYY-MM-DD" or a datetime.date. Only allowed if
        no minutes have been played on the current day yet (t == 0, no
        readings) -- otherwise call reset() first. Re-runs day 1's setup on
        the new date when called after reset()."""
        if isinstance(date, str):
            date = datetime.datetime.strptime(date, "%Y-%m-%d").date()
        if self.t != 0 or self._readings:
            raise ValueError(
                "cannot change the start date mid-day; call reset() first"
            )
        self.start_date = date
        if self.day == 0:
            self._new_day()
            self.state["temperature"] = self.weather_state.outside_temp
            self.state["humidity"] = self.weather_state.outside_hum

    def reset(self):
        """Fresh run: banks to 0, day 0, new crowd ideal; same hidden roll."""
        self.day = 0
        self.blocks = self.n // 60
        self.banks = [0.0] * self.blocks
        self.crowd_ideal = self.rng.uniform(0.45 * self.capacity, 0.55 * self.capacity)
        self.lam_mult = [1.0] * self.n  # day 1 runs on base(t) with empty banks
        self.weather_state = weather_mod.WeatherState()
        # Same-weekday reputation echo: a persistent bank per block, mirroring
        # the rolling bank. Half-deposits land here; every day that is 7 days
        # after a deposit, a random 0-80% draw of the *current* balance pays
        # out and the rest stays banked. weekday_fires[b] is a list of fire
        # days still pending (one per deposit day, even if several land the
        # same day).
        self.weekday_banks = [0.0] * self.blocks
        self.weekday_fires: list[list[int]] = [[] for _ in range(self.blocks)]
        self._new_day()
        return self.state

    def _new_day(self):
        self.t = 0
        self.parties: list[tuple[int, int]] = []  # (size, minute they arrived)
        self.takeaway: list[tuple[int, int]] = []  # (size, timestep they leave)
        self.stays: list[tuple[int, int]] = []  # (arrival minute, stay length)
        self.seated_by_min: list[int] = []
        self._schedule: list[env_Settings] = []  # settings plan currently in force
        self._sched_start: int = 0  # minute self._schedule[0] applies to
        self._applied: list[env_Settings] = []  # settings actually used each played minute
        self._readings: list[Sensors] = []  # Sensors returned each played minute
        self._closeness: list[float] = []  # hidden closeness per played minute (eval only)
        self._outside_temps: list[float] = []  # relaxed outside temp, per played minute
        self._central_forecast_texts: list[str] = []  # in-force Bishan 2h forecast text, per played minute
        self._central_forecast_codes: list = []  # in-force Bishan 2h forecast code, per played minute

        # --- weather for today ---------------------------------------------
        self.date = self.start_date + datetime.timedelta(days=self.day)
        self.weekday = weather_mod.weekday_name(self.date)
        self._day_weather = weather_mod.get_day_weather(self.date)
        self.weather_state.start_day(self._day_weather)
        self.state = {name: p.outside for name, p in self.physics.items()}
        self.state["temperature"] = self.weather_state.outside_temp
        self.state["humidity"] = self.weather_state.outside_hum
        self._build_central_forecast_lookup()

        # --- day-of-week arrival level: typical level * wobble, applied on
        # top of the existing reputation multiplier. Public holidays replace
        # the level and borrow the weekend rhythm; eve days replace the level
        # but keep the normal weekday rhythm. ------------------------------
        date_str = self.date.isoformat()
        is_holiday = date_str in PUBLIC_HOLIDAYS_2024
        is_eve = date_str in EVE_DAYS_2024
        weekend = self.weekday in ("Saturday", "Sunday") or is_holiday
        if is_holiday:
            level = HOLIDAY_LEVEL
        elif is_eve:
            level = EVE_LEVEL
        else:
            level = WEEKDAY_LEVEL.get(self.weekday, 1.0)
        wobble = self.rng.uniform(*WEEKDAY_WOBBLE_RANGE)
        self._weekday_mult = level * wobble
        self._weekend = weekend

        # --- weekly reputation echo: on any block whose weekday cycle fires
        # today, draw a random 0-80% of that block's current bank balance and
        # pay it out at a random minute; the remainder stays banked. --------
        self._weekday_payouts = [0.0] * self.n
        for b in range(self.blocks):
            still_pending = []
            fires_today = 0
            for fire_day in self.weekday_fires[b]:
                if fire_day == self.day:
                    fires_today += 1
                else:
                    still_pending.append(fire_day)
            self.weekday_fires[b] = still_pending
            for _ in range(fires_today):
                pay = self.rng.uniform(0, 0.8) * self.weekday_banks[b]
                self.weekday_banks[b] -= pay
                minute = b * 60 + self.rng.randrange(60)
                for t in range(self.n):
                    self._weekday_payouts[t] += pay * bump(t, minute, self.payout_width)

    def _build_central_forecast_lookup(self) -> None:
        """Precompute, once per day, the in-force Bishan 2h forecast
        (text, code) for every minute 0..n-1. Walks today's forecast_2h
        (sorted by update_timestamp) once instead of re-filtering per
        minute. Minutes before today's first issue carry the previous
        calendar date's last 2h issue, on every day of the run including
        day 1; ("", None) when that date has no issues either. Same
        no-future / previous-date fallback that latest_forecast_24h() uses
        for the 24h forecast."""
        f2 = self._day_weather.forecast_2h
        # fallback for minutes before today's first issue: the previous
        # calendar date's last issue (public before today's open)
        fallback = ("", None)
        prev_date = self.date - datetime.timedelta(days=1)
        try:
            prev_f2 = weather_mod.get_day_weather(prev_date).forecast_2h
        except Exception:
            prev_f2 = None
        if prev_f2 is not None and len(prev_f2):
            row = prev_f2.iloc[-1]
            fallback = (row["forecast_text"], row["forecast_code"])

        day_open = datetime.datetime(
            self.date.year, self.date.month, self.date.day, 8, 0
        )
        issues = f2.sort_values("update_timestamp") if len(f2) else f2
        # update_timestamp is tz-aware (SGT); localize day_open the same way
        # visible_forecasts() does for wall_clock comparisons.
        day_open_ts = pd.Timestamp(day_open)
        if day_open_ts.tzinfo is None:
            day_open_ts = day_open_ts.tz_localize(weather_mod.SGT)

        lookup = [fallback] * self.n
        if len(issues) == 0:
            self._central_forecast_lookup = lookup
            return
        ts_list = issues["update_timestamp"].tolist()
        text_list = issues["forecast_text"].tolist()
        code_list = issues["forecast_code"].tolist()
        j = 0
        current = fallback
        for t in range(self.n):
            wall_clock = day_open_ts + pd.Timedelta(minutes=t)
            while j < len(ts_list) and ts_list[j] <= wall_clock:
                current = (text_list[j], code_list[j])
                j += 1
            lookup[t] = current
        self._central_forecast_lookup = lookup

    def set_schedule(self, settings: pd.DataFrame) -> None:
        """Replace the settings plan from the current minute to end of day."""
        if len(settings) == 0:
            raise ValueError("settings must have at least one row")
        self._schedule = [
            env_Settings(light=row.light, temperature=row.temperature,
                         volume=row.volume, fan=row.fan)
            for row in settings.itertuples(index=False)
        ]
        self._sched_start = self.t

    def tick(self, k: int = 1) -> None:
        """Play k minutes using the currently set schedule (holds on the last row)."""
        for _ in range(k):
            if not self._schedule:
                raise ValueError("no schedule set for this day; call set_schedule() first")
            i = self.t - self._sched_start
            v = self._schedule[i] if i < len(self._schedule) else self._schedule[-1]
            self._applied.append(v)
            reading = self.step(v)
            self._readings.append(reading)

    def day_df(self) -> pd.DataFrame:
        """Everything played so far: settings applied and the sensors they produced.

        Row t's sensor columns hold the Sensors produced by step() on row
        t-1's settings (causality shift); row 0's sensors are NA.
        """
        sensor_cols = ["temperature_sensor", "humidity", "co2", "noise",
                       "light_sensor", "entered", "left", "occupancy"]
        sensors_df = pd.DataFrame(
            {
                "temperature_sensor": [s.temperature for s in self._readings],
                "humidity": [s.humidity for s in self._readings],
                "co2": [s.co2 for s in self._readings],
                "noise": [s.noise for s in self._readings],
                "light_sensor": [s.light for s in self._readings],
                "entered": [s.entered for s in self._readings],
                "left": [s.left for s in self._readings],
                "occupancy": [s.occupancy for s in self._readings],
            }
        )
        sensors_df = sensors_df.shift(1)  # row t <- Sensors from step() on row t-1

        # outside_temperature: relaxed outside temp + noise, same lag convention.
        noisy_outside = [
            max(20.0, min(36.0, ot + self.reading_rng.gauss(0, 0.3)))
            for ot in self._outside_temps
        ]
        outside_series = pd.Series(noisy_outside).shift(1)

        # central forecast: as-issued public forecast, same causality-shift
        # convention as the other per-minute columns for consistency.
        central_text_series = pd.Series(self._central_forecast_texts).shift(1)
        central_code_series = pd.Series(self._central_forecast_codes).shift(1)

        out = pd.DataFrame(
            {
                "light_set": [v.light for v in self._applied],
                "temperature_set": [v.temperature for v in self._applied],
                "volume": [v.volume for v in self._applied],
                "fan": [v.fan for v in self._applied],
            }
        )
        for col in sensor_cols:
            out[col] = sensors_df[col]
        out["outside_temperature"] = outside_series
        out["central_forecast_text"] = central_text_series
        out["central_forecast_code"] = central_code_series
        return out

    def step(self, v: env_Settings) -> Sensors:
        if self.t >= self.n:
            raise RuntimeError("day is over; call reset()")

        # --- weather: advance the outside relax state for this minute ---
        self.weather_state.step(self.t)
        self._outside_temps.append(self.weather_state.outside_temp)
        fc_text, fc_code = self._central_forecast_lookup[self.t]
        self._central_forecast_texts.append(fc_text)
        self._central_forecast_codes.append(fc_code)

        # --- arrivals -----------------------------------------------------
        # base(t) scaled by weekday level*wobble (before reputation), then
        # by the existing reputation multiplier, then by the global arrival
        # scale (keeps a well-run day around half of capacity).
        lam = (self.lam if self.lam is not None
               else base(self.t, self._weekend) * self._weekday_mult
               * self.lam_mult[self.t] * self.arrival_scale
               * self.weather_state.rain_mult)
        new = _parties(self.rng, lam, self.group_sizes)
        new += [size for at, size in self.bookings if at == self.t]
        entered = sum(new)
        for size in new:
            if sum(s for s, _ in self.parties) + size <= self.capacity:
                self.parties.append((size, self.t))
            else:
                self.takeaway.append((size, self.t + self.rng.randint(1, 8)))

        # --- departures: one coin per party against current conditions --
        c = {
            "temperature": self.state["temperature"],
            "light": self.state["light"],
            "volume": self.state["noise"],
            "co2": self.state["co2"],
            "humidity": self.state["humidity"],
        }
        seated = sum(s for s, _ in self.parties)
        ideal = f(self.t, self.hidden)
        # Weather-shifted comfort: hotter than typical outside -> customers
        # want the room cooler; cooler/rainy outside -> warmer. Only temp shifts.
        nudge = WEATHER_ALPHA * (WEATHER_TYPICAL_OUTSIDE - self.weather_state.outside_temp)
        nudge = max(-self.weather_nudge_cap, min(self.weather_nudge_cap, nudge))
        ideal["temperature"] += nudge
        cl = closeness(c, ideal, self.weights)
        self._closeness.append(cl)
        p_t = hazard(cl, self.p_min, self.p_max)
        stayed = []
        for size, t0 in self.parties:
            if self.rng.random() >= p_t:
                stayed.append((size, t0))
            else:
                self.stays.append((t0, self.t - t0))
        waiting = [(s, out) for s, out in self.takeaway if out > self.t]
        left = seated - sum(s for s, _ in stayed) + sum(
            s for s, out in self.takeaway if out <= self.t
        )
        self.parties = stayed
        self.takeaway = waiting
        self.seated_by_min.append(sum(s for s, _ in self.parties))
        occupancy = self.seated_by_min[-1] + sum(s for s, _ in self.takeaway)

        # --- room dynamics ----------------------------------------------
        # Noise is instantaneous: dB levels combine on the power scale.
        self.state["noise"] = 10 * math.log10(
            10 ** (self.physics["noise"].outside / 10)
            + 10 ** (v.volume / 10)
            + occupancy * 10 ** (PERSON_DB / 10)
        )
        # Fan: each speed exits this fraction of the excess (gap to outside)
        # per minute; replaces the fixed leak for co2/humidity and adds a
        # pull toward outside on temperature.
        rate = self.leak + v.fan * (self.fan_max - self.leak) / 5
        for name, target in self._targets(v).items():
            p = self.physics[name]
            cur = self.state[name]
            if name in ("co2", "humidity"):
                self.state[name] = cur + rate * (target - cur) + p.k * occupancy
            elif name == "temperature":
                # The fan's tug is deliberately weak so the thermostat
                # always dominates (small warm offset, not a rival setpoint).
                # Pulls toward the weather layer's relaxed outside temp.
                self.state[name] = (cur + (target - cur) / p.tau
                                    + self.fan_temp_factor * (rate - self.leak)
                                    * (self.weather_state.outside_temp - cur)
                                    + p.k * occupancy)
            else:
                self.state[name] = cur + (target - cur) / p.tau + p.k * occupancy

        reading = {
            name: self.state[name] + self.rng.gauss(0, self.physics[name].sigma)
            for name in self.state
        }

        self.t += 1
        return Sensors(
            t=self.t - 1, entered=entered, left=left, occupancy=occupancy, **reading
        )

    def _targets(self, v: env_Settings) -> dict[str, float]:
        """Where each quantity is being pulled toward, given the settings."""
        # Outside light: the sun arc (peaks at physics["light"].outside lux
        # around midday, zero at night) dimmed by the weather layer's light
        # factor (cloud/rain).
        sun_arc = self.physics["light"].outside * S(self.t)
        return {
            "temperature": v.temperature,
            "humidity": self.weather_state.outside_hum,
            "co2": self.physics["co2"].outside,
            "light": v.light + sun_arc * self.weather_state.light_factor,
        }

    def weather(self) -> dict:
        """Public weather knowledge for today: date, weekday, and forecasts
        visible so far (only issues whose update_timestamp is at or before
        the current simulated wall-clock time -- no seeing the future)."""
        wall_clock = datetime.datetime(
            self.date.year, self.date.month, self.date.day, 8, 0
        ) + datetime.timedelta(minutes=self.t)
        f24, f2 = weather_mod.visible_forecasts(self._day_weather, wall_clock)
        return {
            "date": self.date,
            "weekday": self.weekday,
            "forecast_24h": f24,
            "forecast_2h": f2,
        }

    def latest_forecast_24h(self) -> pd.DataFrame:
        """Most recent 24-hour forecast issue as of the current sim minute
        (last row of weather()['forecast_24h']). If today has none visible
        yet, returns the previous calendar date's last issue, on every day
        of the run including day 1. Returns the empty today frame when that
        date has no issues either. Same no-future rule as weather()."""
        f24 = self.weather()["forecast_24h"]
        if len(f24):
            return f24.tail(1).reset_index(drop=True)
        # today is self.date; the previous calendar date is one day earlier.
        prev_date = self.date - datetime.timedelta(days=1)
        try:
            prev_weather = weather_mod.get_day_weather(prev_date)
        except Exception:
            return f24
        prev_all = weather_mod.with_rain_chance(prev_weather.forecast_24h)
        if len(prev_all):
            return prev_all.tail(1).reset_index(drop=True)
        return f24


    def next_day(self) -> None:
        """Close the day, settle banks, build tomorrow's arrival curve."""
        assert self.t == self.n, "day is not over yet"
        # Parties still seated count as leaving at closing.
        for size, t0 in self.parties:
            self.stays.append((t0, self.n - t0))
        # Score each block, split the deposit 50/50: half into the rolling
        # bank (as before), half into a persistent same-weekday bank. That
        # bank pays out a random 0-80% draw of its balance 7 days later (and
        # every 7 days after that, mirroring the rolling bank's mechanics).
        for b, score in enumerate(self._block_scores()):
            if score is None:
                score = self.empty_score  # empty hour counts as mildly bad
            deposit = self.deposit_scale * (score - self.z)
            half = deposit / 2
            bal = self.banks[b] + half
            self.banks[b] = max(-self.bank_cap, min(self.bank_cap, bal))
            weekday_bal = self.weekday_banks[b] + half
            self.weekday_banks[b] = max(-self.bank_cap, min(self.bank_cap, weekday_bal))
            self.weekday_fires[b].append(self.day + 7)
        # Morning draws -> payout bells -> arrival multiplier for the new day.
        r = [0.0] * self.n
        for b in range(self.blocks):
            pay = self.rng.uniform(0, self.draw) * self.banks[b]
            self.banks[b] -= pay
            m = b * 60 + self.rng.randrange(60)
            for t in range(self.n):
                r[t] += pay * bump(t, m, self.payout_width)
        self.day += 1
        self._new_day()  # sets up self._weekday_payouts for the new day
        for t in range(self.n):
            r[t] += self._weekday_payouts[t]
        self.lam_mult = [max(self.mult_floor, 1 + self.kappa * math.tanh(x / self.R))
                         for x in r]

    def day_score(self) -> dict:
        """EVAL ONLY, never for the agents: how the hidden grader saw the day so far.
        comfort = mean closeness to the answer key over the minutes played (1 = ideal);
        block = mean of the reputation block grades (dwell x crowd), None if no block had guests;
        stay = mean minutes seated for parties that have left."""
        comfort = (sum(self._closeness) / len(self._closeness)) if self._closeness else None
        blocks = [b for b in self._block_scores() if b is not None]
        stays = [d for _, d in self.stays]
        return {
            "comfort": round(comfort, 3) if comfort is not None else None,
            "block": round(sum(blocks) / len(blocks), 3) if blocks else None,
            "stay": round(sum(stays) / len(stays), 1) if stays else None,
        }

    def _block_scores(self) -> list[float | None]:
        """sqrt(dwell * crowd) per 1-hour block; None if no arrivals."""
        target = 1 / self.p_min  # minutes of stay worth a full dwell grade
        out = []
        for b in range(self.blocks):
            lo = b * 60
            grades = [min(st / target, 1) for t0, st in self.stays
                      if lo <= t0 < lo + 60]
            if not grades:
                out.append(None)
                continue
            c, k = self.crowd_ideal, self.capacity
            crowd = [
                max(0.0, 1 - abs((x - c) / (c if x < c else k - c))
                    ** self.crowd_shape)
                for x in self.seated_by_min[lo:lo + 60]
            ]
            out.append(math.sqrt(
                (sum(grades) / len(grades)) * (sum(crowd) / len(crowd))
            ))
        return out


def _parties(rng: random.Random, lam: float, sizes: dict[int, float]) -> list[int]:
    """Sizes of the parties arriving this timestep. Empty list is normal."""
    k = _poisson(rng, lam)
    return rng.choices(list(sizes), weights=list(sizes.values()), k=k)


def _poisson(rng: random.Random, lam: float) -> int:
    """Knuth's method — random.Random has no poisson."""
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1
