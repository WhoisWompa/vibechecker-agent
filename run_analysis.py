"""Run the analysis team once over a navigation state folder (the after-close pass).

    uv run python run_analysis.py --state state_nav/demo                  # after the last recorded day
    uv run python run_analysis.py --state state_nav/demo --date 2024-04-05
    uv run python run_analysis.py --state state_nav/demo --live           # stream every prompt and reply to a page

What it reads: <state>/question_bank.json (open questions), <state>/history.db (the master record),
<state>/memory.json (the experience memory; created if missing), <state>/checklist.md.
What it writes: verdicts into <state>/memory.json, new lines into checklist.md, "answered" onto bank
entries memory can now answer, a report in <state>/analysis/<date>.json and the research pass's event
log in <state>/analysis/run_<date>/events.jsonl (what --live shows).

The same pass runs inside run_navigation.py with --analysis nightly; this entry point is for running
it by hand between days, and for the demo recording. Needs a model (agent/.env).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ARCH = ROOT / "agent" / "Agent_architecture"
for _p in (ARCH, ARCH / "navigation_team"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _paths  # noqa: E402,F401  (sys.path for both teams)
from after_close import AfterClose, open_questions  # noqa: E402


def last_recorded_date(state: Path) -> str | None:
    days = sorted(p.stem for p in (state / "days").glob("*.json")) if (state / "days").exists() else []
    if days:
        return days[-1]
    try:
        import sqlite3
        with sqlite3.connect(f"file:{state / 'history.db'}?mode=ro", uri=True) as conn:
            row = conn.execute("SELECT MAX(date) FROM readings").fetchone()
        return row[0] if row and row[0] else None
    except Exception:
        return None


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--state", default="state_nav", help="the navigation team's state folder")
    p.add_argument("--date", default=None, help="the day being closed (default: last day on record)")
    p.add_argument("--memory", default=None, help="memory file (default <state>/memory.json)")
    p.add_argument("--max-questions", type=int, default=12, help="distinct bank questions researched per pass")
    p.add_argument("--max-flags", type=int, default=5, help="scanner flags handed to the reflection agent")
    p.add_argument("--live", action="store_true", help="open Lionel's live view and wait for Start")
    p.add_argument("--dry-run", action="store_true", help="only list what the pass would research")
    a = p.parse_args()

    state = Path(a.state)
    date = a.date or last_recorded_date(state) or time.strftime("%Y-%m-%d")
    bank = json.loads((state / "question_bank.json").read_text(encoding="utf-8")) if (state / "question_bank.json").exists() else []
    questions = open_questions(bank, a.max_questions)
    print(f"analysis team over {state} for {date}: {len(questions)} open question(s)")
    for q in questions:
        print(f"  - {q}")
    if a.dry_run:
        return

    pas = AfterClose(str(state), memory_path=a.memory, max_questions=a.max_questions, max_flags=a.max_flags)

    if a.live:
        from run_research import start_server  # Lionel's live page: / -> live_view.html, /events -> the event log
        log_dir = pas.out_dir / f"run_{date}"
        log_dir.mkdir(parents=True, exist_ok=True)
        server, run_event, status = start_server(log_dir, questions)
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        print(f"live view: {url}  (press Start on the page)")
        webbrowser.open(url)
        run_event.wait()
        status["state"] = "running"
        try:
            report = pas.run(date)
        finally:
            status["state"] = "idle"
    else:
        report = pas.run(date)

    print(json.dumps({k: report[k] for k in ("answered", "still_open", "flags") if k in report}, indent=2))
    if report.get("research_error"):
        print("research pass FAILED:\n" + report["research_error"][-800:])
    print(f"report: {pas.out_dir / (date + '.json')}")


if __name__ == "__main__":
    main()
