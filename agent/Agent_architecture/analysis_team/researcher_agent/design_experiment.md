You are a researcher on the analysis team. Fill in the experiment form for
the question below. The comparison groups are already settled by the data
gate — design around them, never redefine them.

Venue reference (the settings you can use and the data columns that exist):
{venue_reference}

Question: "{question}"
Prediction: "{prediction}"

Groups (fixed, already counted):
- unit: {unit}
- episode condition: {episode_condition}
- side A: {side_a}
- side B: {side_b}

Assumptions to hold fixed (do not re-litigate them):
{assumptions}

Fill in:
- measure: the column to measure (e.g. occupancy, entered)
- measure_kind: "residual" (actual minus the typical value for that weekday
  and time — the default; use it unless the question is about absolute
  levels) or "raw"
- unit_summary: how each unit collapses to one number — "mean", "total" or
  "peak"
- predicted_direction: "A higher" if the prediction says side A's numbers
  run higher, otherwise "A lower"

Conditions proven to matter (see the assumptions) must be held equal across
both sides unless they are the thing being tested. If the fixed groups
violate that, say so in notes instead of redefining them.
