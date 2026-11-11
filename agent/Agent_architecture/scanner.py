"""Deterministic (non-LLM) scanner over the readings history for statistical anomalies."""

import math
import sqlite3
import sys

SD_UNITS = 2.0


def _sample_sd(values, mean):
    n = len(values)
    if n < 2:
        return 0.0
    return math.sqrt(sum((x - mean) ** 2 for x in values) / (n - 1))


def _slot(time_str):
    """Floor 'HH:MM' to its 30-minute slot label ('HH:00' or 'HH:30')."""
    hour, minute = time_str.split(":")
    return f"{hour}:00" if int(minute) < 30 else f"{hour}:30"


def _load(db_path):
    """Read readings and build per-day totals, weekdays, and per-day-per-slot totals."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT date, weekday, time, entered FROM readings").fetchall()
    finally:
        conn.close()

    day_weekday = {}
    day_total = {}
    day_slot_total = {}
    for date, weekday, time_str, entered in rows:
        amount = entered or 0.0
        day_weekday[date] = weekday
        day_total[date] = day_total.get(date, 0.0) + amount
        slot = _slot(time_str)
        day_slot_total.setdefault(date, {})
        day_slot_total[date][slot] = day_slot_total[date].get(slot, 0.0) + amount

    return day_weekday, day_total, day_slot_total


def _check_weird_day(day_weekday, day_total):
    flags = []
    for date, total in day_total.items():
        weekday = day_weekday[date]
        others = [day_total[d] for d, w in day_weekday.items() if w == weekday and d != date]
        if len(others) < 3:
            continue
        mean = sum(others) / len(others)
        sd = _sample_sd(others, mean)
        if sd <= 0:
            continue
        units = (total - mean) / sd
        if abs(units) > SD_UNITS:
            flags.append({
                "check": "weird_day",
                "label": date,
                "value": total,
                "baseline": mean,
                "sd_units": units,
                "n": len(others),
                "text": f"{date} had {total:.1f} walk-ins, vs a usual {mean:.1f} for that weekday ({units:+.1f} SD, n={len(others)}).",
            })
    return flags


def _check_weekday_bias(day_weekday, day_total):
    flags = []
    all_totals = list(day_total.values())
    if not all_totals:
        return flags
    overall_mean = sum(all_totals) / len(all_totals)

    by_weekday = {}
    for date, weekday in day_weekday.items():
        by_weekday.setdefault(weekday, []).append(day_total[date])

    for weekday, totals in by_weekday.items():
        n = len(totals)
        if n < 3:
            continue
        mean = sum(totals) / n
        sd = _sample_sd(totals, mean)
        if sd <= 0:
            continue
        se = sd / math.sqrt(n)
        units = (mean - overall_mean) / se
        if abs(units) > SD_UNITS:
            flags.append({
                "check": "weekday_bias",
                "label": weekday,
                "value": mean,
                "baseline": overall_mean,
                "sd_units": units,
                "n": n,
                "text": f"{weekday} averages {mean:.1f} walk-ins vs an overall average of {overall_mean:.1f} ({units:+.1f} SD, n={n}).",
            })
    return flags


def _check_time_slot_bias(day_slot_total):
    flags = []
    slot_day_totals = {}
    all_slot_values = []
    for date, slots in day_slot_total.items():
        for slot, total in slots.items():
            slot_day_totals.setdefault(slot, []).append(total)
            all_slot_values.append(total)
    if not all_slot_values:
        return flags
    grand_mean = sum(all_slot_values) / len(all_slot_values)

    for slot, totals in slot_day_totals.items():
        n = len(totals)
        if n < 3:
            continue
        mean = sum(totals) / n
        sd = _sample_sd(totals, mean)
        if sd <= 0:
            continue
        se = sd / math.sqrt(n)
        units = (mean - grand_mean) / se
        if abs(units) > SD_UNITS:
            flags.append({
                "check": "time_slot_bias",
                "label": slot,
                "value": mean,
                "baseline": grand_mean,
                "sd_units": units,
                "n": n,
                "text": f"The {slot} slot averages {mean:.1f} walk-ins vs a grand average of {grand_mean:.1f} per slot ({units:+.1f} SD, n={n}).",
            })
    return flags


def _check_drift(day_total):
    flags = []
    dates_sorted = sorted(day_total.keys())
    if len(dates_sorted) <= 14:
        return flags
    later_dates = dates_sorted[-14:]
    earlier_dates = dates_sorted[:-14]
    if len(earlier_dates) < 7:
        return flags

    later = [day_total[d] for d in later_dates]
    earlier = [day_total[d] for d in earlier_dates]
    n1, n2 = len(later), len(earlier)
    mean1 = sum(later) / n1
    mean2 = sum(earlier) / n2
    sd1 = _sample_sd(later, mean1)
    sd2 = _sample_sd(earlier, mean2)
    se = math.sqrt((sd1 ** 2) / n1 + (sd2 ** 2) / n2)
    if se <= 0:
        return flags
    units = (mean1 - mean2) / se
    if abs(units) > SD_UNITS:
        flags.append({
            "check": "drift",
            "label": "last 14 days",
            "value": mean1,
            "baseline": mean2,
            "sd_units": units,
            "n": n1,
            "text": f"The last 14 days average {mean1:.1f} walk-ins vs {mean2:.1f} before that ({units:+.1f} SD, n={n1}).",
        })
    return flags


def scan(db_path: str) -> list[dict]:
    """Scan a readings db (opened read-only) for statistical flags. No LLM involved."""
    day_weekday, day_total, day_slot_total = _load(db_path)

    flags = []
    flags.extend(_check_weird_day(day_weekday, day_total))
    flags.extend(_check_weekday_bias(day_weekday, day_total))
    flags.extend(_check_time_slot_bias(day_slot_total))
    flags.extend(_check_drift(day_total))

    flags.sort(key=lambda f: abs(f["sd_units"]), reverse=True)
    return flags


def _build_test_db(path):
    """Build ~30 days of a flat, repeating pattern, plant one outlier day and one
    elevated weekday, and return the planted date/weekday for assertions."""
    import datetime

    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE readings (date TEXT, weekday TEXT, time TEXT, entered REAL)"
    )

    weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    start = datetime.date(2026, 1, 5)  # a Monday
    slots = [f"{h:02d}:{m:02d}" for h in range(9, 17) for m in (0, 30)]  # 16 slots
    base_per_slot = 100.0 / len(slots)

    outlier_date = None
    elevated_weekday = "Wed"
    for i in range(30):
        date = start + datetime.timedelta(days=i)
        date_str = date.isoformat()
        weekday = weekdays[date.weekday()]

        # deterministic small variation by slot index, same pattern every day
        day_total_target = 100.0
        if weekday == elevated_weekday:
            day_total_target += 40.0  # clearly elevated weekday
        if i == 15:
            day_total_target += 500.0  # extreme outlier day
            outlier_date = date_str

        # deterministic tiny day-to-day variation so same-weekday totals have a
        # small nonzero SD, without disturbing the planted anomalies
        day_total_target += 0.1 * (i % 5)

        per_slot = day_total_target / len(slots)
        for j, slot_time in enumerate(slots):
            # deterministic tiny variation, same shape every day, doesn't affect totals check
            variation = 0.01 * (j % 3)
            conn.execute(
                "INSERT INTO readings (date, weekday, time, entered) VALUES (?, ?, ?, ?)",
                (date_str, weekday, slot_time, per_slot + variation),
            )

    conn.commit()
    conn.close()
    return outlier_date, elevated_weekday


def _build_flat_db(path):
    """30 days, perfectly flat and identical, no planted anomalies."""
    import datetime

    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE readings (date TEXT, weekday TEXT, time TEXT, entered REAL)"
    )
    weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    start = datetime.date(2026, 1, 5)
    slots = [f"{h:02d}:{m:02d}" for h in range(9, 17) for m in (0, 30)]
    per_slot = 100.0 / len(slots)
    for i in range(30):
        date = start + datetime.timedelta(days=i)
        date_str = date.isoformat()
        weekday = weekdays[date.weekday()]
        for slot_time in slots:
            conn.execute(
                "INSERT INTO readings (date, weekday, time, entered) VALUES (?, ?, ?, ?)",
                (date_str, weekday, slot_time, per_slot),
            )
    conn.commit()
    conn.close()


def _self_test():
    import os
    import tempfile

    tmp_dir = tempfile.mkdtemp()

    # planted anomalies
    db_path = os.path.join(tmp_dir, "test.db")
    outlier_date, elevated_weekday = _build_test_db(db_path)
    flags = scan(db_path)

    weird_day_flags = [f for f in flags if f["check"] == "weird_day" and f["label"] == outlier_date]
    assert weird_day_flags, "expected a weird-day flag for the planted outlier date"

    weekday_bias_flags = [f for f in flags if f["check"] == "weekday_bias" and f["label"] == elevated_weekday]
    assert weekday_bias_flags, "expected a weekday-bias flag for the planted elevated weekday"

    # flat dataset: no weird-day or drift flags
    flat_path = os.path.join(tmp_dir, "flat.db")
    _build_flat_db(flat_path)
    flat_flags = scan(flat_path)
    assert not any(f["check"] == "weird_day" for f in flat_flags), "flat dataset should have no weird-day flags"
    assert not any(f["check"] == "drift" for f in flat_flags), "flat dataset should have no drift flags"

    print("ok")


if __name__ == "__main__":
    _self_test()

    if len(sys.argv) > 1:
        for flag in scan(sys.argv[1]):
            print(flag["text"])
