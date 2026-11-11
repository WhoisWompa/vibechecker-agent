# How to use the simulation

The simulation is a cafe. The agent controls four room settings and receives sensor readings back, one row per minute. The cafe is a black box: the agent only ever sees what is written on this page.

## Setup

```python
from simulation.env import Cafe

cafe = Cafe()              # 30 seats by default
cafe = Cafe(capacity=50)   # or set the number of seats
cafe.reset()               # start a fresh run at day 1
```

The number of seats is known to the agent — it is the owner's own floor plan.

## Time

- One timestep = 1 minute.
- `t = 0` is 8am. A day is 840 minutes long (8am – 10pm).

## Settings the agent sends

A schedule is a pandas DataFrame with one row per minute and these four columns:

| Column | Meaning | Type / range |
|---|---|---|
| `light` | light level | lux |
| `temperature` | thermostat setpoint | °C |
| `volume` | music volume | dB |
| `fan` | ventilation fan speed | whole number 0 (off) – 5 (max) |

```python
cafe.set_schedule(df)
```

- The table can cover any stretch of the day — the whole day, or just the next stretch. Row 0 applies to the next minute that hasn't been played yet.
- If the table runs out before the day ends, the last row's settings stay in force.
- A new schedule can be pushed at any point during the day. It replaces the old plan from the current minute to the end of the day.

## What the agent receives

```python
df = cafe.day_df()   # everything played so far today
```

One row per played minute. The four settings columns come back as `light_set`, `temperature_set`, `volume`, `fan`, plus these reading columns:

| Column | Meaning | Range |
|---|---|---|
| `temperature_sensor` | room temperature | 0 – 70 °C |
| `humidity` | relative humidity | 0 – 100 %RH |
| `co2` | CO₂ level | 0 – 2000 ppm |
| `noise` | sound level | 30 – 130 dB |
| `light_sensor` | measured light (outside light shines in, so this ≠ the set light) | 0 – 1500 lux |
| `outside_temperature` | temperature outside the cafe | 20 – 36 °C |
| `entered` | people who walked in that minute | count |
| `left` | people who walked out that minute | count |
| `occupancy` | people inside at that minute | count |

Row `t`'s readings show what row `t−1`'s settings produced (a setting takes one minute to act on the room). Row 0 has no readings, and the final minute's settings never get a reading row of their own.

## Weather (public knowledge)

The cafe sits in Bishan, Singapore. Each simulated day is a real calendar date in 2024, played in order from 1 Jan 2024. Weather affects the room: outside air leaks in, sunlight shines in, and the crowd's comfort shifts with the day.

```python
cafe.weather()
```

Returns a dict with the forecasts published **so far** — new issues appear as the day plays, never in advance:

| Key | Meaning |
|---|---|
| `date` | today's calendar date |
| `weekday` | Monday – Sunday |
| `forecast_24h` | table of today's island-wide 24-hour forecast issues (a fresh issue roughly every 6 hours) |
| `forecast_2h` | table of today's 2-hour Bishan forecast issues (a fresh issue roughly every 30 minutes) |

`forecast_24h` columns:

| Column | Meaning | Range |
|---|---|---|
| `temperature_low`, `temperature_high` | expected outside temperature band for the day | °C, roughly 22 – 36 |
| `humidity_low`, `humidity_high` | expected outside humidity band for the day | %RH, roughly 50 – 100 |
| `wind_speed_low`, `wind_speed_high` | expected wind band | km/h, 5 – 30 |
| `forecast_text` | expected weather in words | e.g. Fair, Cloudy, Showers, Thundery Showers |

`forecast_2h` columns:

| Column | Meaning |
|---|---|
| `issued_at` | minute the forecast came out |
| `forecast_text` | expected Bishan weather for the next 2 hours — one of: Fair, Partly Cloudy, Cloudy, Light Rain, Showers, Thundery Showers (and similar wordings) |

Rainier categories go with a cooler, damper, darker outside; fair categories with a hotter, brighter one.

```python
cafe.latest_forecast_24h()
```

Returns just the most recent 24-hour forecast issue as of the current minute (same columns as `forecast_24h`). At 8am this is the early-morning issue; after a midday refresh has come out, it returns that one instead. Forecasts only ever appear once published — never ahead of time.

## Days and runs

```python
cafe.next_day()   # close the day, start the next; only after all 840 minutes are played
cafe.reset()      # wipe everything and start a fresh run at day 1
```

A run is a sequence of days played back-to-back with `next_day()`. Each new day starts with no schedule — push one before ticking.
`output example.xlsx` in this folder shows two finished days so the layout is unambiguous.