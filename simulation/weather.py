"""Weather layer: 2024 NEA forecasts -> minute-by-minute outside conditions.

Loaded once (module-level cache) and indexed by calendar date. Day n of a
run plays the nth date of 2024 (day 1 = 2024-01-01), wrapping after 366 days.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime
import math
import os

import pandas as pd

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "weather",
)
FILE_2H = os.path.join(DATA_DIR, "Historical2hourWeatherForecast2024.csv")
FILE_24H = os.path.join(DATA_DIR, "24hr", "Historical24hourWeatherForecast2024.csv")

BISHAN = "Bishan"

# category -> (light_factor, temp_blend, hum_blend)
# temp_blend b: target = clear_curve - b*(clear_curve - Tlo)
# hum_blend b: target = clear_curve + b*(Hhi - clear_curve)  (blend toward daily high)
CATEGORY_TRIOS: dict[str, tuple[float, float, float]] = {
    "fair": (1.0, 0.0, 0.0),
    "sunny": (1.0, 0.0, 0.0),
    "fair & warm": (1.0, 0.0, 0.0),
    "partly cloudy": (0.8, 0.1, 0.1),
    "cloudy": (0.55, 0.25, 0.25),
    "hazy": (0.55, 0.25, 0.25),
    "slightly hazy": (0.55, 0.25, 0.25),
    "mist": (0.55, 0.25, 0.25),
    "fog": (0.55, 0.25, 0.25),
    "light rain": (0.35, 0.7, 0.7),
    "drizzle": (0.35, 0.7, 0.7),
    "showers": (0.35, 0.7, 0.7),
    "light showers": (0.35, 0.7, 0.7),
    "passing showers": (0.35, 0.7, 0.7),
    "moderate rain": (0.35, 0.7, 0.7),
    "heavy rain": (0.2, 0.9, 0.9),
    "heavy showers": (0.2, 0.9, 0.9),
    "thundery showers": (0.2, 0.9, 0.9),
    "heavy thundery showers": (0.2, 0.9, 0.9),
    "heavy thundery showers with gusty winds": (0.2, 0.9, 0.9),
    "windy": (0.55, 0.25, 0.25),  # treated as Cloudy
}
DEFAULT_TRIO = CATEGORY_TRIOS["cloudy"]  # unrecognised -> Cloudy
LOOKAHEAD_MIN = 45  # minutes a heavy-blend category's target starts pulling early
RELAX_FRAC = 0.05  # fraction of remaining gap closed per minute (tau ~ 20 min)
LOOKAHEAD_BLEND_THRESHOLD = 0.7  # temp_blend >= this triggers look-ahead

# category (same 2h-forecast category used above) -> arrival rate multiplier.
# Applied minute by minute on top of the weekday level and reputation
# multiplier. Unknown/missing category = 1.0.
RAIN_ARRIVAL_MULT: dict[str, float] = {
    "fair": 1.0,
    "sunny": 1.0,
    "fair & warm": 1.0,
    "partly cloudy": 1.0,
    "cloudy": 0.95,
    "light rain": 0.75,
    "showers": 0.75,
    "rain": 0.75,
    "drizzle": 0.75,
    "heavy showers": 0.5,
    "thundery showers": 0.5,
}
DEFAULT_RAIN_ARRIVAL_MULT = 1.0

# forecast_text -> numeric rain-intensity percentage shown alongside forecast
# text in agent-visible views. Unknown text -> 10.
RAIN_CHANCE: dict[str, int] = {
    "fair": 0,
    "fair (day)": 0,
    "fair (night)": 0,
    "sunny": 0,
    "partly cloudy": 5,
    "cloudy": 10,
    "hazy": 5,
    "windy": 5,
    "light rain": 60,
    "light showers": 60,
    "drizzle": 60,
    "showers": 70,
    "passing showers": 70,
    "rain": 70,
    "heavy showers": 90,
    "heavy rain": 90,
    "thundery showers": 90,
}
DEFAULT_RAIN_CHANCE = 10


def _lookup_by_text(table: dict, forecast_text: str, default):
    """Case/substring lookup, longest key first, shared by the category tables."""
    if not forecast_text:
        return default
    text = str(forecast_text).strip().lower()
    if text in table:
        return table[text]
    for key in sorted(table, key=len, reverse=True):
        if key in text:
            return table[key]
    return default


def category_trio(forecast_text: str) -> tuple[float, float, float]:
    """Map a forecast_text to (light_factor, temp_blend, hum_blend), case/substring."""
    return _lookup_by_text(CATEGORY_TRIOS, forecast_text, DEFAULT_TRIO)


def rain_arrival_mult(forecast_text: str) -> float:
    """Map a forecast_text (2h-forecast category) to an arrival rate multiplier."""
    return _lookup_by_text(RAIN_ARRIVAL_MULT, forecast_text, DEFAULT_RAIN_ARRIVAL_MULT)


def rain_chance(forecast_text: str) -> int:
    """Map a forecast_text to a numeric rain-intensity percentage."""
    return _lookup_by_text(RAIN_CHANCE, forecast_text, DEFAULT_RAIN_CHANCE)


@dataclass
class DayWeather:
    """Everything the weather layer needs for one calendar date."""

    date: datetime.date
    t_lo: float
    t_hi: float
    h_lo: float
    h_hi: float
    # category schedule: list of (effective_from minutes-since-8am, category text)
    # sorted ascending; carries forward between entries
    categories: list[tuple[float, str]]
    forecast_24h: pd.DataFrame  # update_timestamp, temperature_low/high, ...
    forecast_2h: pd.DataFrame  # update_timestamp, forecast_text


class _WeatherStore:
    """Loads and indexes both CSVs once."""

    def __init__(self):
        self._loaded = False
        self._by_date: dict[datetime.date, DayWeather] = {}
        self._dates: list[datetime.date] = []
        self._avg_t_lo = 24.0
        self._avg_t_hi = 31.0
        self._avg_h_lo = 65.0
        self._avg_h_hi = 90.0

    def _load(self):
        if self._loaded:
            return
        self._loaded = True

        df24 = pd.read_csv(FILE_24H)
        df24["update_timestamp"] = pd.to_datetime(df24["update_timestamp"])
        df24["date"] = pd.to_datetime(
            df24["date"], format="mixed", dayfirst=True
        ).dt.date
        # one row per (date, update_timestamp) issue: dedupe the repeated
        # time_period rows, keep the shared temp/humidity fields
        issues24 = (
            df24.drop_duplicates(subset=["date", "update_timestamp"])
            .sort_values(["date", "update_timestamp"])
            .reset_index(drop=True)
        )
        if len(issues24):
            self._avg_t_lo = float(issues24["temperature_low"].mean())
            self._avg_t_hi = float(issues24["temperature_high"].mean())
            self._avg_h_lo = float(issues24["relative_humidity_low"].mean())
            self._avg_h_hi = float(issues24["relative_humidity_high"].mean())

        df2 = pd.read_csv(FILE_2H)
        df2 = df2[df2["location_name"] == BISHAN].copy()
        df2["update_timestamp"] = pd.to_datetime(df2["update_timestamp"])
        df2["timestamp"] = pd.to_datetime(df2["timestamp"])
        df2["date"] = pd.to_datetime(
            df2["date"], format="mixed", dayfirst=True
        ).dt.date
        df2 = df2.sort_values(["date", "timestamp"]).reset_index(drop=True)

        all_dates = sorted(
            set(issues24["date"]).union(df2["date"])
        )
        self._dates = all_dates

        for date in all_dates:
            day_open = datetime.datetime(date.year, date.month, date.day, 8, 0)

            day24 = issues24[issues24["date"] == date]
            if len(day24):
                first = day24.iloc[0]
                t_lo, t_hi = float(first["temperature_low"]), float(first["temperature_high"])
                h_lo, h_hi = float(first["relative_humidity_low"]), float(first["relative_humidity_high"])
                fcast24 = day24[[
                    "update_timestamp", "temperature_low", "temperature_high",
                    "relative_humidity_low", "relative_humidity_high",
                    "wind_speed_low", "wind_speed_high", "forecast_text",
                ]].reset_index(drop=True)
            else:
                t_lo, t_hi = self._avg_t_lo, self._avg_t_hi
                h_lo, h_hi = self._avg_h_lo, self._avg_h_hi
                fcast24 = pd.DataFrame(columns=[
                    "update_timestamp", "temperature_low", "temperature_high",
                    "relative_humidity_low", "relative_humidity_high",
                    "wind_speed_low", "wind_speed_high", "forecast_text",
                ])

            day2 = df2[df2["date"] == date]
            if len(day2):
                categories = [
                    (_minutes_since_open(row.timestamp, day_open), row.forecast_text)
                    for row in day2.itertuples(index=False)
                ]
                fcast2 = day2[["update_timestamp", "forecast_text", "forecast_code"]].reset_index(drop=True)
            else:
                categories = [(-1e9, "Cloudy")]
                fcast2 = pd.DataFrame(columns=["update_timestamp", "forecast_text", "forecast_code"])

            self._by_date[date] = DayWeather(
                date=date, t_lo=t_lo, t_hi=t_hi, h_lo=h_lo, h_hi=h_hi,
                categories=categories, forecast_24h=fcast24, forecast_2h=fcast2,
            )

    def date_for_day(self, day_index: int) -> datetime.date:
        """day_index: 0-based (day 1 of a run -> index 0) -> 2024-01-01 + index, wrapped."""
        self._load()
        return (datetime.date(2024, 1, 1) + datetime.timedelta(days=day_index % 366))

    def get(self, date: datetime.date) -> DayWeather:
        self._load()
        if date not in self._by_date:
            # date outside the loaded year range or fully missing: neutral day
            return DayWeather(
                date=date, t_lo=self._avg_t_lo, t_hi=self._avg_t_hi,
                h_lo=self._avg_h_lo, h_hi=self._avg_h_hi,
                categories=[(-1e9, "Cloudy")],
                forecast_24h=pd.DataFrame(columns=[
                    "update_timestamp", "temperature_low", "temperature_high",
                    "relative_humidity_low", "relative_humidity_high",
                    "wind_speed_low", "wind_speed_high", "forecast_text",
                ]),
                forecast_2h=pd.DataFrame(columns=["update_timestamp", "forecast_text", "forecast_code"]),
            )
        return self._by_date[date]


def _minutes_since_open(ts: pd.Timestamp, day_open: datetime.datetime) -> float:
    delta = ts.to_pydatetime().replace(tzinfo=None) - day_open
    return delta.total_seconds() / 60.0


_STORE = _WeatherStore()  # module-level singleton cache


RISE_WARP = 0.7  # warps the rising half-cosine so 8am sits ~25% up (not ~12%)


def clear_temp_curve(t: float, t_lo: float, t_hi: float) -> float:
    """Clear-sky outside temperature at minute t (0 = 8am). Min ~6am, max 3pm.

    Warped half-cosine rising 6am (t=-120) to 3pm (t=420) -- the warp front-
    loads the rise so 8am sits ~25% of the way up, as spec'd, while still
    bottoming out at 6am and peaking at 3pm. Slower half-cosine falling 3pm
    to 6am next day (18h = 1080 min), easing to roughly the midpoint by 10pm.
    """
    if t <= 420:
        frac = max(0.0, min(1.0, (t + 120) / 540.0)) ** RISE_WARP
        return t_lo + (t_hi - t_lo) * (1 - math.cos(math.pi * frac)) / 2
    # falling leg: 3pm -> 6am next day, 1080 minutes total
    frac = min((t - 420) / 1080.0, 1.0)
    return t_hi - (t_hi - t_lo) * (1 - math.cos(math.pi * frac)) / 2


def clear_hum_curve(t: float, h_lo: float, h_hi: float) -> float:
    """Clear-sky outside humidity: mirror of the temp curve (high at 6am, low at 3pm)."""
    # h_hi at 6am (t=-120), h_lo at 3pm (t=420) -- same shape, swap endpoints.
    return clear_temp_curve(t, h_hi, h_lo)


@dataclass
class WeatherState:
    """Per-Cafe mutable relax state for one run."""

    outside_temp: float = 0.0
    outside_hum: float = 0.0
    light_factor: float = 1.0
    rain_mult: float = 1.0  # arrival rate multiplier for the category in force now
    _day: DayWeather | None = field(default=None, repr=False)
    _schedule: list[tuple[float, str]] = field(
        default_factory=list, repr=False
    )  # (effective-from minute incl look-ahead, forecast_text)

    def start_day(self, day: DayWeather) -> None:
        """Reset relax state to the day's opening targets."""
        self._day = day
        self._schedule = _build_schedule(day.categories)
        text = self._text_at(0.0)
        light, temp_b, hum_b = category_trio(text)
        clear_t = clear_temp_curve(0.0, day.t_lo, day.t_hi)
        clear_h = clear_hum_curve(0.0, day.h_lo, day.h_hi)
        self.outside_temp = clear_t - temp_b * (clear_t - day.t_lo)
        self.outside_hum = clear_h + hum_b * (day.h_hi - clear_h)
        self.light_factor = light
        self.rain_mult = rain_arrival_mult(text)

    def _text_at(self, t: float) -> str:
        text = ""
        for start, tx in self._schedule:
            if start <= t:
                text = tx
            else:
                break
        return text

    def step(self, t: float) -> None:
        """Advance relax state to minute t (called once per simulated minute)."""
        day = self._day
        text = self._text_at(t)
        light_f, temp_b, hum_b = category_trio(text)
        clear_t = clear_temp_curve(t, day.t_lo, day.t_hi)
        clear_h = clear_hum_curve(t, day.h_lo, day.h_hi)
        target_t = clear_t - temp_b * (clear_t - day.t_lo)
        target_h = clear_h + hum_b * (day.h_hi - clear_h)
        target_l = light_f
        self.outside_temp += RELAX_FRAC * (target_t - self.outside_temp)
        self.outside_hum += RELAX_FRAC * (target_h - self.outside_hum)
        self.light_factor += RELAX_FRAC * (target_l - self.light_factor)
        self.rain_mult = rain_arrival_mult(text)


def _build_schedule(
    categories: list[tuple[float, str]]
) -> list[tuple[float, str]]:
    """Turn (effective-from minute, forecast_text) into (start, trio), with
    look-ahead: high-blend categories (temp_blend >= 0.7) take effect
    LOOKAHEAD_MIN minutes early (clamped to day start, t=0)."""
    out = []
    for start, text in categories:
        trio = category_trio(text)
        if trio[1] >= LOOKAHEAD_BLEND_THRESHOLD:
            start = max(0.0, start - LOOKAHEAD_MIN) if start > -1e8 else start
        out.append((start, text))
    out.sort(key=lambda x: x[0])
    return out


def date_for_day(day_index_1based: int) -> datetime.date:
    """day 1 -> 2024-01-01; wraps after 366 days."""
    return _STORE.date_for_day(day_index_1based - 1)


def weekday_name(date: datetime.date) -> str:
    return date.strftime("%A")


def get_day_weather(date: datetime.date) -> DayWeather:
    return _STORE.get(date)


SGT = datetime.timezone(datetime.timedelta(hours=8))


def with_rain_chance(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of df with a rain_chance column mapped from forecast_text,
    placed right next to it. df must have a forecast_text column (or be empty)."""
    out = df.copy()
    if "forecast_text" in out.columns:
        idx = out.columns.get_loc("forecast_text") + 1
        out.insert(idx, "rain_chance", out["forecast_text"].map(rain_chance))
    return out


def visible_forecasts(
    day: DayWeather, wall_clock: datetime.datetime
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(forecast_24h, forecast_2h) filtered to update_timestamp <= wall_clock,
    each carrying a rain_chance column next to forecast_text."""
    wc = pd.Timestamp(wall_clock)
    if wc.tzinfo is None:
        wc = wc.tz_localize(SGT)
    f24 = day.forecast_24h[day.forecast_24h["update_timestamp"] <= wc].reset_index(drop=True)
    f2 = day.forecast_2h[day.forecast_2h["update_timestamp"] <= wc].reset_index(drop=True)
    return with_rain_chance(f24), with_rain_chance(f2)
