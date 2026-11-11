"""Today's checklist for the daily agent: a synthesis of the standing habits
and the owner's note, built in plain code every morning.

Two sources:
- ``state/checklist.md``: one line per standing habit (decisions rule 36).
  Hand-written to start; the reflection agent may add lines later.
- today's facts: the weekday, the calendar, the 24-hour forecast, and each
  owner's note. Every fact becomes an item to check and, where memory might
  know something, a plain-word question to ask the memory agent first.

No model call here: the questions are templated, the daily agent gets the
answers in its prompt.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from questions import note_question

DEFAULT_HABITS = [
    "Check what day of the week it is and whether it is a public or school holiday.",
    "Check the 24-hour forecast: is rain expected, how hot will it get.",
    "Read the owner's note for today and plan around every item in it.",
    "Ask memory what is known before guessing; a guess must say it is a guess in the reason.",
    "Lunch (12-2pm) and dinner (6-8pm) are the busiest stretches; plan them as their own blocks.",
    "Never plan settings outside the comfort bounds: temperature 21-26 C, volume 40-70 dB, CO2 near 800 ppm and never above 1000.",
    "After 9pm an empty room goes to energy mode: dimmer, quiet, fan off.",
]


def standing_habits(state_dir: str) -> List[str]:
    p = Path(state_dir) / "checklist.md"
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# Checklist of habits\n" + "".join(f"- {h}\n" for h in DEFAULT_HABITS), encoding="utf-8")
    lines = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("- "):
            lines.append(line[2:].strip())
    return lines


def build_items(date: str, weekday: str, calendar: List[str], forecast24: Dict[str, Any],
                notes: List[Dict[str, Any]]) -> List[Dict[str, Optional[str]]]:
    """Today's items: each has what to check and, where useful, a question for memory."""
    items: List[Dict[str, Optional[str]]] = []
    items.append({"item": f"Today is {weekday} {date}.",
                  "question": f"what settings suit a {weekday} in this cafe?"})
    for ev in calendar:
        if ev == "weekend":
            continue
        items.append({"item": f"Calendar: {ev}.",
                      "question": f"what settings suit this cafe on a {ev.split(':')[0]}?"})
    text = forecast24.get("forecast_text")
    lo = forecast24.get("temperature_low")
    hi = forecast24.get("temperature_high")
    if text:
        band = f", outside {lo:g}-{hi:g} C" if lo is not None and hi is not None else ""
        items.append({"item": f"24-hour forecast: {text}{band}.",
                      "question": f"what settings work on a {str(text).lower()} day?"})
    else:
        items.append({"item": "No 24-hour forecast published yet at open; the intraday agent watches the 2-hour issues.",
                      "question": None})
    tl = str(text or "").lower()
    if any(w in tl for w in ("rain", "shower", "thunder")):
        items.append({"item": "Rain is expected at some point today.",
                      "question": "what do people want from this room when it rains?"})
    if hi is not None and hi >= 33:
        items.append({"item": f"A hot day: up to {hi:g} C outside.",
                      "question": "what works in this room on a very hot day?"})
    for n in notes:
        if n.get("kind") == "override":
            items.append({"item": f"Owner's override: {n['text']} (copy its settings verbatim for its window).",
                          "question": None})
        else:  # the question is about the kind of note, in memory's words; the note itself rides along
            items.append({"item": f"Owner's note: {n['text']}",
                          "question": note_question(n["text"]), "note": n})
    return items


def render(habits: List[str], items: List[Dict[str, Optional[str]]]) -> str:
    lines = ["Standing habits:"] + [f"- {h}" for h in habits] + ["", "Today's items:"]
    for it in items:
        lines.append(f"- {it['item']}")
    return "\n".join(lines)
