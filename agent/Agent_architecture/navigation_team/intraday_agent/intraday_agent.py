"""Intraday agent: the executor for the day.

Plain code pushes the plan's blocks minute by minute and, every check
interval, compares what the plan assumed with what is happening. Only a
discrepancy wakes the model:

- forecast:  the 2-hour forecast's category (fair / cloudy / rain) differs
             from the block's assumed weather
- crowd:     the room's fullness category (quiet / moderate / busy) differs
             from the block's assumed crowd, clear of the boundary
- co2:       CO2 over the 800 ppm comfort target (and again over the 1000 limit)
- temp_drift: the room more than 2 C from its setpoint for a quarter-hour
- light_drift: the light sensor more than 50 lux off the light setting for a quarter-hour (daylight)
- leaving:   departures per occupant-minute up 50% on the hour before
- empty:     no one in the room for 15 min after 9pm -> energy mode, plain code, no model
- owner:     an owner message about later today
- note:      an owner's note for today that arrived after the morning plan (a new
             calendar event, or a note filed while the cafe is open) whose window is
             still ahead

Each kind fires once per block. On a discrepancy the agent asks the memory
agent, then rewrites the rest of the day (``IntradayAgent.replan``, one
model call) or, without a model, applies a small deterministic adjustment
(``heuristic_replan``). Every push is logged with its reason so the master
record carries the intent.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, TypedDict

import pandas as pd
from pydantic import BaseModel

_DIR = Path(__file__).parent


def _prompt(name: str) -> str:
    return (_DIR / f"{name}.md").read_text(encoding="utf-8")


_NAV_TEAM_DIR = str(Path(__file__).resolve().parent.parent)
if _NAV_TEAM_DIR not in sys.path:
    sys.path.insert(0, _NAV_TEAM_DIR)

from blocks import (  # noqa: E402
    DAY_MINUTES,
    SettingsBlock,
    _block_to_dict,
    _validate_coverage,
    block_at,
    clean_settings,
    clock,
    enforce_bounds,
    crowd_category,
    settings_at,
    splice,
    validate_assumes,
    weather_category,
    CHANGE_CAP,
    cap_changes,
    describe_where,
    inherit_origin,
    stamp_origin,
    stretch_bounds,
    stretch_of,
    where_now,
)
from questions import note_question  # noqa: E402

CO2_TARGET = 800.0    # NEA / SS 554 comfort target
CO2_FLOOR = 1000.0    # the limit
TEMP_DRIFT = 2.0      # room more than this from the setpoint for a quarter-hour: the thermostat is losing
LEAVE_RISE = 1.8      # departures per occupant-minute, last hour vs the hour before
LEAVE_MIN_OCC = 300.0 # occupant-minutes (5 people for an hour) before a leave-rate verdict counts
ENERGY_FROM = 780     # 9pm: an empty room goes to energy mode, no model needed
WINDOW = 15
CROWD_MARGIN = 0.12   # how far past a category boundary the room must be before "crowd" fires
WARMUP_MIN = 30       # no crowd verdicts in the first half hour: the room is always empty at open
TEMP_WARMUP = 60      # the room starts at outside temperature and takes an hour to settle
PERSIST = 2           # a crowd discrepancy must hold for this many consecutive checks before it fires
COOLDOWN = {"forecast": 60, "crowd": 120, "co2:target": 840, "co2:limit": 60, "temp_drift": 240, "leaving": 180, "empty": 840}
LIGHT_DRIFT = 50.0    # lux the room reads above (daylight) or below its light setting for a quarter-hour
LIGHT_WARMUP = 30     # the first half hour: let the opening ramp settle before judging the light
LIGHT_FIX = 1.0       # fraction of the measured excess taken off the setting for the rest of the day
DAYLIGHT_TO = 600     # blocks starting at or after this minute are left alone by the light correction
COOLDOWN["light_drift"] = 240


# ---------------------------------------------------------------------------
# Structured-output models
# ---------------------------------------------------------------------------

class ReplanBlocks(BaseModel):
    settings_blocks: list[SettingsBlock]


class ReplanPatch(BaseModel):
    """Only the windows that change. Everything not returned is kept as it is."""
    changes: list[SettingsBlock]
    kept: str = ""  # one line on what stays and why


def apply_patch(remaining: List[dict], changes: List[dict], now: int) -> List[dict]:
    """Overlay changed windows on the remaining blocks; unchanged stretches survive untouched."""
    out = [dict(b) for b in remaining]
    for c in sorted(changes, key=lambda b: b["from"]):
        lo, hi = max(now, int(c["from"])), min(DAY_MINUTES, int(c["to"]))
        if hi <= lo:
            continue
        pieces = []
        for b in out:
            if b["to"] <= lo or b["from"] >= hi:
                pieces.append(b)
                continue
            if b["from"] < lo:
                pieces.append({**b, "to": lo})
            if b["to"] > hi:
                pieces.append({**b, "from": hi})
        cut = block_at(out, lo) if out else {}
        pieces.append(inherit_origin({**c, "from": lo, "to": hi}, cut))  # keeps the cut block's birth window
        out = sorted(pieces, key=lambda b: b["from"])
    return out


# ---------------------------------------------------------------------------
# Observing the room (plain code)
# ---------------------------------------------------------------------------

def snapshot(day_df: pd.DataFrame, t: int, weather: Dict[str, Any], capacity: int) -> Dict[str, Any]:
    """The last quarter-hour summarised. Raw rows never reach a prompt."""
    tail = day_df.tail(WINDOW)
    last5 = day_df.tail(5)

    def mean(frame, col):
        s = pd.to_numeric(frame[col], errors="coerce").dropna() if col in frame else pd.Series(dtype=float)
        return float(s.mean()) if len(s) else None

    occ = mean(tail, "occupancy") or 0.0
    snap = {
        "t": t, "clock": clock(t), "capacity": capacity,
        "occupancy_mean_15": round(occ, 1), "occupancy_frac": round(occ / capacity, 3) if capacity else 0.0,
        "entered_15": int(pd.to_numeric(tail.get("entered"), errors="coerce").fillna(0).sum()) if "entered" in tail else 0,
        "left_15": int(pd.to_numeric(tail.get("left"), errors="coerce").fillna(0).sum()) if "left" in tail else 0,
        "temperature": mean(last5, "temperature_sensor"), "humidity": mean(last5, "humidity"),
        "co2": mean(tail, "co2"), "noise": mean(last5, "noise"), "light": mean(last5, "light_sensor"),
        "outside_temperature": mean(last5, "outside_temperature"),
        "settings": {},
        "forecast_2h": "", "forecast_2h_issued": None, "forecast_24h": "",
    }
    if len(day_df):
        row = day_df.iloc[-1]
        snap["settings"] = {"light": float(row["light_set"]), "temperature": float(row["temperature_set"]),
                            "volume": float(row["volume"]), "fan": int(row["fan"])}
    f2 = weather.get("forecast_2h")
    if f2 is not None and len(f2):
        last = f2.iloc[-1]
        snap["forecast_2h"] = str(last.get("forecast_text", ""))
        snap["forecast_2h_issued"] = int(last["issued_at"]) if "issued_at" in f2.columns and pd.notna(last["issued_at"]) else None
    f24 = weather.get("forecast_24h")
    if f24 is not None and len(f24):
        snap["forecast_24h"] = str(f24.iloc[-1].get("forecast_text", ""))
    snap["crowd"] = crowd_category(snap["occupancy_frac"])
    snap["weather"] = weather_category(snap["forecast_2h"]) if snap["forecast_2h"] else None
    # thermostat drift: the quarter-hour mean room temperature against the setpoint in force
    temp15 = mean(tail, "temperature_sensor")
    snap["temp_drift"] = round(temp15 - snap["settings"]["temperature"], 2) if temp15 is not None and snap["settings"] else None
    # daylight: the quarter-hour mean of what the light sensor read above the setting in force each minute
    if "light_sensor" in tail and "light_set" in tail and len(tail):
        ex = (pd.to_numeric(tail["light_sensor"], errors="coerce") - pd.to_numeric(tail["light_set"], errors="coerce")).dropna()
        snap["light_drift"] = round(float(ex.mean()), 1) if len(ex) else None
    else:
        snap["light_drift"] = None
    # leave rate: departures per occupant-minute, this hour vs the hour before
    def leave_rate(frame):
        if frame is None or not len(frame) or "left" not in frame:
            return None, 0.0
        occ_min = float(pd.to_numeric(frame["occupancy"], errors="coerce").fillna(0).sum())
        left = float(pd.to_numeric(frame["left"], errors="coerce").fillna(0).sum())
        return (left / occ_min if occ_min > 0 else None), occ_min
    last60 = day_df.tail(60)
    prev60 = day_df.iloc[-120:-60] if len(day_df) >= 120 else None
    snap["leave_rate_60"], snap["occ_min_60"] = leave_rate(last60)
    snap["leave_rate_prev"], snap["occ_min_prev"] = leave_rate(prev60)
    return snap


ONCE_PER_STRETCH = ("crowd", "forecast", "temp_drift", "light_drift")  # these fire at most once per 2-hour stretch


def _cool(fired: Set[tuple], name: str, t: int, kind: str) -> bool:
    """True if this discrepancy may fire now: not fired within its cooldown, and (for the kinds above)
    not already fired in this stretch. Records the firing."""
    for entry in fired:
        if entry[0] != name:
            continue
        if t - entry[1] < COOLDOWN[kind]:
            return False
        if kind in ONCE_PER_STRETCH and stretch_of(entry[1]) == stretch_of(t):
            return False
    fired.add((name, t))
    return True


# ---------------------------------------------------------------------------
# The previous stretch: what was planned, what ran, what the room did, what fired
# ---------------------------------------------------------------------------

def previous_stretch(day_df: pd.DataFrame, blocks: List[dict], pushes: List[dict], events: List[dict],
                     t: int) -> Optional[Dict[str, Any]]:
    """The last completed 2-hour stretch, or None in the first one."""
    i = stretch_of(t)
    if i == 0:
        return None
    lo, hi = stretch_bounds(i - 1)
    frame = day_df.iloc[lo:hi] if day_df is not None and len(day_df) > lo else pd.DataFrame()

    def mean(col, nd=1):
        if col not in frame or not len(frame):
            return None
        s = pd.to_numeric(frame[col], errors="coerce").dropna()
        return round(float(s.mean()), nd) if len(s) else None

    def total(col):
        return int(pd.to_numeric(frame[col], errors="coerce").fillna(0).sum()) if col in frame and len(frame) else 0

    applied = {"light": mean("light_set", 0), "temperature": mean("temperature_set"), "volume": mean("volume"), "fan": mean("fan")}
    return {
        "stretch": i - 1, "from": lo, "to": hi, "window": f"{clock(lo)}-{clock(hi)}",
        "planned": [{"from": b["from"], "to": b["to"], "settings": clean_settings(b["settings"]),
                     "reasons": str(b.get("reasons", ""))[:90]} for b in blocks if b["from"] < hi and b["to"] > lo],
        "applied": applied if all(v is not None for v in applied.values()) else None,
        "room": {"temperature": mean("temperature_sensor"), "light": mean("light_sensor", 0), "co2": mean("co2", 0),
                 "noise": mean("noise"), "occupancy": mean("occupancy")},
        "entered": total("entered"), "left": total("left"),
        "discrepancies": [{"t": e["t"], "clock": clock(e["t"]), "kind": e["kind"], "detail": str(e.get("detail", ""))[:100]}
                          for e in events if lo <= e.get("t", -1) < hi],
        "pushes": [{"t": p["t"], "clock": clock(p["t"]), "mode": p.get("mode", ""), "note": str(p.get("note", ""))[:100]}
                   for p in pushes if lo <= p.get("t", -1) < hi],
    }


def describe_stretch(ps: Optional[Dict[str, Any]]) -> str:
    """The previous stretch in plain words, for the prompt and the log."""
    if not ps:
        return "none yet: this is the first stretch of the day."
    def fmt(s):
        return (f"light {s['light']:g} lux, {s['temperature']:g} C, {s['volume']:g} dB, fan {s['fan']:g}"
                if s and all(v is not None for v in s.values()) else "unknown")
    lines = [f"{ps['window']} (the previous stretch):"]
    lines.append("  planned: " + "; ".join(f"{clock(b['from'])}-{clock(b['to'])} {fmt(b['settings'])} ({b['reasons']})" for b in ps["planned"]))
    lines.append("  actually ran (mean): " + fmt(ps["applied"]))
    r = ps["room"]
    room = ", ".join(f"{k} {v:g}" for k, v in (("temperature", r["temperature"]), ("light", r["light"]), ("co2", r["co2"]),
                                                 ("noise", r["noise"]), ("seated", r["occupancy"])) if v is not None)
    lines.append(f"  the room (mean): {room or 'unknown'}; {ps['entered']} came in, {ps['left']} left")
    lines.append("  discrepancies: " + ("; ".join(f"{d['clock']} {d['kind']}: {d['detail']}" for d in ps["discrepancies"]) or "none"))
    lines.append("  pushes: " + ("; ".join(f"{p['clock']} ({p['mode']}) {p['note']}" for p in ps["pushes"]) or "none"))
    return "\n".join(lines)


def _light_intent_offset(reasons: str) -> float:
    """How far the room level the plan intends sits above the block's light setting: the baseline's
    learned daylight offset ("lights N lux under what we ask for") minus every light-drift step
    already applied intraday. The reasons string is the plan's carrier of intent, so it is read here."""
    learned = sum(float(x) for x in re.findall(r"lights (\d+(?:\.\d+)?) lux under what we ask for", reasons or ""))
    applied = sum(float(x) for x in re.findall(r"light drift \(([+-]\d+) lux", reasons or ""))
    return learned - applied


def check(snap: Dict[str, Any], block: Dict[str, Any], fired: Set[tuple]) -> List[Dict[str, Any]]:
    """Discrepancies between the block's premise and the room. Each kind has a cooldown, so a
    push (which starts a new block at "now") cannot make the same discrepancy fire again."""
    events: List[Dict[str, Any]] = []
    assumes = block.get("assumes") or {}
    t = snap["t"]

    if snap.get("weather") and assumes.get("weather") and snap["weather"] != assumes["weather"]:
        if _cool(fired, f"forecast:{snap['weather']}", t, "forecast"):
            events.append({"kind": "forecast", "t": snap["t"],
                           "detail": f"the plan assumed {assumes['weather']} for this block; the 2-hour forecast "
                                     f"now says '{snap['forecast_2h']}' ({snap['weather']})"})

    assumed_crowd = assumes.get("crowd")
    if assumed_crowd and snap["crowd"] != assumed_crowd and snap["t"] >= WARMUP_MIN:
        frac = snap["occupancy_frac"]
        clear = ((snap["crowd"] == "quiet" and frac < 0.25 - CROWD_MARGIN)
                 or (snap["crowd"] == "busy" and frac > 0.6 + CROWD_MARGIN)
                 or (snap["crowd"] == "moderate" and 0.25 + CROWD_MARGIN < frac < 0.6 - CROWD_MARGIN))
        seen = (f"crowd-seen:{snap['crowd']}", t // WINDOW - 1)  # the previous check saw it too
        if clear:
            if seen in fired or PERSIST <= 1:
                if _cool(fired, f"crowd:{snap['crowd']}", t, "crowd"):
                    events.append({"kind": "crowd", "t": t,
                                   "detail": f"the plan assumed a {assumed_crowd} room for this block; it is {snap['crowd']} "
                                             f"({snap['occupancy_mean_15']:.0f} of {snap['capacity']} seats over the last 15 min, "
                                             f"second check in a row)"})
            else:
                fired.add((f"crowd-seen:{snap['crowd']}", t // WINDOW))

    fan_maxed = (snap.get("settings") or {}).get("fan", 0) >= 5
    if snap.get("co2") is not None and snap["co2"] > CO2_TARGET and not fan_maxed:  # fan at 5: nothing left to do
        level = "limit" if snap["co2"] > CO2_FLOOR else "target"
        if _cool(fired, f"co2:{level}", t, f"co2:{level}"):
            events.append({"kind": "co2", "t": t,
                           "detail": f"CO2 has averaged {snap['co2']:.0f} ppm over the last 15 min, above the "
                                     f"{'1000 ppm limit' if level == 'limit' else '800 ppm comfort target'}"})

    drift = snap.get("temp_drift")
    if drift is not None and abs(drift) > TEMP_DRIFT and t >= TEMP_WARMUP:
        if _cool(fired, "temp_drift", t, "temp_drift"):
            events.append({"kind": "temp_drift", "t": t,
                           "detail": f"the room has sat {abs(drift):.1f} C {'above' if drift > 0 else 'below'} the "
                                     f"{snap['settings'].get('temperature', 0):g} C setpoint for the last 15 min"})

    lr, prev = snap.get("leave_rate_60"), snap.get("leave_rate_prev")
    if (lr is not None and prev and prev > 0 and lr > LEAVE_RISE * prev
            and snap.get("occ_min_prev", 0) >= LEAVE_MIN_OCC and snap.get("occ_min_60", 0) >= LEAVE_MIN_OCC):
        if _cool(fired, "leaving", t, "leaving"):
            events.append({"kind": "leaving", "t": t,
                           "detail": f"guests are leaving faster: {lr:.3f} departures per occupant-minute this hour "
                                     f"vs {prev:.3f} the hour before; something in the room is not working"})

    ldrift = snap.get("light_drift")  # sensor above the setting in force: daylight through the windows
    if ldrift is not None and t >= LIGHT_WARMUP:
        gap = ldrift - _light_intent_offset(block.get("reasons") or "")  # sensor against the room level the plan intends
        if abs(gap) > LIGHT_DRIFT and _cool(fired, "light_drift", t, "light_drift"):
            events.append({"kind": "light_drift", "t": t,
                           "detail": f"the room has read {abs(gap):.0f} lux {'above' if gap > 0 else 'below'} the light the "
                                     f"plan intended for the last 15 min (daylight adds {ldrift:.0f} lux to the "
                                     f"{snap['settings'].get('light', 0):g} lux setting)"})

    if t >= ENERGY_FROM and snap.get("occupancy_mean_15", 0) == 0 and _cool(fired, "empty", t, "empty"):
        events.append({"kind": "empty", "t": t,
                       "detail": "the room has been empty for 15 min after 9pm: energy mode (dim, quiet, fan off)"})
    return events


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class IntradayAgent:
    """Rewrites the rest of the day on a discrepancy; one model call."""

    def __init__(self, llm):
        self.llm = llm

    def replan(self, events: List[dict], now: int, current_blocks: List[dict], note_blocks: List[dict],
               memory_answers: Optional[List[dict]] = None, snap: Optional[dict] = None,
               capacity: int = 30) -> List[dict]:
        structured_llm = self.llm.with_structured_output(ReplanPatch)
        # Only the rest of the day travels in the prompt, and only the changed windows come back.
        remaining = [b if b["from"] >= now else {**b, "from": now} for b in current_blocks if b["to"] > now]
        past = block_at(current_blocks, max(0, now - 1))
        trimmed_snap = {k: v for k, v in (snap or {}).items()
                        if k in ("clock", "occupancy_mean_15", "occupancy_frac", "crowd", "entered_15", "left_15",
                                 "temperature", "humidity", "co2", "noise", "light", "outside_temperature",
                                 "forecast_2h", "forecast_24h", "weather")}
        trimmed_snap["settings_now"] = (snap or {}).get("settings")
        trimmed_snap["settings_before_now"] = past.get("settings")
        where = (snap or {}).get("where") or where_now(current_blocks, now)
        prompt = _prompt("replan").format(
            capacity=capacity, now=now, clock=clock(now),
            where=describe_where(where),
            previous_stretch=describe_stretch((snap or {}).get("previous_stretch")),
            cap=", ".join(f"{k} {v:g}" for k, v in CHANGE_CAP.items()),
            events="\n".join(f"- {e['kind']}: {e['detail']}" for e in events),
            snapshot=json.dumps(trimmed_snap, default=str),
            memory_answers=json.dumps([{"question": a["question"], "answer": a["answer"]} for a in (memory_answers or [])]),
            current_blocks=json.dumps(remaining),
            note_blocks=json.dumps([{**{k: v for k, v in n.items() if k in ("from", "to", "kind", "fact", "settings", "reason")},
                                     "window": (f"{clock(n['from'])}-{clock(n['to'])}"
                                                if n.get("from") is not None and n.get("to") is not None else "all day")}
                                    for n in note_blocks]),
        )
        last_error: Optional[Exception] = None
        for attempt in range(2):
            ask = prompt if attempt == 0 else (
                prompt + f"\n\nYour previous answer was rejected: {last_error}. Fix exactly that and answer again.")
            try:
                result: ReplanPatch = structured_llm.invoke(ask)
            except Exception as exc:  # a malformed answer gets one more try with the error shown
                last_error = exc
                continue
            changes = [_block_to_dict(b) for b in result.changes]
            if not changes:
                return []  # the model looked and kept the plan
            for b in changes:
                b["settings"] = clean_settings(b["settings"])
            rest = apply_patch(remaining, changes, now)
            try:
                _validate_coverage(rest, now, DAY_MINUTES)
            except ValueError as exc:
                last_error = exc
                continue
            validate_assumes(rest)
            enforce_bounds(rest)
            self.last_patch = {"changed": len(changes), "kept": result.kept}
            return rest
        raise ValueError(f"replan rejected twice: {str(last_error)[:200]}")


def _hour_ago_settings(current_blocks: List[dict], now: int) -> Optional[dict]:
    """Settings in force an hour ago (the hour with the lower leave rate); None if the day is too young."""
    if now < 60 or not current_blocks:
        return None
    return clean_settings(block_at(current_blocks, now - 60)["settings"])


LEAVE_DRIFT = 0.5  # room this far from its own setpoint when guests leave faster: the setpoint is the suspect
LEAVE_STEP = 2.0   # how far to move the setpoint then
LEAVE_HORIZON = 120  # minutes the leaving correction lives: a guess about this stretch, not a fact about the day


def _react_to_leaving(rest: List[dict], current_blocks: List[dict], now: int, snap: Optional[dict],
                      handled: Set[str]) -> str:
    """Guests are leaving faster than the hour before. Move the one lever the sensors point at for
    the rest of the day (room off its setpoint, or CO2 over target when no co2 event is already on
    it). If nothing stands out, a change made since that hour is the suspect: undo it for this block."""
    b = rest[0]
    snap = snap or {}
    drift = snap.get("temp_drift")
    # the room is not working and the plan for later today was written on the same premise: carry the correction forward
    if drift is not None and abs(drift) > LEAVE_DRIFT and "temp_drift" not in handled:
        # a guess, so it lives for this stretch only; the day's record then says whether guests stayed longer
        for later in rest:
            if later["from"] >= now + LEAVE_HORIZON:
                break
            later["settings"]["temperature"] += -LEAVE_STEP if drift > 0 else LEAVE_STEP
            later["reasons"] = f"heuristic after leaving, room {abs(drift):.1f} C {'above' if drift > 0 else 'below'} setpoint: " + later["reasons"]
        return f"leaving -> setpoint {'-' if drift > 0 else '+'}{LEAVE_STEP:g} for the next {LEAVE_HORIZON} min"
    if snap.get("co2") is not None and snap["co2"] > CO2_TARGET and "co2" not in handled:
        for later in rest:
            later["settings"]["fan"] = min(5, later["settings"]["fan"] + 1)
            later["reasons"] = "heuristic after leaving, CO2 over target: fan up one: " + later["reasons"]
        return "leaving -> fan +1 for the rest of the day"
    before = _hour_ago_settings(current_blocks, now)  # no lever stands out: a recent change is the suspect
    if before and before != clean_settings(b["settings"]):
        b["settings"] = dict(before)
        b["reasons"] = "heuristic after leaving: back to the hour-before settings, when guests stayed: " + b["reasons"]
        return "leaving -> undo to hour-before settings"
    return "leaving: no lever stands out and nothing to undo, kept the plan"


def heuristic_replan(events: List[dict], now: int, current_blocks: List[dict],
                     snap: Optional[dict] = None) -> Tuple[List[dict], str]:
    """Small deterministic adjustments for the rest of the day. Returns (rest blocks, note)."""
    rest = []
    for b in current_blocks:
        if b["to"] <= now:
            continue
        nb = dict(b, settings=clean_settings(b["settings"]), assumes=dict(b.get("assumes") or {}))
        nb["from"] = max(now, b["from"])
        rest.append(nb)
    if not rest:
        return [], "nothing left to adjust"
    notes = []
    for e in events:
        kind = e["kind"]
        if kind == "forecast":
            cat = e["detail"].split("(")[-1].rstrip(")").strip()
            for b in rest:
                if b["assumes"].get("weather") == cat:
                    continue
                s = b["settings"]
                # setpoint stays: outside stays 28-35 C rain or shine here and the room already runs warm
                if cat == "rain":
                    s["light"] = min(1500.0, s["light"] + 50)
                elif cat == "fair":
                    s["light"] = max(0.0, s["light"] - 50)
                b["assumes"]["weather"] = cat
                b["reasons"] = f"heuristic after forecast change to {cat}: " + b["reasons"]
            notes.append(f"forecast -> {cat}")
        elif kind == "crowd":
            cat = "busy" if "is busy" in e["detail"] else ("quiet" if "is quiet" in e["detail"] else "moderate")
            for b in rest:  # the day is running {cat}; assume that until the room says otherwise
                if b["assumes"].get("crowd") != cat:  # blocks already planned for it keep their settings
                    s = b["settings"]
                    if cat == "busy":
                        s["volume"] = max(40.0, s["volume"] - 2)
                        s["temperature"] = max(21.0, s["temperature"] - 0.5)  # a full room runs about a degree warmer; cool it, not the fan
                    elif cat == "quiet":
                        s["fan"] = max(0, s["fan"] - 1)
                    b["reasons"] = f"heuristic after crowd turned {cat}: " + b["reasons"]
                b["assumes"]["crowd"] = cat
            notes.append(f"crowd -> {cat}")
        elif kind == "co2":
            b = rest[0]
            b["settings"]["fan"] = min(5, b["settings"]["fan"] + 1)
            b["reasons"] = "heuristic: fan up one for CO2 above target: " + b["reasons"]
            notes.append("co2 -> fan +1")
        elif kind == "temp_drift":
            above = "above" in e["detail"]
            step = -0.5 if above else 0.5
            for b in rest:  # the thermostat is losing all day, not just this block
                b["settings"]["temperature"] += step
                if above and b["settings"]["fan"] > 1 and b is rest[0]:
                    b["settings"]["fan"] -= 1  # the fan warms the room; ease it
                b["reasons"] = "heuristic after thermostat drift: " + b["reasons"]
            notes.append("temp drift -> setpoint " + ("-0.5" if above else "+0.5"))
        elif kind == "leaving":
            notes.append(_react_to_leaving(rest, current_blocks, now, snap, {x["kind"] for x in events}))
        elif kind == "light_drift":
            m = re.search(r"daylight adds (-?\d+) lux", e["detail"])
            daylight = float(m.group(1)) if m else 0.0
            for b in rest:  # the windows add the same daylight to every setting until dusk, not just this block
                if b["from"] >= DAYLIGHT_TO:
                    continue
                # set the lights so the room lands where the plan intends: intended level minus the daylight
                step = round(_light_intent_offset(b["reasons"]) - LIGHT_FIX * daylight)
                if abs(step) < 1:
                    continue
                b["settings"]["light"] = max(0.0, min(1500.0, b["settings"]["light"] + step))
                b["reasons"] = f"heuristic after light drift ({step:+.0f} lux, daylight adds {daylight:.0f}): " + b["reasons"]
            notes.append(f"light drift -> lights {LIGHT_FIX * daylight:.0f} lux under what the plan intends"
                         + (f" until {clock(DAYLIGHT_TO)}" if DAYLIGHT_TO < DAY_MINUTES else ""))
        elif kind == "empty":
            for b in rest:
                b["settings"]["light"] = max(150.0, b["settings"]["light"] - 150)
                b["settings"]["volume"] = 40.0
                b["settings"]["fan"] = 0
                b["assumes"]["crowd"] = "quiet"
                b["reasons"] = "energy mode, room empty after 9pm: " + b["reasons"]
            notes.append("empty -> energy mode")
        elif kind == "note":
            n = e.get("note") or {}
            lo = max(now, int(n["from"])) if n.get("from") is not None else now
            hi = int(n["to"]) if n.get("to") is not None else DAY_MINUTES
            if n.get("kind") == "override" and n.get("settings") and hi > lo:
                # the owner's exact settings for the window, the rest of the block's levers kept
                base = (block_at(rest, lo) or rest[0])["settings"]
                full = clean_settings({**base, **n["settings"]})
                rest = apply_patch(rest, [{"from": lo, "to": hi, "settings": full,
                                           "reasons": f"owner's override: {n.get('reason') or n.get('text', '')}",
                                           "assumes": dict((block_at(rest, lo) or rest[0]).get("assumes") or {})}], now)
                notes.append(f"note -> override {clock(lo)}-{clock(hi)} {n['settings']}")
            else:
                # a fact note: no settings change without a model; the plan records the premise so later
                # checks (crowd, leaving) read against it and the master record carries the intent
                for b in rest:
                    if b["from"] < hi and b["to"] > lo:
                        b["reasons"] = f"owner's note for {clock(lo)}-{clock(hi)}: {n.get('text', '')}; " + b["reasons"]
                        if any(w in str(n.get("text", "")).lower() for w in ("group", "party", "booking", "crowd", "event")):
                            b["assumes"]["crowd"] = "busy"
                notes.append(f"note -> premise recorded for {clock(lo)}-{clock(hi)}")
        else:
            notes.append(f"{kind}: no heuristic, kept the plan")
    enforce_bounds(rest)
    return rest, "; ".join(notes)


# ---------------------------------------------------------------------------
# One check as a graph
# ---------------------------------------------------------------------------

class CheckState(TypedDict, total=False):
    t: int
    snapshot: dict
    block: dict
    events: List[dict]
    memory_answers: List[dict]
    rest: List[dict]
    mode: str
    pushed: bool
    note: str


def same_schedule(blocks: List[dict], rest: List[dict], now: int) -> bool:
    """True if ``rest`` asks for exactly the settings ``blocks`` already schedule from ``now`` to close.
    A replan that only rewrote a block's premise (assumes) or moved a setting into a bound it already
    sits at changes nothing the room would feel, so there is nothing to push."""
    edges = sorted({now, DAY_MINUTES} | {b["from"] for b in blocks + rest if now < b["from"] < DAY_MINUTES})
    for t in edges[:-1]:
        if settings_at(rest, t) != settings_at(blocks, t):
            return False
    return True


def build_intraday_graph(ctx):
    """ctx provides: capacity, date, blocks (current day plan, mutable), note_blocks, fired (set),
    day_df() -> DataFrame, weather() -> dict, memory_door, intraday_agent (or None),
    model_calls_left() -> int, budget_ok() -> bool, push(rest, t, note, mode), owner_messages(t) -> list[dict], log(msg)."""
    from langgraph.graph import END, START, StateGraph

    def observe(state: CheckState) -> CheckState:
        t = state["t"]
        day_df = ctx.day_df()
        snap = snapshot(day_df, t, ctx.weather(), ctx.capacity)
        # where "now" is (stretch, block and how it came to exist) and what the previous stretch did
        snap["where"] = where_now(ctx.blocks, t)
        snap["previous_stretch"] = previous_stretch(day_df, ctx.blocks, getattr(ctx, "pushes", []),
                                                    getattr(ctx, "day_events", []), t)
        return {"snapshot": snap, "block": block_at(ctx.blocks, t)}

    def detect(state: CheckState) -> CheckState:
        t = state["t"]
        events = check(state["snapshot"], state["block"], ctx.fired)
        for m in ctx.owner_messages(t):
            events.append({"kind": "owner", "t": t, "detail": f"owner says: {m}"})
        # notes for today that were not there in the morning: a calendar event added during the day,
        # or a note filed while the cafe is open. Only a note whose window is still ahead matters now.
        for n in (ctx.fresh_notes(t) if hasattr(ctx, "fresh_notes") else []):
            if n.get("to") is not None and n["to"] <= t:
                ctx.log(f"{clock(t)} late note ignored, its window has passed: {n.get('text', '')}")
                continue
            window = (f" ({clock(n['from'])}-{clock(n['to'])})" if n.get("from") is not None and n.get("to") is not None
                      else " (rest of the day)")
            events.append({"kind": "note", "t": t, "note": n,
                           "detail": f"a new owner's note for today{window}: {n.get('text', '')}"})
            ctx.note_blocks.append({"from": n.get("from"), "to": n.get("to"), "kind": n.get("kind", "fact"),
                                    "fact": n.get("text", ""), "settings": n.get("settings"),
                                    "reason": n.get("reason", ""), "source": n.get("source", "owner")})
        if events:
            ctx.log(f"{clock(state['t'])} discrepancy: " + "; ".join(e["detail"] for e in events))
            if hasattr(ctx, "day_events"):
                ctx.day_events.extend({"t": t, "kind": e["kind"], "detail": e["detail"]} for e in events)
        return {"events": events, "pushed": False}

    def route(state: CheckState) -> str:
        return "ask_memory" if state["events"] else "hold"

    def hold(state: CheckState) -> CheckState:
        return {"mode": "hold"}

    def ask_memory(state: CheckState) -> CheckState:
        questions = []
        for e in state["events"]:
            if e["kind"] == "forecast":
                questions.append(f"what should change in this room when the weather turns to {state['snapshot'].get('weather')} mid-day?")
            elif e["kind"] == "crowd":
                questions.append(f"what settings suit this room when it is unexpectedly {state['snapshot'].get('crowd')}?")
            elif e["kind"] == "co2":
                questions.append("how fast does the fan clear CO2 in this room, and what does it cost in warmth?")
            elif e["kind"] == "temp_drift":
                questions.append("why does this room drift from its setpoint, and what fixes it?")
            elif e["kind"] == "leaving":
                questions.append("when guests start leaving faster mid-day here, what has been the cause before?")
            elif e["kind"] == "empty":
                continue
            elif e["kind"] == "light_drift":
                questions.append("how much does daylight change the light in this room through the day?")
            elif e["kind"] == "note":  # about the kind of note, in memory's words (the same as the morning)
                questions.append(note_question(e["note"].get("text", "")))
            elif e["kind"] == "owner":
                questions.append(note_question(e["detail"].replace("owner says:", "", 1)))
            else:
                questions.append(f"what is known about this kind of situation in this cafe: {e['kind']}?")
        answers = ctx.memory_door.ask_all(questions, asked_by="intraday", date=ctx.date, t=state["t"],
                                          situation="; ".join(e["detail"] for e in state["events"]))
        return {"memory_answers": answers}

    def _held(rest: List[dict], t: int, note: str) -> str:
        """The change cap: every lever a replan moved stays within CHANGE_CAP of what the plan already
        said for that time; the room then ramps there at RAMP_PER_MIN, so the change stays gradual."""
        if not rest:
            return note
        held = cap_changes(rest, ctx.blocks, t)
        if held:
            ctx.log(f"{clock(t)} change cap: " + "; ".join(held))
            note = f"{note}; held: " + "; ".join(h.split(': ', 1)[1] for h in held)
        return note

    state_snap: Dict[str, Any] = {}

    def replan(state: CheckState) -> CheckState:
        t = state["t"]
        state_snap["snapshot"] = state.get("snapshot") or {}
        rule_only = all(e["kind"] == "empty" for e in state["events"])
        if not rule_only and ctx.intraday_agent is not None and ctx.model_calls_left() > 0 and ctx.budget_ok():
            if getattr(ctx, "tracker", None) is not None:
                ctx.tracker.context(purpose="intraday replan", questions=[f"{e['kind']} at {clock(t)}" for e in state["events"]])
            if hasattr(ctx, "replan_attempts"):
                ctx.replan_attempts += 1  # the day's cap counts these, not the memory questions
            try:
                rest = ctx.intraday_agent.replan(state["events"], t, ctx.blocks, ctx.note_blocks,
                                                 state["memory_answers"], state["snapshot"], ctx.capacity)
                if not rest:
                    ctx.log(f"{clock(t)} model looked and kept the plan")
                    return {"rest": [], "mode": "model", "note": "model kept the plan"}
                patch = getattr(ctx.intraday_agent, "last_patch", {})
                note = f"model changed {patch.get('changed', '?')} block(s); {patch.get('kept', '')}".strip("; ")
                return {"rest": rest, "mode": "model", "note": _held(rest, t, note)}
            except Exception as exc:
                ctx.log(f"replan: model failed ({exc!r}); heuristic instead")
        rest, note = heuristic_replan(state["events"], t, ctx.blocks, snap=state.get("snapshot"))
        return {"rest": rest, "mode": "heuristic", "note": _held(rest, t, note)}

    def push(state: CheckState) -> CheckState:
        if not state["rest"]:
            return {"pushed": False}
        t = state["t"]
        # every block keeps its birth window and stretch; a block this push changed is stamped as its maker
        for b in state["rest"]:
            inherit_origin(b, block_at(ctx.blocks, max(t, b["from"])))
            if clean_settings(b["settings"]) != settings_at(ctx.blocks, max(t, b["from"])):
                b["origin"] = {"by": state["mode"], "at": t, "note": str(state["note"])[:120]}
        stamp_origin(state["rest"], "daily")
        new_blocks = splice(ctx.blocks, t, state["rest"])
        _validate_coverage(new_blocks, 0, DAY_MINUTES)
        if same_schedule(ctx.blocks, state["rest"], state["t"]):
            # the plan's premise moved (assumes, reasons) but no setting did: record it, push nothing
            ctx.blocks[:] = new_blocks
            ctx.log(f"{clock(state['t'])} no setting changed ({state['mode']}): {state['note']}; premise recorded, nothing pushed")
            return {"pushed": False}
        ctx.blocks[:] = new_blocks
        ctx.push(state["rest"], state["t"], state["note"], state["mode"])
        ctx.log(f"{clock(state['t'])} pushed ({state['mode']}): {state['note']}")
        return {"pushed": True}

    g = StateGraph(CheckState)
    g.add_node("observe", observe)
    g.add_node("detect", detect)
    g.add_node("hold", hold)
    g.add_node("ask_memory", ask_memory)
    g.add_node("replan", replan)
    g.add_node("push", push)
    g.add_edge(START, "observe")
    g.add_edge("observe", "detect")
    g.add_conditional_edges("detect", route, {"ask_memory": "ask_memory", "hold": "hold"})
    g.add_edge("hold", END)
    g.add_edge("ask_memory", "replan")
    g.add_edge("replan", "push")
    g.add_edge("push", END)
    return g.compile()
