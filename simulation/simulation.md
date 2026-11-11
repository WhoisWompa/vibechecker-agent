# 1. Core architecture

**[SETTLED]** The simulation is built around a hidden answer key the agent never sees.

```
f(t)                 -> v*        hidden ideal room conditions at time t (same every day)
physics(v̂, crowd)    -> c_t       actual room conditions at time t
g(c_t, t)            -> p_t       leave probability for each seated party this timestep
h(day's behaviour)   -> λ_next(t) next day's arrival rate curve, computed at end of day
base(t)              -> λ         default arrival curve; day 1 and every reset start here
```

- `f(t)` produces the ideal room **conditions** for time `t`. The agent's settings never touch `f` — the settings only steer the actual conditions through the physics.
- `g` measures how close the actual conditions sit to `f(t)` and pays out immediately through dwell. Closeness is `exp(−½ · weighted distance²)`, a number in [0, 1].
- `h` reads the day's **behaviour** — dwell and crowdedness — and pays out over the following days through arrivals (§8).

The two levers the agent holds:
- **Today**: good conditions keep the current crowd seated longer.
- **Tomorrow**: good conditions today pull a bigger crowd tomorrow.

---

## 2. What the simulator is responsible for

**[SETTLED]** The simulator does exactly three things. Everything else is agent-side or harness-side.

| | Responsibility |
|---|---|
| **Input** | Accept a settings vector at each timestep `t` |
| **Occupant process** | Rate at which people enter, how long they stay |
| **Output** | Sensor readings |

Effectively a `step(settings) -> sensors` loop. **(built)**

**[SETTLED]** The simulator accepts a new settings vector at **every** timestep. **(built)**

**[SETTLED] Input and output table.** A day's settings are one table: one row per minute, one column per actuator (light, temperature, volume, fan). The simulator hands back the same table with the sensor readings added as new columns next to the settings (temperature, humidity, co2, noise, light, entered, left, occupancy).

**[SETTLED] Schedules and live updates. (built)**
- A schedule table can cover any stretch of the day, not just the whole day. Row 0 applies to the next minute that hasn't been played yet. If the table runs out before the day ends, the last row's settings stay in force.
- A new schedule can be pushed at any point during the day. It replaces the old plan from the current minute to the end of the day.
- Time moves only when asked: `tick(k)` plays the next `k` minutes on whatever schedule is in force. `day_df()` returns the table of everything played so far, in the format above.
- A fresh day has no schedule; playing it before pushing one is an error.
- To play a whole day in one go: set a full-day schedule once and tick through to closing.

Since a setting takes a minute to change the room, each row's sensor readings show what the *previous* row's settings produced. So the first row has no sensor readings yet, and the settings on the very last row never get a sensor reading of their own. Light and temperature appear on both sides, so the settings columns are named `light_set` and `temperature_set` and the matching sensor columns are named `light_sensor` and `temperature_sensor`.

---

## 3. Sensors and actuators

### Sensors (what the agent observes)
**[SETTLED] (built — all six)**

- Temperature sensor
- Humidity sensor
- CO₂ sensor
- Noise sensor
- Light sensor (outside light shines in, so measured light ≠ set light)
- Door counter — reports how many people entered/left at time t, and the occupancy count itself

**[SETTLED] Sensor ranges.** Each reading sits inside a fixed range. Readings are clipped to these bounds.

| Sensor | Range | Unit |
|---|---|---|
| Light | 0 – 1500 | lux |
| Noise | 30 – 130 | dB |
| Temperature | 0 – 70 | °C |
| CO₂ | 0 – 2000 | ppm |
| Humidity | 0 – 100 | %RH |

### Actuators (`v̂` — what the agent sets)
**[SETTLED] (built)** 4 dimensions:

1. Light level
2. Room temperature (setpoint)
3. Music volume
4. Ventilation fan — **integer speed 0–5** (0 = off), settable every timestep **(built)**

---

## 4. Physical dynamics

### Lumped first-order model
**[SETTLED] (built)** Temperature, humidity, and CO₂ treat the room as one well-mixed body relaxing exponentially toward a target:

```
next = current + (dt/tau) * (target - current) + k * occupancy
```

`tau` is the time constant (order 20–30 min for a cafe). The occupancy term is the crowd pushing the value off target. With a steady crowd the room settles at `target + k·occupancy·tau`; the constants are sized so a full house (30 people) sits about +1.5 °C, +7.5 % humidity, and +1350 ppm CO₂ above target.

**(built)** Each of these carries its own `tau`, `k`, an outside/ambient value it sits at when empty, and a sensor-noise spread.

**[SETTLED] (built) Ventilation.** The fan swaps room air for outside air. Each speed sets the fraction of the *excess* (the gap to outside air) that exits per minute:

```
rate(speed) = leak + speed · (fan_max − leak) / 5
```

Defaults: `leak` = 4%/min (speed 0, a leaky building — equivalent to the existing tau ≈ 25 min) up to `fan_max` = 17%/min at speed 5 (tau ≈ 6 min). Both are cafe settings.

- CO₂ and humidity: their pull toward outside runs at `rate(speed)` instead of the fixed `1/tau`.
- Temperature: the fan adds a second, much weaker pull, `0.1 · (rate(speed) − leak) · (T_outside − T)`, alongside the thermostat pull. The thermostat always dominates: the room converges near the setpoint, with the fan holding a small warm offset — about +1.5 °C at full blast, +0.6 °C at speed 2 (with outside 6 °C above the setpoint). At speed 0 the pull is zero (leak losses are already inside the thermostat's `tau`).

**(built)** Noise and light are instantaneous. Noise combines its sources on the power scale (how decibels actually add):

```
noise = 10·log₁₀( 10^(35/10) + 10^(music dB/10) + occupancy · 10^(58/10) )
```

— 35 dB empty-room ambient, the music at its set level, and 58 dB per talking person.

**(built)** Where each quantity is pulled toward:

| Quantity | Target |
|---|---|
| Temperature | the setpoint the agent set |
| Light | set light + outside light — so the reading is never just the setting |
| Noise | ambient + music + crowd, combined as above |
| Humidity, CO₂ | ambient; they only move because occupants push them |

The output of the physics is the actual room conditions `c_t` — the same quantities the sensors noisily report, and the input to `g` and `h`.

---

## 5. Time

**[SETTLED]** `f(t)` is the **same function every day.**

**[SETTLED]** `t = 0` is **8am**, always. The latest a day can run to is midnight, so with 1-minute timesteps the widest possible day is **960 timesteps**. A cafe that opens later than 8am still counts `t` from 8am — the timesteps before it opens are recorded as N/A.

**[SETTLED]** Timesteps are 1 minute. `f` is defined in continuous minutes, so a different step size only changes where it is sampled.

**[SETTLED]** One **run** is a sequence of days. Day 1 always runs on the default arrival curve `base(t)`. Each day's conditions produce the next day's arrival curve. A **reset** wipes the arrival curve back to `base(t)`.

**[SETTLED] (built)** The agent commits a schedule for as much of the day as it wants, can push a new one at any point during the day (§2), and reviews the data at end of day.

---

## 6. Arrival process

**[SETTLED]** The arrival rate curve `λ(t)` is **frozen before the day starts**. Nothing that happens during the day changes today's arrivals — today's conditions shape **tomorrow's** curve (§8).

**[SETTLED] (built)** No continuous-time process. Arrivals are a **count per timestep**. People arrive in **parties**:

```
K_t ~ Poisson(λ(t))       how many parties walk up during timestep t
G_i ~ group-size table    how many people are in each party
```

`λ` counts **parties per timestep, not people per timestep.**

**[SETTLED] (built)** The default curve, `t` in minutes since 8am, day length 840 minutes (8am – 10pm):

```
base(t) = 0.15                        quiet floor
        + 0.30 · bump(t,  30, 40)     breakfast, 8:30am
        + 0.75 · bump(t, 270, 60)     lunch, 12:30pm
        + 0.60 · bump(t, 660, 75)     dinner, 7pm
```

where `bump(t, c, w) = exp(−(t − c)² / (2w²))` — the same hill shape used by `f(t)`.

**[SETTLED] (built)** The whole arrival rate is scaled by a tunable `ARRIVAL_SCALE` (default 0.35), sized so a well-run day hovers around half of capacity.

**[SETTLED] (built)** The group-size table is a fixed set of weights — mostly 1s and 2s, a few 3s and 4s — the same all day.

**[SETTLED] (built)** The cafe has 30 seats (adjustable when creating the cafe). A party arriving to a full house does a takeaway instead: they stay 1–8 minutes (random) and then leave on their own — room conditions don't affect them. While waiting they still count toward occupancy and the room physics.

---

## 7. Leaving process (hazard model)

**[SETTLED] (built)** Dwell is not decided at arrival. Every timestep, each seated party flips one coin and leaves with probability `p_t`:

```
p_t = p_min + (p_max − p_min) · (1 − closeness(c_t, f(t)))
```

- Perfect conditions → hazard `p_min = 1/60`; the mean stay is `1/p_min` timesteps (~60-minute stays).
- Terrible conditions → hazard `p_max = 1/8` (~8-minute stays).
- One coin per **party** — the whole table leaves together.

The hazard responds to **current** conditions: if the room turns bad mid-afternoon, the room drains while it is bad.

**[SETTLED] (built)** Crowding does not enter the hazard — guests tolerate a packed room and stay. Crowding costs the cafe through the nightly crowd grade (§8) instead.

**[SETTLED] (built) Air freshness.** CO₂ and humidity are two more dimensions in the closeness score, with one difference from the other three: they are **one-sided**. Each has a hidden comfort point, flat across the whole day. At or below the point the dimension contributes nothing; above it, the excess feeds the same bell-curve distance formula as the other dimensions. Fresh air is neutral, never a bonus.

---

## 8. Next-day arrivals: `h`

**[SETTLED] (built) Block score, two ingredients.** The day splits into 1-hour blocks. Each block gets one score per day:

```
score = √(dwell × crowd)
```

**Dwell** — graded per party, credited to the hour the party arrived in:

```
party grade = min(stay in minutes / (1/p_min), 1)
dwell = average party grade over the block's arrivals

The full-marks bar `1/p_min` is the best average stay the hazard allows (60 minutes at the default `p_min = 1/60`), so it moves automatically if `p_min` changes. **(built)**
```

A block with no arrivals gets no score that day and its bank is untouched.

**Crowd** — graded per minute, averaged over the block's 60 minutes:

```
minute grade = max(0, 1 − |(x − c) / r|⁴)     r = c below the ideal, k − c above it
```

`x` = people seated, `k` = capacity. The ideal fullness `c` is hidden and rolled once per run, uniform between 45% and 55% of capacity. The grade is 1 at the ideal and falls to 0 at an empty room and at a full one. The exponent 4 sets the shape: flat near the ideal, dropping fast near the edges.

**[SETTLED] (built) Reputation banks.** Each block has its own bank, starting at 0 on day 1.

- End of day: each block deposits `20 × (score − z)`. Scores above `z` add, below subtract.
- A block with no arrivals is scored as a mildly bad hour instead of being skipped.
- Balances are clamped to ±15 (cap is a cafe setting; smaller cap = reputation fades faster).
- Before each new day: each block draws a uniformly random amount between 0 and 80% of its balance and spends it. Unspent balance stays for later days, so a good day can surface one, two, or three days later. Negative balances draw negative payouts.
- The payout lands at a uniformly random minute inside the block, then spreads around that minute as a normal bell, width ~25 min — wide enough to lift most of the block's hour.

**[SETTLED] Severity knob.** One cafe setting, `severity` (0–1, default 0.5), sets how punishing reputation is. It drives three internals, each sliding between a forgiving and a harsh value (anchors at severity 0 / 0.5 / 1):

| internal | 0 (forgiving) | 0.5 (balanced) | 1 (punishing) |
|---|---|---|---|
| flip point `z` | 0.20 | 0.35 | 0.45 |
| empty-hour score | `z` (no penalty) | 0.275 | 0.20 |
| arrival multiplier floor | 0.50 | 0.25 | 0.05 |

At 0, bad play barely hurts. At 0.5, ruining a cafe and recovering both take ~4–5 days. At 1, two bad days take ~two weeks to undo. The multiplier floor keeps a trickle of walk-ins at every severity, so a ruined cafe can always climb back; a great reputation self-erodes through overcrowding — neither end locks in.

**[SETTLED] (built) Multiplier.** The payout `r` at minute `t` moves arrivals through:

```
λ_next(t) = base(t) × (1 + κ · tanh(r / R)),    R = 1
```

`tanh` squashes smoothly, so the multiplier always stays between `1 − κ` and `1 + κ`. `κ` is a cafe setting from 0 to 1, default 1: at 0 reputation barely moves arrivals, at 1 it has full effect.

**[SETTLED] (built) Multi-day loop.** A run = many days on one hidden roll, one crowd ideal, one set of cafe settings.

- Day end (minute 840): parties still seated count as leaving at closing; blocks score; deposits land.
- Overnight: room air returns to outside conditions; banks draw payouts; the new day's arrival curve is built.
- Carries across days: bank balances, the hidden roll, the crowd ideal.
- A reset starts a fresh run: banks to 0, day counter to 0, new hidden roll unless one is passed in.

**[SETTLED]** The simulator has no built-in goal or reward signal. It reports outcomes (sensors, entered, left, occupancy); every entering customer counts the same. Goals live with the agent.

- `base(t)` is the default daily curve and the reset state; day 1 runs on it with empty banks.

---

## 9. The hidden ideal `f(t)`

**[SETTLED] (built)** `f(t)` produces three ideal conditions — light, temperature, volume. Each is a smooth curve built from two helpers, with `t` in minutes since 8am:

**Sun arc** (sunrise 7am, sunset 7:15pm — Singapore):

```
S(t) = sin( π · (t + 60) / 735 )    for −60 ≤ t ≤ 675, else 0
```

**Bump** (smooth hill centred at `c`, width `w` minutes):

```
bump(t; c, w) = exp( −(t − c)² / (2w²) )
```

**The three curves:**

```
temp*(t)  = T₀ − A_T·S(t − 60) − E₁·bump(t; c₁, 45) − E₂·bump(t; c₂, 45)   [°C]
light*(t) = L₀ − A_L·S(t)                                             [lux]
vol*(t)   = V₀ + B₁·bump(t; c₁, 45) + B₂·bump(t; c₂, 75)              [dB]
```

Temperature and light dip at midday, with the temperature valley landing 1 hour after the light valley; temperature also dips a little at lunch and evening; volume lifts around lunch and evening. The same `S(t)` also drives the ambient outside light in the physics (§4).

**[SETTLED] The shapes are fixed; the exact numbers are hidden and rolled once per run.** At the start of a run each parameter is drawn from its range below, then held fixed for the whole run. The agent can guess the shape from common sense, but must find this run's exact numbers by trying settings and watching the response.

| Parameter | Meaning | Range |
|---|---|---|
| `T₀` | baseline ideal temp | 23.5 – 26 °C |
| `A_T` | midday temp dip | 1 – 4 °C |
| `L₀` | baseline ideal light | 450 – 800 lux |
| `A_L` | midday light dip | 250 – 600 lux |
| `V₀` | baseline ideal volume | 58 – 68 dB |
| `E₁`, `E₂` | lunch / evening temp dip | 0.5 – 1.5 °C each |
| `B₁`, `B₂` | lunch / evening volume lift | 5 – 15 / 8 – 20 dB |
| `c₁`, `c₂` | lunch / evening bump centres | 11:30am – 1pm / 7pm – 9pm |
| `co2_comfort` | CO₂ comfort point (flat, one-sided — §7) | 600 – 800 ppm |
| `humidity_comfort` | humidity comfort point (flat, one-sided — §7) | 75 – 80 %RH |

---

## 10. Weather layer

**[SETTLED] (built)** Each simulated day plays one real 2024 date, in order: day 1 = 1 Jan 2024 by default, day 2 the next date, and so on. No repeating days.

**[SETTLED] (built) Start date.** `set_start_date("2024-03-15")` makes day 1 play that real date; following days follow the calendar. Works before or after `reset()`; refuses if minutes of the day have already been played. `reset()` keeps the chosen start date.

**[SETTLED] Data sources.** Two NEA datasets from data.gov.sg, stored in `data/weather/`:

- 24-hour forecast (2024): https://data.gov.sg/datasets/d_0e3254694f5c7eaf77478e5fbd580f64/view (collection: https://data.gov.sg/collections/2213/view) — per day: temperature high/low, humidity high/low, wind, forecast text per region per 6-hour window.
- 2-hour forecast (2024): https://data.gov.sg/datasets/d_d3e4808d740fc2b838f88b2e46e02d93/view (collection: https://data.gov.sg/collections/2179/view) — forecast text every ~30 minutes per area. The simulation uses the **Bishan** area.

**[SETTLED]** The weather layer turns each date's forecasts into minute-by-minute outside conditions and feeds three of the physics' outside slots:

1. **Outside temperature** — daily curve anchored to the day's forecast low and high.
2. **Outside humidity** — moves within the day's forecast low/high band.
3. **Outside light** — the sun arc dimmed by cloud and rain from the 2-hour forecast.

Outside CO₂ stays constant.

**[SETTLED] (built) Forecast → minute-by-minute conditions.**
- Outside temperature and humidity follow a smooth daily curve anchored to the day's forecast low and high (coolest ~6am, warmest ~3pm; humidity mirrored).
- Each 2-hour forecast category maps to a trio of targets: light factor, temperature pull, humidity pull. First-pass values (tunable):

| Category | Light factor | Temperature | Humidity |
|---|---|---|---|
| Fair / Sunny | 1.0 | normal curve | normal curve |
| Partly Cloudy | 0.8 | tiny dip | slight rise |
| Cloudy | 0.55 | ~1 °C dip | modest rise |
| Light Rain / Showers | 0.35 | well down | well up |
| Heavy / Thundery Showers | 0.2 | near daily low | near daily high |

- Outside conditions relax toward the targets a fixed fraction per minute (no jumps). Rainy slots start pulling 30–60 minutes before they begin (look-ahead); when the forecast clears, conditions relax back to the normal curve.
- Gaps in the data: carry the last forecast forward. A fully missing day plays as a neutral cloudy day using yearly-average high/low.

**[SETTLED] (built) Rain cuts arrivals.** Each minute, the arrival rate is multiplied by a factor from the 2-hour forecast category in force (first-pass, tunable): Fair/Sunny 1.0, Partly Cloudy 1.0, Cloudy 0.95, Light Rain/Showers 0.75, Heavy/Thundery Showers 0.5. Applies on top of the weekday level and reputation multiplier.

**[SETTLED] (built) Weather-shifted comfort.** The customers' ideal temperature nudges opposite to outside heat:

```
ideal temp = normal ideal + α · (typical outside temp − outside temp now)
```

with `α ≈ 0.3` and typical daytime outside ~29.5 °C (both tunable). Hotter than typical outside → customers want the room cooler; cooler/rainy outside → they want it warmer. The nudge follows outside temperature minute by minute. All other parts of the hidden ideal are untouched by weather.

**[SETTLED] (built) Day-of-week crowds.** Each weekday has a typical crowd level scaling the arrival curve: Mon 0.7, Tue 0.75, Wed 0.8, Thu 0.85, Fri 1.1, Sat 1.25, Sun 1.15 (first-pass, tunable). Each actual day rolls a random wobble of ±10% on its typical level.

**[SETTLED] (built) Weekend rhythm.** On Saturday and Sunday the lunch bump becomes a brunch bump (11am–1pm) and the dinner bump is smaller than on weekdays.

**[SETTLED] (built) Public holidays and eves.** The 12 Singapore 2024 public holiday dates (1 Jan; 10–12 Feb; 29 Mar; 10 Apr; 1 May; 22 May; 17 Jun; 9 Aug; 31 Oct; 25 Dec) play at a 1.4 crowd level with the weekend rhythm, whatever the weekday. Christmas Eve (24 Dec) and New Year's Eve (31 Dec) play at 1.15 with the normal weekday rhythm. The usual ±10% wobble applies on top. Levels are tunable.

**[SETTLED] (built) What the agent can see (weather).** Weather is public knowledge:
- The calendar date and weekday of the current simulated day.
- Forecasts **as they are issued**, never ahead of time: the 24-hour forecast from its morning issue (updating through the day as new issues land), and each 2-hour Bishan forecast from its half-hour issue time.
- A live **outside temperature** reading each minute (noisy, like the indoor sensors), delivered as an extra column in the day table.
- A convenience call returning only the **latest 24-hour forecast issue** as of the current minute (falling back to the previous day's last issue before today's first one lands).
- Every forecast text (2-hour and 24-hour views) carries a `rain_chance` column beside it: a percentage translating the text (Fair 0, Partly Cloudy 5, Cloudy 10, Light Rain 60, Showers 70, Heavy/Thundery 90; tunable).
- The day table holds one day only: each new day starts a fresh, clean dataframe.
- The day table carries two columns, `central_forecast_text` and `central_forecast_code`: the latest-issued Bishan 2-hour forecast in force at that minute, predicting the upcoming ~2-hour window. New values appear only when an issue is published (~every 30 minutes). These are the only forward-looking columns; all sensor columns report the past minute only.

**[SETTLED] (built) Weekly reputation echo.** Each block's end-of-day deposit splits 50/50:
- half into a **same-weekday bank**, a persistent balance per block (clamped ±15, same cap as the rolling bank). Every time that weekday comes around again (7 days after a deposit), the bank pays out a random 0-80% draw of its current balance, landing at a random minute in the block with the same ~25-min bell spread as the rolling bank. The rest stays banked for the next same weekday;
- half into the existing rolling bank (§8), which fades over the following days as before.

---

## 11. Open items

- **[SETTLED] (built) Distance weights.** Each dimension has a tolerance; weight = 1/tolerance². Defaults: 1.5 °C, 100 lux, 8 dB, 500 ppm CO₂, 8 %RH humidity. Can be overridden when creating the cafe. One set of weights serves both `g` and `h`.
- **[OPEN] Parameter ranges** in §9 are first-pass numbers; adjust when tuning.
