"""After close: the bridge between the navigation team and the analysis team.

The navigation team runs the room by day and leaves three things in the state
folder: the question bank (what memory could not answer), the master record
(``history.db``, every minute) and the checklist of habits. The analysis team
works after close. This module is the one place where the two meet; the
memory agent is the door in both directions.

One pass, for one date:

1. read the open questions in the bank (distinct, budget stops and door
   errors are not gaps);
2. scan the master record for statistical flags (plain code) and let the
   reflection agent turn each flag into a plain question and ask memory;
3. hand every question memory cannot answer to Lionel's research pass
   (hypothesis agent -> supervisor -> researcher over the master record),
   which writes its verdicts into the memory file;
4. re-ask memory each flag question and let the reflection agent update the
   checklist the daily agent reads next morning;
5. re-ask memory each open bank question; the ones it can now answer are
   marked ``answered`` in the bank, with the answer and the date.

A report for the pass is written to ``<state>/analysis/<date>.json`` and one
line is appended to ``<state>/analysis/passes.jsonl``. The research pass's own
event log (every prompt and reply, for the live view) goes to
``<state>/analysis/run_<date>/events.jsonl``.

Nothing here touches the simulation. A crash inside the analysis never takes
the navigation run down: it is written into the report and the day goes on.
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

_ARCH = Path(__file__).resolve().parent
for _p in (_ARCH, _ARCH / "memory_agent", _ARCH / "reflection_agent", _ARCH / "analysis_team"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from memory import ExperienceMemory  # noqa: E402
import run_log  # noqa: E402
import scanner  # noqa: E402
from memory_agent import MemoryAgent  # noqa: E402
from reflection_agent import ReflectionAgent  # noqa: E402

NO_EVIDENCE = "no evidence yet"
MAX_FLAGS = 5          # scanner flags handed to the reflection agent per pass (strongest first)
MAX_QUESTIONS = 12     # distinct bank questions researched per pass


def _norm(q: str) -> str:
    return " ".join(str(q or "").lower().split()).rstrip("?").strip()


def open_questions(bank: List[Dict[str, Any]], limit: Optional[int] = None) -> List[str]:
    """Distinct open questions in the order they were first asked.

    A ``budget`` tag means the door was out of budget, not that memory had no
    answer; an ``error`` key means the door itself failed. Neither is a gap
    for the analysis team."""
    seen = set()
    out: List[str] = []
    for e in bank:
        if e.get("status", "open") != "open":
            continue
        if e.get("tag") == "budget" or e.get("error"):
            continue
        q = " ".join(str(e.get("question", "")).split())
        key = _norm(q)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(q)
    if limit is not None:
        out = out[:limit]
    return out


def _load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _checklist_lines(path: Path) -> List[str]:
    if not path.exists():
        return []
    return [l[2:].strip() for l in path.read_text(encoding="utf-8").splitlines() if l.startswith("- ")]


class AfterClose:
    """One after-close pass over a navigation state folder.

    ``llm_factory(agent_name) -> chat model`` builds the models (``llm.get_llm``
    in a real run; a stub in tests). ``research(questions, db_path,
    memory_path) -> dict`` is the research pass (``workflow.run_research_pass``
    by default)."""

    def __init__(self, state_dir: str, memory_path: Optional[str] = None, db_path: Optional[str] = None,
                 bank_path: Optional[str] = None, checklist_path: Optional[str] = None,
                 llm_factory: Optional[Callable[[str], Any]] = None,
                 research: Optional[Callable[[List[str], str, str], Dict[str, Any]]] = None,
                 max_questions: int = MAX_QUESTIONS, max_flags: int = MAX_FLAGS,
                 log: Optional[Callable[[str], None]] = None) -> None:
        self.state_dir = Path(state_dir)
        self.memory_path = Path(memory_path) if memory_path else self.state_dir / "memory.json"
        self.db_path = Path(db_path) if db_path else self.state_dir / "history.db"
        self.bank_path = Path(bank_path) if bank_path else self.state_dir / "question_bank.json"
        self.checklist_path = Path(checklist_path) if checklist_path else self.state_dir / "checklist.md"
        self.out_dir = self.state_dir / "analysis"
        self.max_questions = max_questions
        self.max_flags = max_flags
        self._llm_factory = llm_factory
        self._research = research
        self.log = log or (lambda msg: print("  " + msg))

    # -- pieces, each replaceable in tests --

    def llm(self, agent: str):
        if self._llm_factory is None:
            from llm import get_llm
            self._llm_factory = get_llm
        return self._llm_factory(agent)

    def research(self, questions: List[str], db_path: str, memory_path: str) -> Dict[str, Any]:
        if self._research is None:
            from workflow import run_research_pass
            self._research = run_research_pass
        return self._research(questions, db_path, memory_path)

    def memory_agent(self, log_name: str = "reflection_log.json") -> MemoryAgent:
        """A fresh memory agent over the memory file as it is on disk right now."""
        mem = ExperienceMemory.load(str(self.memory_path)) if self.memory_path.exists() else ExperienceMemory()
        return MemoryAgent(self.llm("memory"), mem, str(self.out_dir / log_name))

    def flags(self) -> List[Dict[str, Any]]:
        if not self.db_path.exists():
            return []
        try:
            return scanner.scan(str(self.db_path))[: self.max_flags]
        except Exception as exc:  # a scanner failure is a report line, not a stop
            self.log(f"scanner failed: {exc!r}"[:200])
            return []

    # -- the pass --

    def run(self, date: str) -> Dict[str, Any]:
        t0 = time.time()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        bank = _load_json(self.bank_path, [])
        questions = open_questions(bank, self.max_questions)
        report: Dict[str, Any] = {"date": date, "questions": list(questions), "flags": [], "flag_items": [],
                                  "research": None, "answered": [], "still_open": [],
                                  "checklist_before": _checklist_lines(self.checklist_path)}
        self.log(f"after close {date}: {len(questions)} open question(s) in the bank")

        # 2. flags -> questions -> memory (the reflection agent, before the run)
        flags = self.flags()
        report["flags"] = [f.get("text", "") for f in flags]
        items: List[Dict[str, Any]] = []
        reflection: Optional[ReflectionAgent] = None
        if flags:
            try:
                reflection = ReflectionAgent(self.llm("reflection"), self.memory_agent(), str(self.checklist_path))
                items = reflection.before_run(flags)
            except Exception as exc:
                self.log(f"reflection agent (before) failed: {exc!r}"[:200])
                report["reflection_error"] = traceback.format_exc()[-1500:]
                items = []
            known = {_norm(q) for q in questions}
            for it in items:
                if it["answer"].strip().lower() == NO_EVIDENCE and _norm(it["question"]) not in known:
                    questions.append(it["question"])
                    known.add(_norm(it["question"]))
            self.log(f"{len(flags)} flag(s) from the master record, "
                     f"{sum(1 for it in items if it['answer'].strip().lower() == NO_EVIDENCE)} unexplained")
        report["flag_items"] = [{"flag": it["flag"].get("text", ""), "question": it["question"],
                                 "answer_before": it["answer"]} for it in items]
        report["questions"] = list(questions)

        # 3. the research pass over the master record; verdicts land in the memory file
        if questions:
            run_dir = self.out_dir / f"run_{date}"
            try:
                run_log.start_run(run_dir)
                run_log.event("note", f"after close {date}: questions fed in:\n" + "\n".join(questions))
                summary = self.research(questions, str(self.db_path), str(self.memory_path))
                report["research"] = summary
                run_log.event("note", f"run finished: {summary}")
                self.log(f"research pass: {len(summary.get('hypotheses', {}))} hypothesis(es) in memory, "
                         f"{len(summary.get('nodes_tested', []))} node(s) tested, "
                         f"{summary.get('discards', 0)} discarded")
            except Exception as exc:
                self.log(f"research pass failed: {exc!r}"[:200])
                report["research_error"] = traceback.format_exc()[-2000:]
                run_log.event("note", "run crashed:\n" + traceback.format_exc())
            finally:
                run_log.set_context(None, None)
                run_log.end_run()  # release events.jsonl so the state folder can be deleted
            report["event_log"] = str(run_dir / "events.jsonl")
        else:
            self.log("nothing to research")

        # 4. and 5. ask memory again, now that the analysis team has written to it
        try:
            agent = self.memory_agent()
        except Exception as exc:
            self.log(f"memory agent failed: {exc!r}"[:200])
            report["memory_error"] = traceback.format_exc()[-1500:]
            agent = None
        if agent is not None and reflection is not None and items:
            try:
                reflection.memory_agent = agent
                reflection.after_run(items)
            except Exception as exc:
                self.log(f"reflection agent (after) failed: {exc!r}"[:200])
                report["reflection_error"] = traceback.format_exc()[-1500:]
        if agent is not None:
            answered, still_open = self._file_answers(bank, agent, date)
            report["answered"], report["still_open"] = answered, still_open
            self.log(f"bank: {len(answered)} question(s) answered, {len(still_open)} still open")

        report["checklist_after"] = _checklist_lines(self.checklist_path)
        report["seconds"] = round(time.time() - t0, 1)
        (self.out_dir / f"{date}.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        with open(self.out_dir / "passes.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"date": date, "questions": len(report["questions"]), "flags": len(flags),
                                "answered": len(report["answered"]), "still_open": len(report["still_open"]),
                                "research_error": bool(report.get("research_error")),
                                "seconds": report["seconds"]}) + "\n")
        return report

    def _file_answers(self, bank: List[Dict[str, Any]], agent: MemoryAgent, date: str):
        """Re-ask every open bank question once; file the answers into the bank."""
        answers: Dict[str, str] = {}
        for q in open_questions(bank, self.max_questions):
            try:
                answers[_norm(q)] = agent.ask(q)
            except Exception as exc:
                self.log(f"memory ask failed for '{q[:60]}': {exc!r}"[:200])
        answered: List[Dict[str, str]] = []
        still_open: List[str] = []
        seen = set()
        for e in bank:
            if e.get("status", "open") != "open":
                continue
            key = _norm(e.get("question", ""))
            ans = answers.get(key)
            e["passes"] = int(e.get("passes", 0)) + 1
            if ans is not None and ans.strip().lower() != NO_EVIDENCE:
                e["status"] = "answered"
                e["answer"] = ans
                e["answered_on"] = date
                if key not in seen:
                    answered.append({"question": e["question"], "answer": ans})
            elif key not in seen and ans is not None:
                still_open.append(e["question"])
            seen.add(key)
        self.bank_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.bank_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(bank, indent=2), encoding="utf-8")
        tmp.replace(self.bank_path)
        return answered, still_open


def run_after_close(state_dir: str, date: str, **kwargs) -> Dict[str, Any]:
    """Convenience: one pass over a state folder."""
    return AfterClose(state_dir, **kwargs).run(date)
