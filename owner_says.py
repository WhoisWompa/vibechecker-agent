"""The owner tells the cafe something; it lands on the owner's calendar, after a yes.

    uv run python owner_says.py "book a group of 12 next Friday 3 to 5pm"
    uv run python owner_says.py "we close early on the 12th, last order 8pm" --as-of 2024-04-05
    uv run python owner_says.py "quieter evenings: 6pm-10pm Saturday light 300 volume 45" --yes

    --as-of YYYY-MM-DD   the date "today" means (default: today); the demo replays 2024 dates
    --calendar NAME      the owner's calendar (default: VIBE_CALENDAR in agent/.env)
    --yes                create without asking
    --dry-run            show what would be created and stop

Needs the calendar setup in agent/Agent_architecture/navigation_team/CALENDAR_SETUP.md and a
model in agent/.env (OWNER_MODEL, else DEFAULT_MODEL). One model call per sentence.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "agent" / "Agent_architecture"))
sys.path.insert(0, str(ROOT / "agent" / "Agent_architecture" / "navigation_team"))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("text", help="what the owner said, in quotes")
    p.add_argument("--as-of", default=dt.date.today().isoformat())
    p.add_argument("--calendar", default=None)
    p.add_argument("--yes", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()

    from calendar_notes import CalendarSource
    from llm import get_llm
    from owner_desk import OwnerDesk, describe, link_in

    calendar = a.calendar or os.environ.get("VIBE_CALENDAR")
    if not calendar:
        print("no calendar named: pass --calendar or set VIBE_CALENDAR in agent/.env")
        return 2
    desk = OwnerDesk(CalendarSource(calendar), get_llm("owner"))
    req = desk.propose(a.text, a.as_of)
    print("the desk read that as:")
    print("  " + describe(req).replace("\n", "\n  "))
    print(f"  on calendar '{calendar}'")
    if a.dry_run:
        return 0
    if not a.yes:
        answer = input("create this event? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("nothing created")
            return 0
    reply = desk.create(req)
    link = link_in(reply)
    print("created" + (f": {link}" if link else ""))
    if not link:
        print("  server said: " + reply[:300])
    return 0


if __name__ == "__main__":
    sys.exit(main())
