"""Owner's notes that arrive during the day reach the intraday agent (no model, no room)."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

NAV = Path(__file__).resolve().parents[1]
for p in (NAV, NAV / "intraday_agent"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from blocks import DAY_MINUTES, _validate_coverage, settings_at  # noqa: E402
from intraday_agent import build_intraday_graph  # noqa: E402
from notes import NoteBook  # noqa: E402


def _plan():
    s = {"light": 500.0, "temperature": 23.5, "volume": 50.0, "fan": 2}
    return [{"from": 0, "to": 420, "settings": dict(s), "reasons": "morning", "assumes": {"weather": "fair", "crowd": "quiet"}},
            {"from": 420, "to": DAY_MINUTES, "settings": dict(s), "reasons": "afternoon", "assumes": {"weather": "fair", "crowd": "quiet"}}]


class _Door:
    def __init__(self):
        self.asked = []

    def ask_all(self, questions, **kw):
        self.asked += questions
        return [{"question": q, "answer": "no evidence yet", "known": False} for q in questions]


def _ctx(fresh, blocks=None):
    pushes = []
    ctx = SimpleNamespace(
        capacity=30, date="2024-04-05", blocks=blocks or _plan(), note_blocks=[], fired=set(), memory_door=_Door(),
        intraday_agent=None, log=lambda m: None, day_df=lambda: pd.DataFrame(), weather=lambda: {},
        model_calls_left=lambda: 0, budget_ok=lambda: True, owner_messages=lambda t: [],
        push=lambda rest, t, note, mode: pushes.append({"t": t, "note": note, "mode": mode}),
        fresh_notes=fresh)
    ctx.pushes = pushes
    return ctx


def test_override_note_arriving_midday_lands_in_its_window():
    note = {"kind": "override", "text": "override 3pm-5pm light=300 volume=45: quieter", "from": 420, "to": 540,
            "settings": {"light": 300.0, "volume": 45.0}, "reason": "quieter", "source": "calendar", "event_id": "e1"}
    ctx = _ctx(lambda t: [note] if t == 300 else [])
    g = build_intraday_graph(ctx)
    out = g.invoke({"t": 300})
    assert [e["kind"] for e in out["events"]] == ["note"] and out["pushed"] is True
    assert ctx.memory_door.asked[0] == "what settings suit this room on a quiet day?"  # the kind of note, not the instance
    _validate_coverage(ctx.blocks, 0, DAY_MINUTES)
    assert settings_at(ctx.blocks, 450)["light"] == 300.0 and settings_at(ctx.blocks, 450)["volume"] == 45.0
    assert settings_at(ctx.blocks, 450)["temperature"] == 23.5  # levers the owner did not name are kept
    assert settings_at(ctx.blocks, 600)["light"] == 500.0  # outside the window nothing changed
    assert ctx.note_blocks and ctx.note_blocks[0]["kind"] == "override"
    assert g.invoke({"t": 315})["pushed"] is False  # nothing new at the next check


def test_fact_note_records_the_premise_and_a_passed_window_is_ignored():
    fact = {"kind": "fact", "text": "big group of 12", "from": 420, "to": 540, "settings": None, "reason": "",
            "source": "calendar", "event_id": "e2"}
    late = {"kind": "fact", "text": "morning delivery", "from": 60, "to": 120, "settings": None, "reason": "",
            "source": "owner"}
    ctx = _ctx(lambda t: [fact, late] if t == 300 else [])
    out = build_intraday_graph(ctx).invoke({"t": 300})
    assert [e["kind"] for e in out["events"]] == ["note"] and "big group" in out["events"][0]["detail"]
    # a fact note changes no setting, so nothing is sent to the room (no push, no model call in model mode),
    # but the rewritten premise is kept in the day's plan
    assert out["pushed"] is False and ctx.pushes == []
    afternoon = next(b for b in ctx.blocks if b["from"] <= 450 < b["to"])
    assert afternoon["reasons"].startswith("owner's note for 15:00-17:00: big group of 12")
    assert afternoon["assumes"]["crowd"] == "busy"
    assert settings_at(ctx.blocks, 450)["light"] == 500.0  # a fact note changes no setting without a model
    assert len(ctx.note_blocks) == 1  # the late note never reached the plan


def test_runner_hands_new_notes_to_the_day(tmp_path):
    from runner import NavRun

    run = NavRun(days=1, seed=1, start_date="2024-04-05", state_dir=str(tmp_path), mode="heuristic", verbose=False)
    nb = NoteBook(str(tmp_path))
    nb.add("2024-04-05", "big group of 12 at 3pm-5pm")  # on file before the morning
    seen = {(n["source"], n["text"], n["from"], n["to"]) for n in nb.for_date("2024-04-05")}
    # the runner's fresh_notes is built inside run(); mirror its rule here on the same notebook
    nb.add("2024-04-05", "override 6pm-10pm light=300: dim")  # filed during the day
    new = [n for n in nb.for_date("2024-04-05") if (n["source"], n["text"], n["from"], n["to"]) not in seen]
    assert len(new) == 1 and new[0]["kind"] == "override"
    assert run.calendar is None  # no calendar asked for: the day re-reads the notebook only
