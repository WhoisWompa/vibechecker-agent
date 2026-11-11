"""Run the navigation team against the cafe.

    uv run python run_navigation.py --days 2 --seed 1 --start 2024-01-05 \
        --note "big group of 12 at 3pm-5pm" --mode model

    --mode model      daily + intraday agents call the model (default)
    --mode heuristic  same loop, no model calls
    --mode fixed      baseline timetable only, no intraday checks
    --note "text"     an owner's note for day 1 (or "2024-01-06: text" for a date)
    --message "t=390: text"   an owner message that reaches the intraday agent at that minute
    --calendar "Vibechecker owner"   read each day's owner's notes from that Google Calendar
    --analysis nightly   run the analysis team after every day (weekly: after every 7th and the last day);
                         it reads the question bank and history.db, writes verdicts into <state>/memory.json
                         and the checklist, and marks bank questions answered. Needs a model even in
                         heuristic mode. --memory seeds <state>/memory.json on the first run only.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "agent" / "Agent_architecture" / "navigation_team"))

from runner import NavRun  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=1)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--start", default=None, help="calendar date for day 1, e.g. 2024-01-05")
    p.add_argument("--mode", choices=["model", "heuristic", "fixed"], default="model")
    p.add_argument("--state", default="state_nav", help="where the team writes everything")
    p.add_argument("--note", action="append", default=[], help="owner's note; repeatable")
    p.add_argument("--message", action="append", default=[], help="owner message 't=390: ...'; repeatable")
    p.add_argument("--capacity", type=int, default=30)
    p.add_argument("--max-model-calls", type=int, default=12,
                   help="intraday replans by the model per day (memory questions do not count; --budget-calls caps everything)")
    p.add_argument("--check-every", type=int, default=15, help="minutes between intraday checks")
    p.add_argument("--memory", default=None, help="path to an ExperienceMemory json (Lionel's)")
    p.add_argument("--severity", type=float, default=None, help="cafe severity knob, 0 (forgiving) to 1")
    p.add_argument("--preset", default=None, help="cafe preset, e.g. demo (a room that can fill)")
    p.add_argument("--booking", action="append", default=[],
                   help="a booked party the simulation makes real: 'minute:size', e.g. '420:12' for 12 people at 3pm; repeatable")
    p.add_argument("--budget-calls", type=int, default=80, help="hard stop: model calls for the whole run")
    p.add_argument("--budget-usd", type=float, default=1.0, help="hard stop: estimated dollars for the whole run")
    p.add_argument("--calendar", default=os.environ.get("VIBE_CALENDAR"),
                   help="the owner's Google Calendar (name or id); its events become the day's owner's notes. "
                        "Needs `uv sync --extra calendar` and GOOGLE_OAUTH_CREDENTIALS in agent/.env")
    p.add_argument("--analysis", choices=["off", "nightly", "weekly"], default="off",
                   help="when the analysis team runs after close (default off)")
    p.add_argument("--quiet", action="store_true")
    a = p.parse_args()

    knobs = {}
    if a.severity is not None:
        knobs["severity"] = a.severity
    if a.preset:
        knobs["preset"] = a.preset
    if a.booking:
        knobs["bookings"] = [(int(b.split(":")[0]), int(b.split(":")[1])) for b in a.booking]
    NavRun(days=a.days, seed=a.seed, start_date=a.start, state_dir=a.state, mode=a.mode, notes=a.note,
           messages=a.message, capacity=a.capacity, max_model_calls=a.max_model_calls,
           check_every=a.check_every, memory_path=a.memory, verbose=not a.quiet, cafe_knobs=knobs,
           budget_calls=a.budget_calls, budget_usd=a.budget_usd, calendar=a.calendar,
           analysis=a.analysis).run()


if __name__ == "__main__":
    main()
