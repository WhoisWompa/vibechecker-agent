"""The owner's notes: one dated file per day. The stand-in for a calendar.

``state/notes/<date>.json`` holds the notes for that day, each
``{kind, text, from, to, settings, reason, source}``. Two kinds (decisions
rule 41): a fact note says what is happening and the daily agent decides the
settings; an override note carries exact settings for its window and is
copied into the plan as-is. Settings numbers enter a note from the owner's
hand only.

A note is written in plain words with an optional time window:

    "big group of 12 at 3pm-5pm"
    "closing early, last order 8pm"
    "override 6pm-10pm light=300 volume=45: quieter evenings"

Windows are read the way a person writes them: "3pm-5pm", "14:00-16:00",
"after 6pm", "before 11am", "at lunch", "all day". No window means the whole
day.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DAY_MINUTES = 840
PERIODS = {"morning": (0, 240), "brunch": (180, 300), "lunch": (240, 360), "afternoon": (360, 600),
           "evening": (600, 840), "dinner": (600, 780), "night": (720, 840),
           "all day": (0, 840), "whole day": (0, 840)}


def _clock_to_minute(h: int, ampm: Optional[str], m: int = 0) -> int:
    if ampm:
        h = h % 12 + (12 if ampm == "pm" else 0)
    return (h * 60 + m) - 8 * 60


def parse_window(text: str) -> Optional[Tuple[int, int]]:
    """A time window in minutes since open, or None."""
    t = text.lower()
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(?:-|to|until|till)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)", t)
    if m:
        h1, m1, ap1, h2, m2, ap2 = m.groups()
        ap1 = ap1 or ap2
        return (max(0, _clock_to_minute(int(h1), ap1, int(m1 or 0))),
                min(DAY_MINUTES, _clock_to_minute(int(h2), ap2, int(m2 or 0))))
    m = re.search(r"(\d{1,2}):(\d{2})\s*(?:-|to)\s*(\d{1,2}):(\d{2})", t)
    if m:
        h1, m1, h2, m2 = (int(x) for x in m.groups())
        return (max(0, h1 * 60 + m1 - 480), min(DAY_MINUTES, h2 * 60 + m2 - 480))
    m = re.search(r"(?:after|from)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)", t)
    if m:
        return (max(0, _clock_to_minute(int(m.group(1)), m.group(3), int(m.group(2) or 0))), DAY_MINUTES)
    m = re.search(r"(?:before|until|till)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)", t)
    if m:
        return (0, min(DAY_MINUTES, _clock_to_minute(int(m.group(1)), m.group(3), int(m.group(2) or 0))))
    m = re.search(r"\bat\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)", t)
    if m:
        start = max(0, _clock_to_minute(int(m.group(1)), m.group(3), int(m.group(2) or 0)))
        return (start, min(DAY_MINUTES, start + 120))
    for word, win in PERIODS.items():
        if word in t:
            return win
    return None


def _parse_settings(text: str) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for lever, val in re.findall(r"\b(light|temperature|temp|volume|fan)\s*=\s*(-?\d+(?:\.\d+)?)", text.lower()):
        lever = "temperature" if lever == "temp" else lever
        out[lever] = int(val) if lever == "fan" else float(val)
    return out


def make_note(text: str, source: str = "owner") -> Dict[str, Any]:
    """Plain words -> a note dict. 'override ...' with lever=value pairs becomes an override."""
    text = text.strip()
    window = parse_window(text)
    body, _, reason = text.partition(":") if text.lower().startswith("override") else (text, "", "")
    if text.lower().startswith("override"):
        settings = _parse_settings(body)
        if not settings:
            raise ValueError("an override note needs lever=value pairs, e.g. 'override 6pm-10pm light=300'")
        return {"kind": "override", "text": text, "from": window[0] if window else 0,
                "to": window[1] if window else DAY_MINUTES, "settings": settings,
                "reason": reason.strip() or "owner's override", "source": source}
    return {"kind": "fact", "text": text, "from": window[0] if window else None,
            "to": window[1] if window else None, "settings": None, "reason": "", "source": source}


def parse_spec(spec: str) -> Tuple[Optional[str], str]:
    """'2024-01-05: big group at 3pm' -> ('2024-01-05', 'big group at 3pm'); no date -> (None, text)."""
    m = re.match(r"\s*(\d{4}-\d{2}-\d{2})\s*[:|]\s*(.+)$", spec, re.S)
    if m:
        return m.group(1), m.group(2).strip()
    return None, spec.strip()


class NoteBook:
    """The dated notes folder."""

    def __init__(self, state_dir: str) -> None:
        self.dir = Path(state_dir) / "notes"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, date: str) -> Path:
        return self.dir / f"{date}.json"

    def add(self, date: str, text: str, source: str = "owner") -> Dict[str, Any]:
        note = make_note(text, source)
        notes = self.for_date(date)
        notes.append(note)
        with open(self._path(date), "w", encoding="utf-8") as f:
            json.dump(notes, f, indent=2)
        return note

    def add_prepared(self, date: str, note: Dict[str, Any]) -> bool:
        """File a note that is already parsed (a calendar event). Returns False if it is on file already:
        same source and event id, or same source, text and window."""
        notes = self.for_date(date)
        for old in notes:
            if old.get("source") != note.get("source"):
                continue
            if note.get("event_id") and old.get("event_id") == note["event_id"]:
                return False
            if old.get("text") == note.get("text") and old.get("from") == note.get("from") and old.get("to") == note.get("to"):
                return False
        notes.append(note)
        with open(self._path(date), "w", encoding="utf-8") as f:
            json.dump(notes, f, indent=2)
        return True

    def for_date(self, date: str) -> List[Dict[str, Any]]:
        p = self._path(date)
        if not p.exists():
            return []
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)


def full_settings(partial: Dict[str, float], base: Dict[str, float]) -> Dict[str, float]:
    """An override may name only some levers; the rest come from the base plan."""
    out = dict(base)
    out.update(partial)
    return out
