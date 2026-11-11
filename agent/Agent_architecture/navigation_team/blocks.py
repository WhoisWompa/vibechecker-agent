"""Shared settings-block models, validators and the per-minute schedule they
turn into. Plain data + deterministic checks — no LLM here.

The venue is a cafe; one timestep = 1 minute, t=0 is 8am, a day is 840
minutes (0..840).

A block is ``{from, to, settings, reasons, assumes}``. ``assumes`` is the
premise the block's settings rest on, written as two short words so the
intraday agent can compare it with what actually happens without a model
call: ``{"weather": fair|cloudy|rain, "crowd": quiet|moderate|busy}``.
"""

from __future__ import annotations

from typing import Dict, List, Optional

import pandas as pd
from pydantic import BaseModel, Field

DAY_MINUTES = 840
LEVERS = ("light", "temperature", "volume", "fan")

# The fixed 2-hour grid the team learns on: seven stretches, 08-10 ... 20-22. Plan blocks are
# free-form; every block also knows which stretch it started in.
STRETCH = 120
N_STRETCH = 7

# hardware range per lever: (low, high)
RANGE: Dict[str, tuple] = {"light": (0.0, 1500.0), "temperature": (18.0, 30.0),
                           "volume": (30.0, 90.0), "fan": (0, 5)}

# Largest per-minute step a guest will not notice (Ashley's rule, 3 Sep):
# ~1 dB for loudness, ~1% of level for light, 0.1 C for warmth. Fan is not
# felt directly and may move at once.
RAMP_PER_MIN: Dict[str, float] = {"light": 6.0, "temperature": 0.1, "volume": 0.5, "fan": 5.0}

# Largest move one replan may make on a lever, measured from what the plan already said for that
# time (Ashley's rule, 7 Sep): a replan adjusts, it never lurches. The previous stretch is context
# the model reads; the cap bounds the size of this replan's own step. Together with RAMP_PER_MIN
# (the room takes 10 min to move 1 C, about 17 min to move 100 lux) every change stays gradual.
# Owner overrides and the after-9pm energy mode are exempt. Enforced in code, not in the model.
CHANGE_CAP: Dict[str, float] = {"light": 100.0, "temperature": 1.0, "volume": 4.0, "fan": 1}

# Hard comfort bounds the agent never crosses, owner overrides included. These sit
# outside every agent: whatever a plan says, the room never gets a setting past them.
COMFORT: Dict[str, tuple] = {"temperature": (21.0, 26.0), "volume": (40.0, 70.0)}

WEATHER_WORDS = ("fair", "cloudy", "rain")
CROWD_WORDS = ("quiet", "moderate", "busy")


# ---------------------------------------------------------------------------
# Structured-output models (shared building blocks)
# ---------------------------------------------------------------------------

class Settings(BaseModel):
    light: float
    temperature: float
    volume: float
    fan: int


class SettingsBlock(BaseModel):
    from_: int
    to: int
    settings: Settings
    reasons: str
    assumes: Dict[str, str] = Field(default_factory=dict)


class NoteBlock(BaseModel):
    from_: Optional[int] = None
    to: Optional[int] = None
    kind: str  # "fact" | "override"
    fact: str = ""
    settings: Optional[Settings] = None
    reason: str = ""
    source: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def clamp(lever: str, value: float) -> float:
    lo, hi = RANGE[lever]
    v = min(max(float(value), lo), hi)
    return int(round(v)) if lever == "fan" else round(v, 2)


def _settings_to_dict(s: Settings) -> dict:
    return {"light": s.light, "temperature": s.temperature, "volume": s.volume, "fan": s.fan}


def _block_to_dict(b: SettingsBlock) -> dict:
    return {"from": b.from_, "to": b.to, "settings": _settings_to_dict(b.settings),
            "reasons": b.reasons, "assumes": dict(b.assumes or {})}


def _note_block_to_dict(n: NoteBlock) -> dict:
    return {
        "from": n.from_,
        "to": n.to,
        "kind": n.kind,
        "fact": n.fact,
        "settings": _settings_to_dict(n.settings) if n.settings is not None else None,
        "reason": n.reason,
        "source": n.source,
    }


def clean_settings(s: dict) -> dict:
    """Clamp a settings dict to the hardware range and round it."""
    return {lever: clamp(lever, s.get(lever, 0)) for lever in LEVERS}


def _validate_coverage(blocks: list[dict], start: int, end: int):
    """Blocks must be ordered by 'from', cover [start, end) with no gaps or overlaps."""
    if not blocks:
        raise ValueError(f"no settings blocks produced to cover {start}..{end}")
    ordered = sorted(blocks, key=lambda b: b["from"])
    if ordered != blocks:
        raise ValueError("settings blocks are not ordered by 'from'")
    if ordered[0]["from"] != start:
        raise ValueError(f"coverage does not start at {start} (starts at {ordered[0]['from']})")
    cursor = start
    for b in ordered:
        if b["from"] != cursor:
            if b["from"] > cursor:
                raise ValueError(f"gap in settings blocks between {cursor} and {b['from']}")
            raise ValueError(f"overlap in settings blocks at {b['from']} (expected {cursor})")
        if b["to"] <= b["from"]:
            raise ValueError(f"block from {b['from']} to {b['to']} is empty or inverted")
        cursor = b["to"]
    if cursor != end:
        raise ValueError(f"coverage does not end at {end} (ends at {cursor})")


def _validate_override_windows(settings_blocks: list[dict], note_blocks: list[dict]):
    """Every override note's window must carry exactly the note's settings."""
    for note in note_blocks:
        if note["kind"] != "override":
            continue
        if note["settings"] is None:
            raise ValueError("override note is missing settings")
        window_start = note["from"]
        window_end = note["to"]
        covering = [
            b for b in settings_blocks
            if window_start is not None and window_end is not None
            and b["from"] < window_end and b["to"] > window_start
        ]
        for b in covering:
            if clean_settings(b["settings"]) != clean_settings(note["settings"]):
                raise ValueError(
                    f"override note window {window_start}..{window_end} not carried "
                    f"verbatim into settings blocks (mismatch at block {b['from']}..{b['to']})"
                )


def enforce_bounds(blocks: list[dict]) -> list[str]:
    """Clamp every block to the comfort bounds; report what was moved."""
    moved = []
    for b in blocks:
        s = clean_settings(b["settings"])
        for lever, (lo, hi) in COMFORT.items():
            if s[lever] < lo or s[lever] > hi:
                moved.append(f"block {b['from']}: {lever} {s[lever]:g} held to {lo:g}-{hi:g}")
                s[lever] = min(max(s[lever], lo), hi)
        b["settings"] = s
    return moved


def validate_assumes(blocks: list[dict]) -> list[str]:
    """Normalise each block's assumes to the two known words; report what was fixed."""
    fixed = []
    for b in blocks:
        a = dict(b.get("assumes") or {})
        w = str(a.get("weather", "")).lower()
        c = str(a.get("crowd", "")).lower()
        a["weather"] = weather_category(w) if w else "fair"
        a["crowd"] = c if c in CROWD_WORDS else crowd_from_text(c)
        if a != b.get("assumes"):
            fixed.append(f"block {b['from']}: assumes normalised to {a}")
        b["assumes"] = a
    return fixed


def weather_category(text: str) -> str:
    """Collapse a forecast wording to fair / cloudy / rain."""
    t = (text or "").lower()
    if any(w in t for w in ("rain", "shower", "thunder", "storm", "drizzle")):
        return "rain"
    if any(w in t for w in ("cloud", "overcast", "hazy", "haze", "mist", "fog")):
        return "cloudy"
    return "fair"


def crowd_from_text(text: str) -> str:
    t = (text or "").lower()
    if any(w in t for w in ("busy", "full", "packed", "crowd", "peak", "rush")):
        return "busy"
    if any(w in t for w in ("quiet", "empty", "slow", "sparse", "dead")):
        return "quiet"
    return "moderate"


def crowd_category(occupancy_frac: float) -> str:
    """quiet under a quarter full, busy over six tenths, moderate between."""
    if occupancy_frac < 0.25:
        return "quiet"
    if occupancy_frac > 0.6:
        return "busy"
    return "moderate"


def block_at(blocks: List[dict], t: int) -> dict:
    for b in blocks:
        if b["from"] <= t < b["to"]:
            return b
    return blocks[-1]


# ---------------------------------------------------------------------------
# Stretches and where "now" is
# ---------------------------------------------------------------------------

def stretch_of(t: int) -> int:
    """0..6: which 2-hour stretch minute ``t`` falls in."""
    return min(N_STRETCH - 1, max(0, int(t) // STRETCH))


def stretch_bounds(i: int) -> tuple:
    return i * STRETCH, min(DAY_MINUTES, (i + 1) * STRETCH)


def stamp_origin(blocks: List[dict], by: str, at: Optional[int] = None, note: str = "") -> None:
    """Give each block that lacks one its identity: the window it was born with, its stretch, and who
    made it. A block cut by a push keeps its birth window; a block a push created gets stamped then."""
    for b in blocks:
        if "origin_from" not in b:
            b["origin_from"], b["origin_to"] = b["from"], b["to"]
        if "stretch" not in b:
            b["stretch"] = stretch_of(b["origin_from"])
        if not b.get("origin"):
            b["origin"] = {"by": by, "at": at, "note": str(note)[:120]}


def inherit_origin(new: dict, cut: dict) -> dict:
    """A block made by cutting ``cut`` keeps the cut block's birth window and stretch (not its maker)."""
    for k in ("origin_from", "origin_to", "stretch"):
        if k in cut and k not in new:
            new[k] = cut[k]
    return new


def where_now(blocks: List[dict], t: int) -> dict:
    """Which stretch and which plan block minute ``t`` is inside, and how that block came to exist."""
    i = stretch_of(t)
    lo, hi = stretch_bounds(i)
    b = block_at(blocks, t)
    origin = b.get("origin") or {}
    return {"t": t, "clock": clock(t), "stretch": i, "stretch_from": lo, "stretch_to": hi,
            "minutes_into_stretch": t - lo, "minutes_left_in_stretch": hi - t,
            "block_from": b["from"], "block_to": b["to"],
            "block_origin_from": b.get("origin_from", b["from"]), "block_origin_to": b.get("origin_to", b["to"]),
            "block_set_by": origin.get("by", "daily"), "block_set_at": origin.get("at"),
            "block_set_note": origin.get("note", "")}


def describe_where(w: dict) -> str:
    """One line the model cannot misread."""
    line = (f"{w['clock']}: {w['minutes_into_stretch']} min into the {clock(w['stretch_from'])}-{clock(w['stretch_to'])} "
            f"stretch, {w['minutes_left_in_stretch']} min left.")
    if w.get("block_set_at") is not None:
        line += (f" The block in force ({clock(w['block_origin_from'])}-{clock(w['block_origin_to'])}) was made by a "
                 f"{w['block_set_by']} push at {clock(w['block_set_at'])}" + (f": {w['block_set_note']}" if w.get("block_set_note") else "") + ".")
    else:
        line += f" The block in force ({clock(w['block_origin_from'])}-{clock(w['block_origin_to'])}) is from the morning plan."
    return line


def cap_changes(rest: List[dict], before: List[dict], now: int, reference: Optional[dict] = None,
                cap: Optional[Dict[str, float]] = None) -> List[str]:
    """Hold every lever a replan changed to within ``cap`` of what the plan already said for that time
    (``before``, the schedule this replan is rewriting). With ``reference`` given, that fixed point is
    the anchor instead. Blocks the replan left as they were are not touched; owner overrides and the
    energy mode are exempt. Returns what was held, in words."""
    cap = cap or CHANGE_CAP
    ref = clean_settings(reference) if reference else None
    held: List[str] = []
    for b in rest:
        reasons = str(b.get("reasons", ""))
        if reasons.startswith("owner's override") or reasons.startswith("energy mode"):
            continue
        was = settings_at(before, max(now, b["from"])) if before else None
        anchor = ref or was
        if anchor is None:
            continue
        new = clean_settings(b["settings"])
        for lever in LEVERS:
            if was is not None and new[lever] == was[lever]:
                continue  # this replan did not move it
            lo, hi = anchor[lever] - cap[lever], anchor[lever] + cap[lever]
            if new[lever] < lo or new[lever] > hi:
                to = clamp(lever, min(max(new[lever], lo), hi))
                held.append(f"block {b['from']}: {lever} {new[lever]:g} held to {to:g} "
                            f"(within {cap[lever]:g} of the {'previous stretch' if ref else 'plan'}'s {anchor[lever]:g})")
                new[lever] = to
        b["settings"] = new
    return held


def settings_at(blocks: List[dict], t: int) -> dict:
    return clean_settings(block_at(blocks, t)["settings"])


def splice(blocks: List[dict], now: int, rest: List[dict]) -> List[dict]:
    """Keep the past (before ``now``) from ``blocks``; the rest of the day comes from ``rest``."""
    kept = []
    for b in blocks:
        if b["to"] <= now:
            kept.append(b)
        elif b["from"] < now:
            kept.append({**b, "to": now})
    return kept + [dict(b) for b in rest]


def _ramp(start: float, end: float, lever: str, minutes: int) -> List[float]:
    step = RAMP_PER_MIN[lever]
    values: List[float] = []
    v = start
    for _ in range(minutes):
        if abs(end - v) <= step:
            v = end
        else:
            v += step if end > v else -step
        values.append(clamp(lever, v))
    return values


def schedule_df(blocks: List[dict], from_t: int = 0, current: Optional[dict] = None,
                n: int = DAY_MINUTES) -> pd.DataFrame:
    """One row per minute from ``from_t`` to close, ramping every block change.

    ``current`` is what the room is running at ``from_t`` (None = start the
    first block at its own settings). Columns are what ``set_schedule`` reads.
    """
    level = dict(current) if current else settings_at(blocks, from_t)
    level = clean_settings(level)
    rows: List[dict] = []
    t = from_t
    while t < n:
        b = block_at(blocks, t)
        end = min(n, b["to"]) if b["to"] > t else n
        goal = clean_settings(b["settings"])
        span = end - t
        ramps = {lever: _ramp(level[lever], goal[lever], lever, span) for lever in LEVERS}
        for i in range(span):
            rows.append({lever: ramps[lever][i] for lever in LEVERS})
        level = {lever: ramps[lever][-1] for lever in LEVERS}
        t = end
    return pd.DataFrame(rows, columns=list(LEVERS))


def clock(t: int) -> str:
    h, m = divmod(8 * 60 + int(t), 60)
    return f"{h:02d}:{m:02d}"


def plan_markdown(date: str, weekday: str, blocks: List[dict], notes: Optional[List[dict]] = None,
                  header: str = "") -> str:
    """The plan file as the daily agent hands it to the intraday agent."""
    lines = [f"# Plan for {weekday} {date}", ""]
    if header:
        lines += [header, ""]
    if notes:
        lines.append("## Owner's notes")
        for nt in notes:
            win = f" ({clock(nt['from'])}-{clock(nt['to'])})" if nt.get("from") is not None else ""
            lines.append(f"- [{nt.get('kind', 'fact')}]{win} {nt.get('fact') or nt.get('text', '')}"
                         + (f" -> {nt['settings']}" if nt.get("settings") else ""))
        lines.append("")
    lines.append("## Blocks")
    lines.append("| from | to | light | temp | volume | fan | assumes | reason |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for b in blocks:
        s = clean_settings(b["settings"])
        a = b.get("assumes") or {}
        lines.append(f"| {clock(b['from'])} | {clock(b['to'])} | {s['light']:g} | {s['temperature']:g} | "
                     f"{s['volume']:g} | {s['fan']} | {a.get('weather', '')}/{a.get('crowd', '')} | "
                     f"{b['reasons'].replace('|', '/')} |")
    return "\n".join(lines) + "\n"
