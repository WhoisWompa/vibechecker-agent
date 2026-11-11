"""Render a navigation run as a scrubbable day timeline, for the demo.

    uv run python run_timeline.py state_nav/model-demo3 --open          # every day in the run, a day switcher on top
    uv run python run_timeline.py state_nav/eval5/model --compare state_nav/eval5/fixed state_nav/eval5/heuristic
    uv run python run_timeline.py state_nav/eval5/model --date 2024-01-07   # one day only

Writes <state_dir>/timeline.html: one self-contained file, no network needed.
Reads only what the navigation team wrote: plans/, days/, notes/, question_bank.json,
messages.jsonl and history.db. `--compare` overlays other runs of the same dates
(their crowd and their pushes) so fixed, heuristic and model can be seen side by side.
Open with `#t=390` to start at 14:30, `#t=390&open=1` to also open that minute's prompts,
`#d=2024-01-07` to start on a day.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "scripts" / "timeline_template.html"

LOG_LINE = re.compile(r"^(\d\d:\d\d) (discrepancy|pushed \((\w+)\)|no setting changed \((\w+)\)|model looked and kept the plan|change cap)(?::\s*(.*))?$")
MEM_BLOCK = re.compile(r"What the memory agent answered.*?:\n(\[.*?\])\n\n", re.S)


def clock_to_t(clock: str) -> int:
    h, m = clock.split(":")
    return (int(h) - 8) * 60 + int(m)


def load_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def events_from_log(log: list[str]) -> list[dict]:
    """Group the day's log into one event per clock time: what fired, what was asked, what happened."""
    events: dict[int, dict] = {}
    for line in log:
        m = LOG_LINE.match(line)
        if not m:
            continue
        clock, kind, rest = m.group(1), m.group(2), m.group(5) or ""
        mode = m.group(3) or m.group(4)
        ev = events.setdefault(clock_to_t(clock), {"t": clock_to_t(clock), "clock": clock,
                                                    "fired": [], "outcome": None, "mode": None,
                                                    "note": "", "held": []})
        if kind == "discrepancy":
            ev["fired"] = [rest.strip()]
        elif kind.startswith("pushed"):
            ev["outcome"] = "pushed"
            ev["mode"] = mode
            note, _, _held = rest.partition("; held: ")  # the cap is also logged on its own line
            ev["note"] = note.strip().rstrip('"')
        elif kind == "model looked and kept the plan":
            ev["outcome"] = "kept"
            ev["mode"] = "model"
        elif kind.startswith("no setting changed"):
            ev["outcome"] = "kept"
            ev["mode"] = mode
            ev["note"] = rest.split("; held:")[0].strip()
        elif kind == "change cap":
            ev["held"].append(rest)
    return [events[k] for k in sorted(events)]


def readings(db: Path, date: str, cols: tuple[str, ...]) -> list[dict]:
    if not db.exists():
        return []
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(f"select time, {', '.join(cols)} from readings where date=? order by time", (date,)).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for r in rows:
        d = {c: r[c] for c in cols}
        d["t"] = clock_to_t(r["time"])
        out.append(d)
    return out


FULL_COLS = ("light_set", "temperature_set", "volume", "fan", "temperature_sensor", "co2", "noise",
             "light_sensor", "occupancy", "entered", "left", "outside_temperature", "central_forecast_text",
             "block_from", "set_by")


def model_calls(path: Path, date: str) -> tuple[list[dict], dict[int, list[dict]]]:
    """The day's prompts and replies, and the memory answers each intraday prompt carried."""
    calls: list[dict] = []
    answers: dict[int, list[dict]] = {}
    if not path.exists():
        return calls, answers
    for line in path.read_text().splitlines():
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            continue
        if m.get("date") != date:
            continue
        loop = m.get("loop", "")
        t = 0 if loop == "morning" else int(loop.split("@")[1]) if "@" in loop else None
        reply = m.get("reply", "")
        kept = None
        reply_text = reply
        try:
            shown = []
            for part in json.loads(reply):
                inp = part.get("input", {})
                if "kept" in inp:
                    kept = inp["kept"]
                shown.append({part.get("name", "reply"): inp} if part.get("type") == "tool_use" else part)
            reply_text = json.dumps(shown, indent=1, ensure_ascii=False)
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass
        prompt = m.get("prompt", "")
        if m.get("agent") == "intraday" and t is not None:
            mb = MEM_BLOCK.search(prompt)
            if mb:
                try:
                    answers[t] = json.loads(mb.group(1))
                except json.JSONDecodeError:
                    pass
        calls.append({"t": t, "agent": m.get("agent"), "purpose": m.get("purpose"),
                      "questions": m.get("questions", []), "model": m.get("model"),
                      "latency_s": m.get("latency_s"), "tokens": m.get("total_tokens"), "usd": m.get("usd"),
                      "kept": kept, "prompt": prompt, "reply": reply_text})
    return calls, answers


STAT_KEYS = ("day", "entered", "mean_occupancy", "peak_occupancy", "stay_est_min", "co2_peak", "co2_over_floor_min",
             "temp_mean", "replans", "model_calls", "tokens", "usd", "questions_asked", "questions_banked", "hidden_score")


def one_day(state_dir: Path, date: str, bank: list[dict]) -> dict:
    plan = load_json(state_dir / "plans" / f"{date}.json", {})
    day = load_json(state_dir / "days" / f"{date}.json", {})
    calls, mem_answers = model_calls(state_dir / "messages.jsonl", date)
    return {
        "date": date,
        "weekday": plan.get("weekday") or day.get("weekday"),
        "mode": plan.get("mode") or day.get("mode"),
        "forecast24": plan.get("forecast24", {}),
        "baseline_source": plan.get("baseline_source") or day.get("baseline_source"),
        "checklist": plan.get("checklist", ""),
        "memory_answers": plan.get("memory_answers", []),
        "morning_blocks": plan.get("settings_blocks", []),
        "note_blocks": plan.get("note_blocks", []),
        "notes": load_json(state_dir / "notes" / f"{date}.json", []),
        "pushes": day.get("pushes", []),
        "events": events_from_log(day.get("log", [])),
        "calls": calls,
        "mem_answers": {str(k): v for k, v in mem_answers.items()},
        "bank": [q for q in bank if q.get("date") == date],
        "readings": readings(state_dir / "history.db", date, FULL_COLS),
        "stats": {k: day.get(k) for k in STAT_KEYS},
    }


def compare_day(state_dir: Path, date: str) -> dict | None:
    day = load_json(state_dir / "days" / f"{date}.json", None)
    if day is None:
        return None
    return {
        "mode": day.get("mode") or state_dir.name,
        "occupancy": [[r["t"], r["occupancy"]] for r in readings(state_dir / "history.db", date, ("occupancy",))
                      if r["occupancy"] is not None],
        "push_ts": [p["t"] for p in day.get("pushes", [])],
        "stats": {k: day.get(k) for k in STAT_KEYS},
    }


def build(state_dir: Path, date: str | None, compare: list[Path]) -> Path:
    plans = sorted(state_dir.glob("plans/*.json"))
    if not plans:
        raise SystemExit(f"no plans in {state_dir}/plans")
    dates = [p.stem for p in plans]
    if date is not None:
        if date not in dates:
            raise SystemExit(f"no plan for {date} in {state_dir}/plans; have {', '.join(dates)}")
        dates = [date]
    bank = load_json(state_dir / "question_bank.json", [])
    days = {d: one_day(state_dir, d, bank) for d in dates}
    cmp = {}
    for c in compare:
        per = {d: compare_day(c, d) for d in dates}
        per = {d: v for d, v in per.items() if v}
        if per:
            cmp[c.name] = per
    data = {"run": state_dir.name, "order": dates, "days": days, "compare": cmp}
    html = TEMPLATE.read_text().replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False))
    out = state_dir / "timeline.html"
    out.write_text(html)
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("state_dir", help="a run folder, e.g. state_nav/model-demo3")
    p.add_argument("--date", default=None, help="one day only; default every day in the folder")
    p.add_argument("--compare", nargs="*", default=[], help="other run folders of the same dates to overlay")
    p.add_argument("--open", action="store_true", help="open in the browser")
    a = p.parse_args()
    out = build(Path(a.state_dir), a.date, [Path(c) for c in a.compare])
    print(out)
    if a.open:
        subprocess.run(["open", str(out)], check=False)


if __name__ == "__main__":
    main()
