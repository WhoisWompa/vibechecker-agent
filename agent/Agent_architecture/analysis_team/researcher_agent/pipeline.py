"""Deterministic experiment pipeline: fetch, build units, summarize, and
compute statistics. Pure code except two thin fetch helpers that call the
data handler's LLM-backed query."""

import math
import random

try:
    from scipy import stats as scipy_stats
except ImportError:
    scipy_stats = None


def fetch_side(data_handler, side_definition: str, columns: list[str]):
    """Fetch one comparison side through the data handler's query method."""
    return data_handler.query(
        f"All rows where {side_definition}. Columns: {', '.join(columns)}."
    )


def drop_first_minute(df):
    """Drop rows for the first minute of each day (settings take a minute to act)."""
    return df[df["time"] != "08:00"].reset_index(drop=True)


def _minutes(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def build_units(df, unit: str, gap: int = 10) -> list:
    """Split a fetched side into per-unit frames: one per day, or one per
    episode (a continuous stretch on the same date with no time gap > gap
    minutes)."""
    if unit == "day":
        return [group for _, group in df.groupby("date", sort=False)]

    units = []
    for _, day_df in df.groupby("date", sort=False):
        day_df = day_df.sort_values("time")
        current_rows = []
        prev_minutes = None
        for _, row in day_df.iterrows():
            mins = _minutes(row["time"])
            if prev_minutes is not None and mins - prev_minutes > gap:
                units.append(_to_frame(current_rows))
                current_rows = []
            current_rows.append(row)
            prev_minutes = mins
        if current_rows:
            units.append(_to_frame(current_rows))
    return units


def _to_frame(rows):
    import pandas as pd
    return pd.DataFrame(rows).reset_index(drop=True)


def typical_curve(df_history, measure) -> dict:
    """Mean of measure per (weekday, 30-minute slot)."""
    slots = df_history["time"].apply(lambda t: _minutes(t) // 30)
    grouped = df_history.assign(_slot=slots).groupby(["weekday", "_slot"])[measure].mean()
    return {key: value for key, value in grouped.items()}


def summarize_unit(unit_df, measure, measure_kind, unit_summary, curve) -> float:
    if measure_kind == "residual":
        overall_mean = sum(curve.values()) / len(curve) if curve else 0.0
        values = []
        for _, row in unit_df.iterrows():
            slot = _minutes(row["time"]) // 30
            typical = curve.get((row["weekday"], slot), overall_mean)
            values.append(row[measure] - typical)
    else:
        values = list(unit_df[measure])

    if unit_summary == "mean":
        return sum(values) / len(values)
    if unit_summary == "total":
        return sum(values)
    if unit_summary == "peak":
        return max(values)
    raise ValueError(f"unknown unit_summary: {unit_summary}")


def count_units(data_handler, plan) -> dict:
    counts = {}
    for label, side in (("A", plan.side_a), ("B", plan.side_b)):
        df = fetch_side(data_handler, side, ["date", "time"])
        df = drop_first_minute(df)
        units = build_units(df, plan.unit)
        counts[label] = len(units)
    return counts


def compute_statistics(a: list, b: list) -> dict:
    na, nb = len(a), len(b)
    mean_a = sum(a) / na
    mean_b = sum(b) / nb
    effect = mean_a - mean_b

    va = sum((x - mean_a) ** 2 for x in a) / (na - 1)
    vb = sum((x - mean_b) ** 2 for x in b) / (nb - 1)
    se = math.sqrt(va / na + vb / nb)

    df_num = (va / na + vb / nb) ** 2
    df_den = (va / na) ** 2 / (na - 1) + (vb / nb) ** 2 / (nb - 1)
    df = df_num / df_den if df_den else na + nb - 2

    if scipy_stats is not None:
        crit = scipy_stats.t.ppf(0.975, df)
    else:
        crit = 1.96

    ci_low = effect - crit * se
    ci_high = effect + crit * se

    rng = random.Random(0)
    pooled = list(a) + list(b)
    count_extreme = 0
    for _ in range(10000):
        rng.shuffle(pooled)
        perm_a = pooled[:na]
        perm_b = pooled[na:]
        perm_effect = sum(perm_a) / na - sum(perm_b) / nb
        if abs(perm_effect) >= abs(effect):
            count_extreme += 1
    p_value = (count_extreme + 1) / 10001

    return {
        "effect": effect,
        "se": se,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "p_value": p_value,
        "n_per_side": {"A": na, "B": nb},
    }


def derive_verdict(ci_low, ci_high, predicted_direction):
    if ci_low > 0:
        return predicted_direction == "A higher"
    if ci_high < 0:
        return predicted_direction == "A lower"
    return None


def run_experiment(data_handler, plan, form) -> dict:
    columns = ["date", "weekday", "time", form.measure]
    df_a = drop_first_minute(fetch_side(data_handler, plan.side_a, columns))
    df_b = drop_first_minute(fetch_side(data_handler, plan.side_b, columns))

    units_a = build_units(df_a, plan.unit)
    units_b = build_units(df_b, plan.unit)

    curve = {}
    if form.measure_kind == "residual":
        df_history = fetch_side(data_handler, "all rows", ["date", "weekday", "time", form.measure])
        curve = typical_curve(df_history, form.measure)

    values_a = [summarize_unit(u, form.measure, form.measure_kind, form.unit_summary, curve) for u in units_a]
    values_b = [summarize_unit(u, form.measure, form.measure_kind, form.unit_summary, curve) for u in units_b]

    statistics = compute_statistics(values_a, values_b)
    verdict = derive_verdict(statistics["ci_low"], statistics["ci_high"], form.predicted_direction)

    return {
        "statistics": statistics,
        "verdict": verdict,
        "values": {"A": values_a, "B": values_b},
    }


if __name__ == "__main__":
    import pandas as pd

    # -- drop_first_minute --
    df = pd.DataFrame({
        "date": ["2024-01-01"] * 3 + ["2024-01-02"] * 3,
        "weekday": ["Monday"] * 3 + ["Tuesday"] * 3,
        "time": ["08:00", "08:01", "08:02", "08:00", "08:01", "08:02"],
        "occupancy": [0, 10, 12, 0, 20, 22],
    })
    dropped = drop_first_minute(df)
    assert "08:00" not in list(dropped["time"]), dropped
    assert len(dropped) == 4, len(dropped)

    # -- build_units: day --
    day_units = build_units(dropped, "day")
    assert len(day_units) == 2, len(day_units)
    assert len(day_units[0]) == 2 and len(day_units[1]) == 2

    # -- build_units: episode, splits on >10 min gap, stitches <=10 min gap --
    ep_df = pd.DataFrame({
        "date": ["2024-01-01"] * 5,
        "weekday": ["Monday"] * 5,
        "time": ["09:00", "09:05", "09:15", "09:40", "09:45"],
        "occupancy": [1, 2, 3, 4, 5],
    })
    # gaps: 09:00->09:05 = 5 (stitch), 09:05->09:15 = 10 (stitch, not > 10),
    # 09:15->09:40 = 25 (split), 09:40->09:45 = 5 (stitch)
    ep_units = build_units(ep_df, "episode")
    assert len(ep_units) == 2, [len(u) for u in ep_units]
    assert len(ep_units[0]) == 3, len(ep_units[0])
    assert len(ep_units[1]) == 2, len(ep_units[1])

    # -- typical_curve and residual summarize --
    hist = pd.DataFrame({
        "weekday": ["Monday", "Monday", "Monday"],
        "time": ["09:00", "09:10", "09:20"],
        "occupancy": [10, 20, 30],
    })
    curve = typical_curve(hist, "occupancy")
    # all three rows fall in slot 09:00//30 = 18 (09:00,09:10,09:20 all < 09:30)
    assert curve[("Monday", 18)] == 20, curve

    unit_df = pd.DataFrame({
        "weekday": ["Monday", "Monday"],
        "time": ["09:00", "09:10"],
        "occupancy": [25, 15],
    })
    residual_mean = summarize_unit(unit_df, "occupancy", "residual", "mean", curve)
    # residuals: 25-20=5, 15-20=-5 -> mean 0
    assert abs(residual_mean - 0.0) < 1e-9, residual_mean

    # -- compute_statistics --
    a = [10, 11, 12, 13]
    b = [1, 2, 2, 3]
    stats_result = compute_statistics(a, b)
    assert abs(stats_result["effect"] - 9.5) < 1e-9, stats_result["effect"]
    assert stats_result["ci_low"] > 0, stats_result
    assert stats_result["p_value"] < 0.05, stats_result["p_value"]

    # -- derive_verdict --
    assert derive_verdict(2, 5, "A higher") is True
    assert derive_verdict(2, 5, "A lower") is False
    assert derive_verdict(-5, -2, "A lower") is True
    assert derive_verdict(-1, 3, "A higher") is None

    print("ok")
