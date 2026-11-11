# Venue reference

The cafe's controllable settings and the readings that come back, one row per minute.

## Settings (what the agent controls)

| Setting | Meaning | Unit / range |
|---|---|---|
| `light` | light level | lux |
| `temperature` | thermostat setpoint | °C |
| `volume` | music volume | dB |
| `fan` | ventilation fan speed | whole number 0 (off) – 5 (max) |

In the venue history these appear as `light_set`, `temperature_set`, `volume`, `fan`.

## Readings (what the sensors report)

| Column | Meaning | Range |
|---|---|---|
| `temperature_sensor` | room temperature | 0 – 70 °C |
| `humidity` | relative humidity | 0 – 100 %RH |
| `co2` | CO₂ level | 0 – 2000 ppm |
| `noise` | sound level | 30 – 130 dB |
| `light_sensor` | measured light (outside light shines in, so this ≠ the set light) | 0 – 1500 lux |
| `outside_temperature` | temperature outside the cafe | 20 – 36 °C |

## Crowd

| Column | Meaning |
|---|---|
| `entered` | people who walked in that minute |
| `left` | people who walked out that minute |
| `occupancy` | people inside at that minute |

A setting takes one minute to act on the room: minute `t`'s readings show what minute `t−1`'s settings produced.

## Intent, written by the navigation team

Every row also says which plan the settings came from, so "22 °C because planned" can be told apart from "22 °C because rain fired".

| Column | Meaning |
|---|---|
| `month` | month name of the date, e.g. `April` |
| `block_from` | minute of the day (0 = 08:00) when the plan block in force began |
| `block_reason` | that block's reason, in words |
| `set_by` | `daily` for the morning plan, `intraday` for an adjustment made during the day |
| `stretch` | 0–6: which 2-hour stretch of the day the minute is in (08–10 is 0, 20–22 is 6); the grid the morning baseline learns on |

Days recorded before 7 Sep 2026 have `stretch` empty.
