"""Daily planning agent: builds the full day's settings blocks and notes.

Two parts:
- ``DailyAgent``: the one model call (plus one repair retry) that turns the
  baseline, the checklist, the owner's notes and the memory answers into
  the day's blocks. Self-contained; everything arrives as parameters.
- ``build_daily_graph(ctx)``: the morning as a LangGraph. Every node except
  ``plan`` is plain code: gather the facts, build the checklist and the
  baseline, ask the memory agent, then plan, validate and write the plan
  file the intraday agent reads.

Without a model (``ctx.daily_agent is None``) or when the model call fails,
``heuristic_plan`` flies the baseline with the owner's overrides applied,
and the plan says so in every reason.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict

from pydantic import BaseModel

_DIR = Path(__file__).parent


def _prompt(name: str) -> str:
    return (_DIR / f"{name}.md").read_text(encoding="utf-8")


_NAV_TEAM_DIR = str(Path(__file__).resolve().parent.parent)
if _NAV_TEAM_DIR not in sys.path:
    sys.path.insert(0, _NAV_TEAM_DIR)

from blocks import (  # noqa: E402
    DAY_MINUTES,
    NoteBlock,
    SettingsBlock,
    _block_to_dict,
    _note_block_to_dict,
    _validate_coverage,
    _validate_override_windows,
    clean_settings,
    clock,
    enforce_bounds,
    plan_markdown,
    validate_assumes,
    weather_category,
)
from questions import is_well_formed, shape  # noqa: E402


# ---------------------------------------------------------------------------
# Structured-output models
# ---------------------------------------------------------------------------

class NoteQuestions(BaseModel):
    questions: list[str]


class DayPlan(BaseModel):
    settings_blocks: list[SettingsBlock]
    note_blocks: list[NoteBlock]


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class DailyAgent:
    """Builds the full day's settings list and notes."""

    def __init__(self, llm):
        self.llm = llm

    def questions_for_note(self, note: dict) -> list[str]:
        structured_llm = self.llm.with_structured_output(NoteQuestions)
        prompt = _prompt("questions_for_note").format(note=note)
        result: NoteQuestions = structured_llm.invoke(prompt)
        return result.questions

    def plan_day(
        self,
        baseline_blocks: list[dict],
        checklist: str,
        notes: list[dict],
        memory_answers: list[dict],
        date: str = "",
        weekday: str = "",
        calendar: Optional[list] = None,
        forecast: str = "",
        baseline_source: str = "seed",
        capacity: int = 30,
    ) -> dict:
        structured_llm = self.llm.with_structured_output(DayPlan)
        prompt = _prompt("plan_day").format(
            capacity=capacity, date=date, weekday=weekday,
            calendar=", ".join(calendar or []) or "ordinary day", forecast=forecast,
            baseline_source=baseline_source, baseline_blocks=json.dumps(baseline_blocks),
            checklist=checklist, notes=json.dumps([with_window(n) for n in notes]),
            memory_answers=json.dumps(memory_answers),
        )
        last_error: Optional[Exception] = None
        for attempt in range(2):
            ask = prompt if attempt == 0 else (
                prompt + f"\n\nYour previous answer was rejected: {last_error}. Fix exactly that and answer again.")
            try:
                result: DayPlan = structured_llm.invoke(ask)
            except Exception as exc:  # a malformed answer gets one more try with the error shown
                last_error = exc
                continue
            settings_blocks = [_block_to_dict(b) for b in result.settings_blocks]
            note_blocks = [_note_block_to_dict(n) for n in result.note_blocks]
            for b in settings_blocks:
                b["settings"] = clean_settings(b["settings"])
            try:
                _validate_coverage(settings_blocks, 0, DAY_MINUTES)
                enforce_bounds(settings_blocks)
                for n in note_blocks:
                    if n.get("settings"):
                        enforce_bounds([{"from": n.get("from", 0), "settings": n["settings"]}])
                        n["settings"] = clean_settings(n["settings"])
                _validate_override_windows(settings_blocks, note_blocks)
            except ValueError as exc:
                last_error = exc
                continue
            validate_assumes(settings_blocks)
            enforce_bounds(settings_blocks)
            return {"settings_blocks": settings_blocks, "note_blocks": note_blocks}
        raise ValueError(f"plan rejected twice: {str(last_error)[:200]}")


# ---------------------------------------------------------------------------
# No-model path
# ---------------------------------------------------------------------------

def with_window(note: dict) -> dict:
    """The note with its window spelled as clock time, so the model never converts minutes itself."""
    lo, hi = note.get("from"), note.get("to")
    window = f"{clock(lo)}-{clock(hi)}" if lo is not None and hi is not None else "all day"
    return {**note, "window": window}


def apply_overrides(blocks: List[dict], notes: List[dict]) -> List[dict]:
    """Cut the blocks at each override window and drop the owner's settings in."""
    out = [dict(b) for b in blocks]
    for n in notes:
        if n.get("kind") != "override" or not n.get("settings"):
            continue
        lo, hi = int(n["from"]), int(n["to"])
        base = clean_settings({**out[0]["settings"], **{}})
        pieces: List[dict] = []
        for b in out:
            if b["to"] <= lo or b["from"] >= hi:
                pieces.append(b)
                continue
            if b["from"] < lo:
                pieces.append({**b, "to": lo})
            if b["to"] > hi:
                pieces.append({**b, "from": hi})
            base = clean_settings(b["settings"])
        full = clean_settings({**base, **n["settings"]})
        pieces.append({"from": lo, "to": hi, "settings": full,
                       "reasons": f"owner's override: {n.get('reason') or n.get('text', '')}",
                       "assumes": {"weather": "fair", "crowd": "moderate"}})
        out = sorted(pieces, key=lambda b: b["from"])
    return out


def heuristic_plan(baseline_blocks: List[dict], notes: List[dict], forecast_text: str = "") -> dict:
    blocks = [dict(b, settings=clean_settings(b["settings"]), reasons="heuristic: " + b["reasons"],
                   assumes={**(b.get("assumes") or {}), "weather": weather_category(forecast_text)})
              for b in baseline_blocks]
    blocks = apply_overrides(blocks, notes)
    validate_assumes(blocks)
    enforce_bounds(blocks)
    _validate_coverage(blocks, 0, DAY_MINUTES)
    note_blocks = [{"from": n.get("from"), "to": n.get("to"), "kind": n.get("kind", "fact"),
                    "fact": n.get("text", ""), "settings": n.get("settings"), "reason": n.get("reason", ""),
                    "source": n.get("source", "owner")} for n in notes]
    return {"settings_blocks": blocks, "note_blocks": note_blocks}


def previous_baseline(state_dir: str, today: str) -> Optional[List[dict]]:
    """Yesterday's baseline table: the newest plan file before ``today`` that recorded one."""
    plans = Path(state_dir) / "plans"
    if not plans.exists():
        return None
    for p in sorted(plans.glob("*.json"), reverse=True):
        if p.stem >= today:
            continue
        try:
            base = json.loads(p.read_text(encoding="utf-8")).get("baseline")
        except (OSError, ValueError):
            continue
        if base:
            return base
    return None


# ---------------------------------------------------------------------------
# The morning as a graph
# ---------------------------------------------------------------------------

class MorningState(TypedDict, total=False):
    date: str
    weekday: str
    calendar: List[str]
    forecast24: Dict[str, Any]
    notes: List[dict]
    habits: List[str]
    items: List[dict]
    checklist: str
    baseline: List[dict]
    baseline_source: str
    memory_answers: List[dict]
    plan: dict
    mode: str
    errors: List[str]
    plan_path: str


def build_daily_graph(ctx):
    """ctx provides: date, weekday, capacity, state_dir, db_path, notebook, memory_door,
    daily_agent (or None), budget_ok() -> bool, forecast24() -> dict, calendar() -> list[str], log(msg)."""
    from langgraph.graph import END, START, StateGraph

    import baseline as baseline_mod
    import checklist as checklist_mod

    def gather(state: MorningState) -> MorningState:
        forecast24 = ctx.forecast24()
        notes = ctx.notebook.for_date(ctx.date)
        calendar = ctx.calendar()
        habits = checklist_mod.standing_habits(ctx.state_dir)
        items = checklist_mod.build_items(ctx.date, ctx.weekday, calendar, forecast24, notes)
        base, source = baseline_mod.baseline_blocks(ctx.db_path, ctx.weekday, forecast24, capacity=ctx.capacity,
                                                    previous=previous_baseline(ctx.state_dir, ctx.date))
        ctx.log(f"gather: {ctx.weekday} {ctx.date}, forecast '{forecast24.get('forecast_text')}', "
                f"{len(notes)} note(s), baseline from {source}, {len(items)} checklist item(s)")
        return {"date": ctx.date, "weekday": ctx.weekday, "calendar": calendar, "forecast24": forecast24,
                "notes": notes, "habits": habits, "items": items,
                "checklist": checklist_mod.render(habits, items), "baseline": base,
                "baseline_source": source, "errors": []}

    def ask_memory(state: MorningState) -> MorningState:
        # Every item carries a templated question in memory's form. With a model, an owner's note may
        # instead raise up to three questions the model phrases (one call per note, budget permitting);
        # a malformed or failed answer falls back to the template.
        questions: List[str] = []
        for it in state["items"]:
            q = it.get("question")
            if not q:
                continue
            # the template question is free when memory holds it word for word; only then is nothing more asked
            if it.get("note") and ctx.daily_agent is not None and ctx.budget_ok() and not ctx.memory_door.knows(q):
                try:
                    if getattr(ctx, "tracker", None) is not None:
                        ctx.tracker.context(purpose="note questions", questions=[it["note"].get("text", "")])
                    phrased = [shape(s) for s in ctx.daily_agent.questions_for_note(it["note"])]
                    good = [s for s in phrased if is_well_formed(s)[0]][:3]
                    ctx.log(f"note questions: model wrote {len(phrased)}, kept {len(good)} for '{it['note'].get('text', '')}'")
                    if good:
                        questions.extend(good)
                        continue
                except Exception as exc:
                    ctx.log(f"note questions: model failed ({exc!r}); using the template")
            questions.append(q)
        questions = list(dict.fromkeys(questions))  # in order, no repeats
        situation = "morning planning" + ("; notes: " + "; ".join(n.get("text", "") for n in state["notes"]) if state["notes"] else "")
        answers = ctx.memory_door.ask_all(questions, asked_by="daily", date=ctx.date, t=0, situation=situation)
        known = sum(1 for a in answers if a["known"])
        ctx.log(f"memory: asked {len(answers)}, answered {known}, banked {len(answers) - known}")
        return {"memory_answers": answers}

    def plan(state: MorningState) -> MorningState:
        forecast_text = str(state["forecast24"].get("forecast_text") or "")
        if ctx.daily_agent is None or not ctx.budget_ok():
            ctx.log("plan: no model" + ("" if ctx.daily_agent is None else " (budget spent)")
                    + ", flying the baseline with overrides")
            return {"plan": heuristic_plan(state["baseline"], state["notes"], forecast_text), "mode": "heuristic"}
        f = state["forecast24"]
        forecast = f"{forecast_text}, outside {f.get('temperature_low')}-{f.get('temperature_high')} C"
        if getattr(ctx, "tracker", None) is not None:
            ctx.tracker.context(purpose="morning plan", questions=[])
        try:
            result = ctx.daily_agent.plan_day(
                state["baseline"], state["checklist"], state["notes"], state["memory_answers"],
                date=state["date"], weekday=state["weekday"], calendar=state["calendar"],
                forecast=forecast, baseline_source=state["baseline_source"], capacity=ctx.capacity)
            ctx.log(f"plan: model wrote {len(result['settings_blocks'])} blocks")
            return {"plan": result, "mode": "model"}
        except Exception as exc:
            ctx.log(f"plan: model failed ({exc!r}); falling back to the baseline")
            return {"plan": heuristic_plan(state["baseline"], state["notes"], forecast_text),
                    "mode": "heuristic-fallback", "errors": state.get("errors", []) + [repr(exc)[:300]]}

    def write(state: MorningState) -> MorningState:
        plans = Path(ctx.state_dir) / "plans"
        plans.mkdir(parents=True, exist_ok=True)
        payload = {"date": state["date"], "weekday": state["weekday"], "mode": state["mode"],
                   "calendar": state["calendar"], "forecast24": state["forecast24"],
                   "baseline_source": state["baseline_source"], "baseline": state["baseline"], "checklist": state["checklist"],
                   "memory_answers": state["memory_answers"], **state["plan"]}
        (plans / f"{state['date']}.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        md = plan_markdown(state["date"], state["weekday"], state["plan"]["settings_blocks"],
                           state["plan"]["note_blocks"],
                           header=f"Mode: {state['mode']}. Forecast: {state['forecast24'].get('forecast_text')}. "
                                  f"Baseline: {state['baseline_source']}.")
        (plans / f"{state['date']}.md").write_text(md, encoding="utf-8")
        return {"plan_path": str(plans / f"{state['date']}.md")}

    g = StateGraph(MorningState)
    g.add_node("gather", gather)
    g.add_node("ask_memory", ask_memory)
    g.add_node("plan", plan)
    g.add_node("write", write)
    g.add_edge(START, "gather")
    g.add_edge("gather", "ask_memory")
    g.add_edge("ask_memory", "plan")
    g.add_edge("plan", "write")
    g.add_edge("write", END)
    return g.compile()
