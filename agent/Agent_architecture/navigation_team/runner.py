"""Drive the cafe through days with the navigation team.

Per day:
  morning   the daily graph: gather, checklist, baseline, ask memory, plan, write the plan file
  8am-10pm  the executor loop: push blocks minute by minute; every ``check_every``
            minutes run the intraday graph (observe, detect, ask memory, replan, push)
  close     enrich the day with intent and append it to the master record; summarise

Three modes for the eval:
  model      daily and intraday agents call the model (Haiku through llm.py)
  heuristic  same loop, no model: baseline plan, rule-based adjustments
  fixed      baseline plan only, no intraday checks (the timetable arm)

Everything the team writes lives under ``state_dir``: plans/, notes/, the
checklist, the question bank, history.db (the master record), tokens.jsonl,
messages.jsonl, days/<date>.json and summary.json.
"""

from __future__ import annotations

import datetime
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import _paths  # noqa: F401  (sys.path setup)

import pandas as pd

from blocks import DAY_MINUTES, schedule_df, settings_at, stamp_origin
from daily_agent import DailyAgent, build_daily_graph
from intraday_agent import IntradayAgent, build_intraday_graph
from memory_door import MemoryDoor
from notes import NoteBook, parse_spec
from record import Provenance, record_day
from tracking import Tracker
import sg_calendar

MODES = ("model", "heuristic", "fixed")
ANALYSIS = ("off", "nightly", "weekly")  # when the analysis team runs: never, after every day, after every 7th and the last day
CALENDAR_REFRESH = 60  # minutes between looks at the owner's calendar during the day


def _forecast_row(df: pd.DataFrame) -> Dict[str, Any]:
    if df is None or not len(df):
        return {"forecast_text": None}
    row = df.iloc[-1].to_dict()
    out = {}
    for k, v in row.items():
        if isinstance(v, (pd.Timestamp, datetime.datetime, datetime.date)):
            out[k] = str(v)
        elif hasattr(v, "item"):
            out[k] = v.item()
        else:
            out[k] = v
    return out


class NavRun:
    def __init__(self, days: int = 1, seed: int = 1, start_date: Optional[str] = None,
                 state_dir: str = "state_nav", mode: str = "model", notes: Optional[List[str]] = None,
                 messages: Optional[List[str]] = None, capacity: int = 30, max_model_calls: int = 12,
                 check_every: int = 15, memory_path: Optional[str] = None, verbose: bool = True,
                 cafe_knobs: Optional[Dict[str, Any]] = None, budget_calls: int = 80,
                 budget_usd: float = 1.0, calendar: Optional[str] = None,
                 analysis: str = "off") -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        if analysis not in ANALYSIS:
            raise ValueError(f"analysis must be one of {ANALYSIS}")
        self.days, self.seed, self.start_date = days, seed, start_date
        self.state_dir = str(Path(state_dir))
        Path(self.state_dir).mkdir(parents=True, exist_ok=True)
        self.mode = mode
        self.note_specs = list(notes or [])
        self.message_specs = list(messages or [])
        self.capacity = capacity
        self.max_model_calls = max_model_calls
        self.check_every = check_every
        self.seed_memory_path = memory_path
        # the live memory lives in the state folder: the analysis team rewrites it after close and
        # the door re-reads it every morning; --memory only seeds it on the first run
        self.memory_path = str(Path(self.state_dir) / "memory.json")
        self.analysis = analysis
        self.after_close_kwargs: Dict[str, Any] = {}  # tests plug stub models / a stub research pass in here
        self.analysis_reports: List[Dict[str, Any]] = []
        self.verbose = verbose
        self.cafe_knobs = dict(cafe_knobs or {})
        self.db_path = str(Path(self.state_dir) / "history.db")
        self.tracker = Tracker(self.state_dir, budget_calls=budget_calls, budget_usd=budget_usd)
        self._budget_warned = False
        self.notebook = NoteBook(self.state_dir)
        self.calendar = None  # the owner's Google Calendar, if asked for
        if calendar:
            try:
                from calendar_notes import CalendarSource
                self.calendar = CalendarSource(calendar)
            except Exception as exc:  # no credentials or no client installed: say so once, run on the notes on file
                print(f"  calendar '{calendar}' not available ({str(exc)[:160]}); using the notes on file")
        self.lines: List[str] = []
        self.summaries: List[Dict[str, Any]] = []

    # -- setup --

    def log(self, msg: str) -> None:
        self.lines.append(msg)
        if self.verbose:
            print("  " + msg)

    def budget_ok(self) -> bool:
        ok = self.tracker.within_budget()
        if not ok and not self._budget_warned:
            self._budget_warned = True
            self.log(f"BUDGET STOP: {self.tracker.tripped}; the rest of the run is heuristic")
        return ok

    def _build_models(self):
        if self.mode != "model":
            return None, None, None
        from llm import get_llm
        daily = get_llm("daily", callbacks=[self.tracker])
        intraday = get_llm("intraday", callbacks=[self.tracker])
        memory = get_llm("memory", callbacks=[self.tracker])  # MEMORY_MODEL in agent/.env: the door stays on the cheap model
        return DailyAgent(daily), IntradayAgent(intraday), memory

    def _build_cafe(self):
        from simulation.env import Cafe  # the room; only its public API is used here
        cafe = Cafe(seed=self.seed, capacity=self.capacity, **self.cafe_knobs)
        if self.start_date:
            cafe.set_start_date(self.start_date)
        return cafe

    def _file_notes(self, first_date: str) -> None:
        for spec in self.note_specs:
            date, text = parse_spec(spec)
            note = self.notebook.add(date or first_date, text)
            self.log(f"note for {date or first_date}: [{note['kind']}] {text}")

    def _calendar_notes(self, date: str) -> None:
        """Each morning: the owner's calendar events for the date become today's notes (filed once)."""
        if self.calendar is None:
            return
        try:
            notes = self.calendar.notes_for(date)
            added = sum(1 for n in notes if self.notebook.add_prepared(date, n))
            self.log(f"calendar '{self.calendar.calendar}': {len(notes)} event(s) on {date}, {added} new note(s)")
        except Exception as exc:  # never take the morning down over the calendar
            self.log(f"calendar unavailable ({str(exc)[:160]}); using the notes on file")

    def _messages_for(self, date: str, first_date: str) -> Dict[int, List[str]]:
        """Owner messages for this date. An undated message belongs to day 1, like an undated note."""
        out: Dict[int, List[str]] = {}
        for spec in self.message_specs:
            m = re.match(r"\s*(?:(\d{4}-\d{2}-\d{2})\s+)?t\s*=\s*(\d+)\s*[:|]\s*(.+)$", spec, re.S)
            if not m:
                raise ValueError(f"message spec must look like 't=390: text' or '2024-01-05 t=390: text', got {spec!r}")
            if (m.group(1) or first_date) != date:
                continue
            out.setdefault(int(m.group(2)), []).append(m.group(3).strip())
        return out

    # -- the run --

    def _seed_memory(self) -> None:
        """Copy the seed memory (--memory) into the state folder once; after that the state's own file rules."""
        live = Path(self.memory_path)
        seed = Path(self.seed_memory_path) if self.seed_memory_path else None
        if seed is None or not seed.exists():
            return
        if live.exists():
            if live.resolve() != seed.resolve():
                print(f"  memory: {live} already exists; the seed {seed} is not copied over it")
            return
        live.parent.mkdir(parents=True, exist_ok=True)
        live.write_bytes(seed.read_bytes())
        print(f"  memory: seeded {live} from {seed}")

    def _analysis_due(self, day_number: int) -> bool:
        if self.analysis == "nightly":
            return True
        if self.analysis == "weekly":
            return day_number % 7 == 0 or day_number == self.days
        return False

    def _after_close(self, day_number: int, date: str) -> None:
        """The analysis team's pass over today's bank and master record; verdicts land in memory."""
        from after_close import AfterClose
        import llm as llm_module
        if not self.budget_ok():
            self.log("after close: budget spent, the analysis team does not run tonight")
            return
        self.tracker.context(day=day_number, date=date, loop="after close", purpose="analysis pass")
        llm_module.default_callbacks.append(self.tracker)  # the analysis team's spend counts toward the run budget
        calls_before = len(self.tracker.records)
        try:
            pas = AfterClose(self.state_dir, memory_path=self.memory_path, db_path=self.db_path,
                             log=self.log, **self.after_close_kwargs)
            report = pas.run(date)
        finally:
            if self.tracker in llm_module.default_callbacks:
                llm_module.default_callbacks.remove(self.tracker)
        recs = self.tracker.records[calls_before:]
        brief = {"date": date, "questions": len(report.get("questions", [])), "flags": len(report.get("flags", [])),
                 "answered": len(report.get("answered", [])), "still_open": len(report.get("still_open", [])),
                 "hypotheses": (report.get("research") or {}).get("hypotheses"),
                 "research_error": bool(report.get("research_error")),
                 "model_calls": len(recs), "tokens": sum(r["total_tokens"] for r in recs),
                 "usd": round(sum(r["usd"] for r in recs), 4), "seconds": report.get("seconds")}
        self.analysis_reports.append(brief)
        if self.summaries:
            self.summaries[-1]["analysis"] = brief
        # the day's json is already written: add the pass and its log lines to it
        day_file = Path(self.state_dir) / "days" / f"{date}.json"
        if day_file.exists():
            body = json.loads(day_file.read_text(encoding="utf-8"))
            body["analysis"] = brief
            body["log"] = list(body.get("log", [])) + [l for l in self.lines]
            day_file.write_text(json.dumps(body, indent=2, default=str), encoding="utf-8")
            self.lines = []
        print(f"  after close: {brief['answered']} answered, {brief['still_open']} still open, "
              f"{brief['model_calls']} model calls, {brief['tokens']} tokens, ${brief['usd']:.4f}"
              + (", research pass FAILED (see analysis/)" if brief["research_error"] else ""))

    def run(self) -> List[Dict[str, Any]]:
        cafe = self._build_cafe()
        daily_model, intraday_model, memory_llm = self._build_models()
        self._seed_memory()
        door = MemoryDoor(memory_llm, str(Path(self.state_dir) / "question_bank.json"), self.memory_path,
                          budget_ok=self.budget_ok, tracker=self.tracker)
        first_date = cafe.date.isoformat()
        self._file_notes(first_date)
        print(f"navigation team: mode={self.mode} seed={self.seed} days={self.days} state={self.state_dir} "
              f"budget={self.tracker.budget_calls} calls / ${self.tracker.budget_usd:.2f}")

        for day in range(self.days):
            date = cafe.date.isoformat()
            weekday = cafe.weekday
            self.tracker.context(day=day + 1, date=date, loop="morning")
            print(f"\nday {day + 1}: {weekday} {date}")
            door.new_day()
            self._calendar_notes(date)
            run = self
            morning_ctx = SimpleNamespace(
                date=date, weekday=weekday, capacity=self.capacity, state_dir=self.state_dir,
                db_path=self.db_path, notebook=self.notebook, memory_door=door,
                daily_agent=daily_model if self.mode == "model" else None, budget_ok=self.budget_ok,
                tracker=self.tracker,
                forecast24=lambda: _forecast_row(cafe.latest_forecast_24h()),
                calendar=lambda: sg_calendar.events_for(date),
                log=self.log,
            )
            morning = build_daily_graph(morning_ctx).invoke({})
            plan_blocks: List[dict] = morning["plan"]["settings_blocks"]
            plan_notes: List[dict] = morning["plan"]["note_blocks"]
            stamp_origin(plan_blocks, "daily")  # birth window, stretch and maker of every block
            prov = Provenance(cafe.n)
            prov.stamp(plan_blocks, 0, "daily")
            sched = {"df": schedule_df(plan_blocks, 0, n=cafe.n), "start": 0}
            cafe.set_schedule(sched["df"])
            pushes: List[Dict[str, Any]] = []
            model_calls_at_open = len(self.tracker.records)
            attempts_at_open = self.tracker.starts
            messages = self._messages_for(date, first_date)

            def push(rest: List[dict], t: int, note: str, mode: str) -> None:
                idx = t - sched["start"]
                current = dict(sched["df"].iloc[idx]) if 0 <= idx < len(sched["df"]) else settings_at(plan_blocks, t)
                sched["df"] = schedule_df(plan_blocks, t, current=current, n=cafe.n)
                sched["start"] = t
                cafe.set_schedule(sched["df"])
                prov.stamp(rest, t, "intraday")
                pushes.append({"t": t, "mode": mode, "note": note, "blocks": [dict(b) for b in rest]})

            # notes that arrive after the morning plan: re-read the notebook every check and, when the
            # owner's calendar is on, refetch it every CALENDAR_REFRESH minutes (bounded network calls)
            def note_key(n: dict):
                return n.get("event_id") or (n.get("source"), n.get("text"), n.get("from"), n.get("to"))
            seen_notes = {note_key(n) for n in self.notebook.for_date(date)}

            def fresh_notes(t: int) -> List[dict]:
                if run.calendar is not None and t % CALENDAR_REFRESH == 0:
                    run._calendar_notes(date)
                new = []
                for n in run.notebook.for_date(date):
                    k = note_key(n)
                    if k not in seen_notes:
                        seen_notes.add(k)
                        new.append(n)
                return new

            day_ctx = SimpleNamespace(
                capacity=self.capacity, date=date, blocks=plan_blocks, note_blocks=plan_notes, fired=set(),
                fresh_notes=fresh_notes, tracker=self.tracker, replan_attempts=0,
                pushes=pushes, day_events=[],  # what fired and what was pushed so far today, for the previous-stretch view
                memory_door=door, intraday_agent=intraday_model if self.mode == "model" else None,
                log=self.log, day_df=cafe.day_df, weather=cafe.weather,
                # the day's cap counts replans only (memory questions are cheap and cached); the whole-run
                # budget in the tracker still counts every call
                model_calls_left=lambda: run.max_model_calls - day_ctx.replan_attempts,
                budget_ok=self.budget_ok,
                owner_messages=lambda t: messages.get(t, []),
                push=push,
            )

            check_graph = build_intraday_graph(day_ctx) if self.mode != "fixed" else None
            for t in range(cafe.n):
                if check_graph is not None and t > 0 and t % self.check_every == 0:
                    self.tracker.context(loop=f"check@{t}")
                    check_graph.invoke({"t": t})
                cafe.tick(1)

            df = cafe.day_df()
            rows = record_day(self.db_path, df, cafe.date, prov)
            summary = self._summarise(day + 1, date, weekday, df, morning, pushes, door, model_calls_at_open)
            summary["rows_recorded"] = rows
            if hasattr(cafe, "day_score"):  # the hidden grader's view: for the eval table only, never shown to an agent
                summary["hidden_score"] = cafe.day_score()
            self.summaries.append(summary)
            self._write_day(summary, pushes)
            print(f"  close: entered {summary['entered']}, mean occupancy {summary['mean_occupancy']:.1f}, "
                  f"stay ~{summary['stay_est_min']:.0f} min, co2 peak {summary['co2_peak']:.0f}, "
                  f"replans {summary['replans']}, model calls {summary['model_calls']}, "
                  f"tokens {summary['tokens']}, banked {summary['questions_banked']}"
                  + (f", hidden comfort {summary['hidden_score']['comfort']}" if summary.get("hidden_score") else ""))
            if self._analysis_due(day + 1):
                self._after_close(day + 1, date)
            cafe.next_day()

        self._finish()
        return self.summaries

    # -- reporting --

    def _summarise(self, day, date, weekday, df, morning, pushes, door, calls_at_open) -> Dict[str, Any]:
        num = lambda c: pd.to_numeric(df[c], errors="coerce") if c in df else pd.Series(dtype=float)
        entered = int(num("entered").fillna(0).sum())
        mean_occ = float(num("occupancy").mean() or 0.0)
        stay = mean_occ * len(df) / entered if entered else 0.0
        co2 = num("co2")
        recs = self.tracker.records[calls_at_open:]
        banked = [q for q in door.bank.load() if q.get("date") == date]
        return {
            "day": day, "date": date, "weekday": weekday, "mode": morning["mode"],
            "forecast": morning["forecast24"].get("forecast_text"), "calendar": morning["calendar"],
            "baseline_source": morning["baseline_source"], "blocks": len(morning["plan"]["settings_blocks"]),
            "entered": entered, "mean_occupancy": round(mean_occ, 2),
            "peak_occupancy": int(num("occupancy").max() or 0), "stay_est_min": round(stay, 1),
            "co2_peak": float(co2.max() or 0.0), "co2_over_floor_min": int((co2 > 1000).sum()),
            "temp_mean": round(float(num("temperature_sensor").mean() or 0.0), 2),
            "replans": len(pushes), "replan_modes": [p["mode"] for p in pushes],
            "model_calls": len(recs), "tokens": sum(r["total_tokens"] for r in recs),
            "usd": round(sum(r["usd"] for r in recs), 4),
            "questions_asked": len([a for a in door.asked if a.get("t") is not None]),
            "questions_banked": len(banked),
        }

    def _write_day(self, summary: Dict[str, Any], pushes: List[Dict[str, Any]]) -> None:
        d = Path(self.state_dir) / "days"
        d.mkdir(parents=True, exist_ok=True)
        body = {k: v for k, v in summary.items() if k != "pushes"}
        (d / f"{summary['date']}.json").write_text(
            json.dumps({**body, "pushes": pushes, "log": [l for l in self.lines]}, indent=2, default=str),
            encoding="utf-8")
        self.lines = []

    def _finish(self) -> None:
        totals = self.tracker.totals()
        out = {"mode": self.mode, "seed": self.seed, "analysis": self.analysis, "days": self.summaries, "tokens": totals,
               "analysis_passes": self.analysis_reports,
               "by_day_agent": self.tracker.summary("day,agent"), "by_loop": self.tracker.summary("loop"),
               "by_purpose": self.tracker.summary("purpose"), "by_question": self.tracker.by_question(),
               "free_answers": self.tracker.free_answers}
        (Path(self.state_dir) / "summary.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
        print("\ntokens by day and agent:")
        print(self.tracker.table("day,agent"))
        if self.tracker.records:
            print("\ntokens by purpose:")
            print(self.tracker.table("purpose"))
            print("\nwhat each question cost (batched calls split evenly):")
            for r in out["by_question"][:12]:
                print(f"  {r['total_tokens']:>6} tok  ${r['usd']:.4f}  {r['calls_share']:>4} call(s)  {r['question'][:80]}")
        if self.tracker.free_answers:
            print(f"\nanswered by memory without a model: {len(self.tracker.free_answers)} question(s)")
        print(f"total: {totals['calls']} calls, {totals['total_tokens']} tokens, ${totals['usd']:.4f}, "
              f"{totals['failures']} failed" + (f"; BUDGET STOP: {totals['budget_tripped']}" if totals['budget_tripped'] else ""))
