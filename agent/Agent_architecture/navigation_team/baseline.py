"""The baseline tool: the day's starting table of settings, in plain code.

Two sources, in order:
1. History. If the master record holds at least one finished day for this
   weekday (or three days of any kind), the baseline per two-hour stretch is
   the weighted median of each past day's median setting, weighted by how
   long guests stayed in that stretch. One odd day still cannot swing the
   plan (decisions rule 39). Deeper learning comes from the memory agent's
   verdicts.
0. Either way, the thermostat offset the record shows (sensor above setpoint,
   per stretch) is folded into the setpoint: a room fact, learnt from day one.
2. Cold start. With no history the seed room is a plausible starting point
   adjusted by the 24-hour forecast: a rainy day gets a touch warmer and
   brighter, a hot fair day a touch cooler with more air, evenings dimmer
   and quieter. These are the hand-written habits of a first week, stated
   in each block's reason so they can be questioned.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from blocks import crowd_category

from blocks import N_STRETCH, STRETCH  # the same 2-hour grid every module uses  # noqa: E402
SEED = {"light": 500.0, "temperature": 22.5, "volume": 50.0, "fan": 2}
MAX_DRIFT_FIX = 2.0  # largest learned thermostat offset the baseline will pre-correct, C
MAX_GLARE_FIX = 200.0  # largest learned daylight offset the baseline will pre-correct, lux
SETTING_COLS = {"light": "light_set", "temperature": "temperature_set", "volume": "volume", "fan": "fan"}
TOP_DAYS = 3  # per stretch, copy the settings of this many best past days (by how long guests stayed)
MIN_VOTE_OCC = 30.0  # occupant-minutes a past stretch needs before its settings count as evidence
MAX_STEP = {"light": 50.0, "temperature": 0.5, "volume": 2.0, "fan": 1}  # largest baseline move per lever per day
PROBE_STEP = 0.7  # C; every history day tries the room this much warmer or cooler than the baseline, alternating


def _stretches() -> List[Tuple[int, int]]:
    return [(i * STRETCH, min(840, (i + 1) * STRETCH)) for i in range(N_STRETCH)]


def seed_baseline(forecast24: Dict[str, Any]) -> List[Dict[str, Any]]:
    text = str(forecast24.get("forecast_text") or "").lower()
    hi = forecast24.get("temperature_high")
    rainy = any(w in text for w in ("rain", "shower", "thunder"))
    warm = hi is not None and hi >= 31 and not rainy
    hot = hi is not None and hi >= 33 and not rainy
    blocks = []
    for lo, hi_t in _stretches():
        s = dict(SEED)
        why = ["seed room"]
        if rainy:
            s["temperature"] += 0.5
            s["light"] += 50
            why.append("rain forecast: a touch warmer and brighter")
        if warm:
            # the room runs warmer than its setpoint the hotter it is outside (record: +0.5..1 C
            # at 27-31 C outside, +2..3 C at 33 C), so ask for less the hotter the forecast
            s["temperature"] -= 0.5
            why.append("warm fair day: a touch cooler")
        if hot:
            s["temperature"] -= 1.0
            s["fan"] = min(5, s["fan"] + 1)
            why.append("hot fair day: cooler still, with more air")
        if 240 <= lo < 360 or 600 <= lo < 720:
            s["fan"] = min(5, s["fan"] + 1)
            why.append("meal rush: more air for the crowd")
        if lo >= 600:
            s["light"] -= 100
            s["volume"] -= 3
            why.append("evening: dimmer and quieter")
        blocks.append({"from": lo, "to": hi_t, "settings": s, "reasons": "; ".join(why),
                       "assumes": {"weather": "rain" if rainy else ("fair" if not text or "fair" in text else "cloudy"),
                                   "crowd": "busy" if (240 <= lo < 360 or 600 <= lo < 720) else "moderate"}})
    return blocks


def _stretch_index(time_str: str) -> int:
    h, m = (int(x) for x in time_str.split(":"))
    return min(N_STRETCH - 1, max(0, ((h - 8) * 60 + m) // STRETCH))


def _optional(conn: sqlite3.Connection, expr: str, needs: Tuple[str, ...]) -> str:
    """``expr`` if the readings table has every column it needs, else NULL (older records)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(readings)")}
    return expr if all(c in cols for c in needs) else "NULL"


def _offsets_from(drifts: Dict[int, List[float]], cap: float = MAX_DRIFT_FIX, nd: int = 1,
                  two_sided: bool = True) -> Dict[int, float]:
    """Per stretch, how far above the setting the room actually ran. Temperature is two-sided (fans
    and open doors can hold the room below its setpoint); daylight only ever adds light."""
    out = {}
    for i, xs in drifts.items():
        if xs:
            out[i] = round(min(cap, max(-cap if two_sided else 0.0, sum(xs) / len(xs))), nd)
    return out


def learned_offsets(db_path: str) -> Tuple[Dict[int, float], Dict[int, float]]:
    """Calibration from the master record, per stretch over all days: how far the room ran
    above the thermostat setpoint, and above the light setting (daylight through the windows).

    Room facts, not preferences, so even one finished day is enough to use them.
    """
    p = Path(db_path)
    if not p.exists():
        return {}, {}
    try:
        with sqlite3.connect(f"file:{p}?mode=ro", uri=True) as conn:
            t_expr = _optional(conn, "temperature_sensor - temperature_set", ("temperature_sensor", "temperature_set"))
            l_expr = _optional(conn, "light_sensor - light_set", ("light_sensor", "light_set"))
            rows = conn.execute(f"SELECT time, {t_expr}, {l_expr} FROM readings").fetchall()
    except sqlite3.Error:
        return {}, {}
    drifts: Dict[int, List[float]] = {i: [] for i in range(N_STRETCH)}
    glare: Dict[int, List[float]] = {i: [] for i in range(N_STRETCH)}
    for time_str, d, g in rows:
        idx = _stretch_index(time_str)
        if d is not None:
            drifts[idx].append(float(d))
        if g is not None:
            glare[idx].append(float(g))
    return _offsets_from(drifts), _offsets_from(glare, MAX_GLARE_FIX, 0, two_sided=False)


def apply_offsets(blocks: List[Dict[str, Any]], offsets: Dict[int, float],
                  glare: Optional[Dict[int, float]] = None) -> List[Dict[str, Any]]:
    """Ask for settings that land where the plan intends, instead of finding out at 12:15.
    A block's temperature is the room temperature the plan wants; this turns it into the setpoint."""
    for i, b in enumerate(blocks):
        off = offsets.get(i, 0.0)
        if abs(off) >= 0.3:
            b["settings"]["temperature"] = round(b["settings"]["temperature"] - off, 1)
            b["reasons"] += (f"; setpoint {abs(off):g} C {'under' if off > 0 else 'over'} what we want, "
                             f"the room ran that much {'warm' if off > 0 else 'cool'} before")
        g = (glare or {}).get(i, 0.0)
        if g >= 30:
            b["settings"]["light"] = round(max(0.0, b["settings"]["light"] - g), 1)
            b["reasons"] += f"; lights {g:g} lux under what we ask for, daylight added that much before"
    return blocks


def _weighted_median(values: List[float], weights: List[float]) -> float:
    order = sorted(range(len(values)), key=lambda k: values[k])
    half = sum(weights) / 2.0
    acc = 0.0
    for k in order:
        acc += weights[k]
        if acc >= half:
            return values[k]
    return values[order[-1]]


def history_baseline(db_path: str, weekday: str, min_days: int = 1, capacity: int = 30,
                     thin_ok: bool = False, seed: Optional[List[Dict[str, Any]]] = None) -> Optional[List[Dict[str, Any]]]:
    """Per stretch, the settings of the past days where guests stayed longest.

    Each past (day, stretch) is one candidate: the room guests felt then (the
    sensor, not the setpoint) and its outcome, occupant-minutes per departure
    (the same departures signal the intraday agent watches). An intraday push
    is evidence like any other minute: it wins a vote only if guests stayed
    longer under it. A candidate needs ``MIN_VOTE_OCC``
    occupant-minutes to vote at all; a near-empty stretch says nothing about
    what guests liked. The best ``TOP_DAYS`` candidates by outcome vote, and the
    weighted median of their settings is the baseline, so the baseline climbs
    towards what worked instead of copying whatever ran yesterday. A stretch
    with no eligible candidate falls back to ``seed`` (the cold-start table).

    Same weekday first, otherwise all days once there are three, or two of
    different weekdays. The learned thermostat offset is folded in as well.
    """
    p = Path(db_path)
    if not p.exists():
        return None
    try:
        with sqlite3.connect(f"file:{p}?mode=ro", uri=True) as conn:
            cur = conn.execute("SELECT COUNT(DISTINCT date) FROM readings WHERE weekday = ?", (weekday,))
            n_weekday = cur.fetchone()[0]
            cur = conn.execute("SELECT COUNT(DISTINCT date), COUNT(DISTINCT weekday) FROM readings")
            n_all, n_weekdays = cur.fetchone()
            if n_weekday >= min_days:
                where, args, src = "WHERE weekday = ?", (weekday,), f"{n_weekday} past {weekday}(s)"
            elif n_all >= 3 or n_weekdays >= 2 or (thin_ok and n_all >= 1):
                # once the record spans two different weekdays it generalises to a third
                where, args, src = "", (), f"{n_all} past days (no {weekday} yet)"
            else:
                return None
            left_expr = _optional(conn, "left", ("left",))
            drift_expr = _optional(conn, "temperature_sensor - temperature_set", ("temperature_sensor", "temperature_set"))
            glare_expr = _optional(conn, "light_sensor - light_set", ("light_sensor", "light_set"))
            # the vote is on what guests felt (sensor), not what was asked for (setpoint): the setpoint
            # already carries yesterday's offset correction, and copying it then correcting again compounds
            felt_t = _optional(conn, "temperature_sensor", ("temperature_sensor",))
            felt_l = _optional(conn, "light_sensor", ("light_sensor",))
            rows = conn.execute(
                f"SELECT date, time, {', '.join(SETTING_COLS.values())}, occupancy, {left_expr}, {drift_expr}, {glare_expr}, "
                f"{felt_t}, {felt_l} FROM readings {where}", args).fetchall()
    except sqlite3.Error:
        return None
    if not rows:
        return None
    # (date, stretch) -> per-lever values, occupant-minutes, departures, drift
    cells: Dict[Tuple[str, int], Dict[str, Any]] = {}
    n_set = len(SETTING_COLS)
    for date, time_str, *vals in rows:
        idx = _stretch_index(time_str)
        c = cells.setdefault((date, idx), {**{k: [] for k in SETTING_COLS}, "occ": 0.0, "left": 0.0, "n": 0, "drift": [], "glare": []})
        occ, left, drift, glare, felt_t, felt_l = vals[n_set:n_set + 6]
        felt = {"temperature": felt_t, "light": felt_l}
        for lever, v in zip(SETTING_COLS, vals):
            v = felt[lever] if felt.get(lever) is not None else v
            if v is not None:
                c[lever].append(float(v))
        if occ is not None:
            c["occ"] += float(occ)
            c["n"] += 1
        if left is not None:
            c["left"] += float(left)
        if drift is not None:
            c["drift"].append(float(drift))
        if glare is not None:
            c["glare"].append(float(glare))
    blocks = []
    offsets = _offsets_from({i: [d for (dt, idx), c in cells.items() if idx == i for d in c["drift"]]
                             for i in range(N_STRETCH)})
    glare = _offsets_from({i: [g for (dt, idx), c in cells.items() if idx == i for g in c["glare"]]
                           for i in range(N_STRETCH)}, MAX_GLARE_FIX, 0, two_sided=False)
    for i, (lo, hi) in enumerate(_stretches()):
        votes = [c for (dt, idx), c in cells.items() if idx == i]
        occ_min = sum(c["occ"] for c in votes)
        n_min = sum(c["n"] for c in votes)
        frac = (occ_min / n_min) / capacity if n_min and capacity else None
        crowd = crowd_category(frac) if frac is not None else ("busy" if (240 <= lo < 360 or 600 <= lo < 720) else "moderate")
        # outcome: minutes a guest stayed on average in that stretch (occupant-minutes per departure)
        eligible = [c for c in votes if c["occ"] >= MIN_VOTE_OCC and all(c[k] for k in SETTING_COLS)]
        eligible.sort(key=lambda c: c["occ"] / (c["left"] + 1.0), reverse=True)
        best = eligible[:TOP_DAYS]
        if not best:
            s = dict((seed or seed_baseline({}))[i]["settings"])
            why = (f"baseline: seed room for this stretch, none of {len(votes)} past day(s) had "
                   f"{MIN_VOTE_OCC:g} occupant-minutes here to learn from")
        else:
            weights = [c["occ"] / (c["left"] + 1.0) for c in best]
            s = {}
            for lever in SETTING_COLS:
                meds = []
                for c in best:
                    xs = sorted(c[lever])
                    meds.append(xs[len(xs) // 2])
                val = _weighted_median(meds, weights)
                s[lever] = int(round(val)) if lever == "fan" else round(val, 1)
            stay = sum(weights) / len(weights)
            why = (f"baseline: the room of the {len(best)} of {len(eligible)} eligible stretch-days ({src}) "
                   f"where guests stayed longest (~{stay:.0f} min per guest)")
        if frac is not None:
            why += f"; the room averaged {frac:.0%} full then"
        blocks.append({"from": lo, "to": hi, "settings": s, "reasons": why,
                       "assumes": {"weather": "fair", "crowd": crowd}})
    return apply_offsets(blocks, offsets, glare)


def probe(blocks: List[Dict[str, Any]], day_index: int) -> List[Dict[str, Any]]:
    """One deliberate trial a day: the whole day runs ``PROBE_STEP`` warmer on even days and cooler on
    odd days. Past days then differ, so the outcome vote has something to choose between, and within a
    week each stretch has seen both sides. The probe is marked so tomorrow's drift cap ignores it."""
    delta = PROBE_STEP if day_index % 2 == 0 else -PROBE_STEP
    for b in blocks:
        b["settings"]["temperature"] = round(b["settings"]["temperature"] + delta, 1)
        b["probe"] = {"temperature": delta}
        b["reasons"] += f"; probe: {delta:+g} C today to learn whether {'warmer' if delta > 0 else 'cooler'} keeps guests longer"
    return blocks


def cap_drift(blocks: List[Dict[str, Any]], previous: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """The baseline moves at most ``MAX_STEP`` per lever per day from yesterday's baseline, so one
    day's evidence nudges the table and a week of evidence moves it; nothing jumps."""
    if not previous or len(previous) != len(blocks):
        return blocks
    for b, prev in zip(blocks, previous):
        ps = dict((prev or {}).get("settings") or {})
        for lever, d in ((prev or {}).get("probe") or {}).items():  # yesterday's probe is not yesterday's baseline
            if lever in ps:
                ps[lever] = ps[lever] - d
        held = []
        for lever, step in MAX_STEP.items():
            if lever not in ps or lever not in b["settings"]:
                continue
            want, was = b["settings"][lever], ps[lever]
            capped = max(was - step, min(was + step, want))
            if capped != want:
                b["settings"][lever] = int(round(capped)) if lever == "fan" else round(capped, 1)
                held.append(f"{lever} {want:g}->{capped:g}")
        if held:
            b["reasons"] += "; stepped from yesterday's baseline (" + ", ".join(held) + "), one day moves it only so far"
    return blocks


def _days_on_record(db_path: str) -> int:
    try:
        with sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True) as conn:
            return int(conn.execute("SELECT COUNT(DISTINCT date) FROM readings").fetchone()[0])
    except sqlite3.Error:
        return 0


def baseline_blocks(db_path: str, weekday: str, forecast24: Dict[str, Any],
                    capacity: int = 30, previous: Optional[List[Dict[str, Any]]] = None) -> Tuple[List[Dict[str, Any]], str]:
    """The day's starting table and where it came from. ``previous`` is yesterday's baseline, if
    on file: today's may step only ``MAX_STEP`` away from it."""
    seed = seed_baseline(forecast24)
    hist = history_baseline(db_path, weekday, capacity=capacity, seed=seed)
    if hist:
        return probe(cap_drift(hist, previous), _days_on_record(db_path)), "history"
    # too little history to copy settings from, but the room's thermostat offset is a fact we can use
    seed = apply_offsets(seed, *learned_offsets(db_path))
    thin = history_baseline(db_path, weekday, capacity=capacity, thin_ok=True, seed=seed)
    if thin:
        # one or two days of a different weekday: meet the record halfway rather than ignore it
        for b, h in zip(seed, thin):
            for lever, v in b["settings"].items():
                mid = (v + h["settings"][lever]) / 2.0
                b["settings"][lever] = int(round(mid)) if lever == "fan" else round(mid, 1)
            b["reasons"] += "; halfway to the best-stay settings of the few days on record (too thin to copy)"
    return cap_drift(seed, previous), "seed"
