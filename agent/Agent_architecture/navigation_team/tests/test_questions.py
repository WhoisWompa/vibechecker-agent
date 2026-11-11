"""Questions to memory are about the kind of situation, in memory's own words."""

import json
import sys
from pathlib import Path

NAV = Path(__file__).resolve().parents[1]
if str(NAV) not in sys.path:
    sys.path.insert(0, str(NAV))

import checklist  # noqa: E402
import notes as notes_mod  # noqa: E402
from questions import is_well_formed, note_question, shape, strip_particulars  # noqa: E402


def test_notes_map_to_the_question_memory_holds():
    assert note_question("big group of 12 at 3pm-5pm") == "what should be done about a big group booked in this cafe?"
    assert note_question("table for 8 booked, 7pm") == note_question("birthday party at 6pm")
    assert note_question("heavy showers expected after 2pm") == "what do people want from this room when it rains?"
    assert note_question("closing early, last order 8pm") == "what should be done when the cafe closes early?"
    assert note_question("live music from 7pm") == "what should be done when there is an event in this cafe?"
    assert note_question("aircon servicing 10am-12pm") == "what should be done during maintenance or a delivery in this cafe?"
    assert note_question("override 6pm-10pm light=300 volume=45: quieter evenings") == note_question("quiet evening")


def test_unknown_notes_lose_their_particulars():
    q = note_question("photographer coming for 45 minutes at 4pm on 2024-04-05")
    assert q.startswith("what should be done in this cafe when the owner says")
    ok, why = is_well_formed(q)
    assert ok, why
    assert "4pm" not in q and "45" not in q and "2024" not in q
    assert strip_particulars("big group of 12 at 3pm-5pm") == "big group"
    assert note_question("12") == "what should be done about an unusual day in this cafe?"


def test_well_formed_rejects_instances_and_compound_questions():
    assert is_well_formed("what should be done about this in this cafe: big group of 12?")[0] is False
    assert is_well_formed("what is a Friday like, and what settings suit it, and how loud?")[0] is False
    assert is_well_formed("big group at 3pm")[0] is False
    assert is_well_formed("what settings suit a Friday in this cafe?") == (True, "")
    assert shape("  What works on hot days ") == "what works on hot days?"
    assert shape("does a group stay longer with quieter music?") == "does a group stay longer with quieter music?"


def test_every_templated_question_is_well_formed():
    items = checklist.build_items("2024-01-05", "Friday", ["public holiday: X", "school holiday: Y"],
                                  {"forecast_text": "Thundery Showers", "temperature_low": 24, "temperature_high": 34},
                                  [notes_mod.make_note("big group of 12 at 3pm-5pm"), notes_mod.make_note("closing early, last order 8pm"),
                                   notes_mod.make_note("photographer at 4pm for 45 minutes")])
    for it in items:
        if it["question"]:
            ok, why = is_well_formed(it["question"])
            assert ok, f"{it['question']!r}: {why}"
    assert all("note" in it for it in items if it["item"].startswith("Owner's note"))


def test_seeded_facts_use_the_questions_the_team_asks():
    from seed_memory import FACTS

    seeded = {f[0] for f in FACTS}
    asked = {note_question("big group of 12 at 3pm-5pm"), note_question("showers later"), note_question("very hot afternoon"),
             note_question("closing early today"), note_question("live music tonight"), note_question("aircon servicing"),
             note_question("school holiday crowd"), note_question("quiet day expected"), note_question("promo day"),
             "what settings suit a Friday in this cafe?", "what settings suit this cafe on a public holiday?",
             "what settings work on a fair & warm day?"}
    assert asked <= seeded, asked - seeded
    for q in seeded:
        ok, why = is_well_formed(q)
        assert ok, f"{q!r}: {why}"


def test_door_answers_a_word_for_word_repeat_without_a_model(tmp_path):
    import memory_door
    from seed_memory import seed

    mem_path = tmp_path / "memory.json"
    seed(str(mem_path))
    door = memory_door.MemoryDoor(None, str(tmp_path / "bank.json"), str(mem_path))  # no model at all
    out = door.ask_all([note_question("big group of 12 at 3pm-5pm"),      # r1, tested true
                        "What works in this room on a very hot day?",       # r3, inconclusive (case differs)
                        "what settings suit a Monday in this cafe?"],       # nothing in memory
                       "daily", "2024-04-05", 0, "test")
    assert [o["known"] for o in out] == [True, False, False]
    assert "music down" in out[0]["answer"] and out[0]["how"] == "exact"
    assert out[1]["answer"].startswith("not yet confirmed") and out[1]["matched_node"] == "r3"
    assert out[2]["answer"] == "no evidence yet"
    bank = door.bank.load()
    assert [e["tag"] for e in bank] == ["urgency bump", "new gap"]
    assert "error" not in bank[1] and bank[1]["note"] == "no model to judge similar memories"


def test_door_remembers_todays_answers_and_forgets_them_next_morning(tmp_path):
    import memory_door
    from seed_memory import seed

    seed(str(tmp_path / "memory.json"))
    free = []

    class Recorder:
        def context(self, **kw):
            pass

        def free(self, question, how="exact"):
            free.append((how, question))

    door = memory_door.MemoryDoor(None, str(tmp_path / "bank.json"), str(tmp_path / "memory.json"), tracker=Recorder())
    q_gap, q_hit = "what settings suit a Monday in this cafe?", "what do people want from this room when it rains?"
    first = door.ask_all([q_gap, q_hit], "intraday", "2024-04-05", 150, "check")
    second = door.ask_all([q_gap, q_hit], "intraday", "2024-04-05", 165, "check")
    assert [o["known"] for o in first] == [False, True] and [o["known"] for o in second] == [False, True]
    assert second[0]["how"] == "cached" and second[1]["how"] == "cached" and second[0]["t"] == 165
    assert len(door.bank.load()) == 1  # the gap was banked once, not twice
    assert free == [("exact", q_hit), ("cached", q_gap), ("cached", q_hit)]
    door.new_day()
    third = door.ask(q_gap, "daily", "2024-04-06", 0, "morning")
    assert "how" not in third and len(door.bank.load()) == 2  # asked afresh the next morning


def test_model_note_questions_are_asked_only_when_the_template_finds_nothing(tmp_path):
    from types import SimpleNamespace

    import memory_door
    from daily_agent import build_daily_graph, with_window
    from notes import NoteBook
    from seed_memory import seed

    assert with_window({"from": 420, "to": 540})["window"] == "15:00-17:00"
    assert with_window({"from": None, "to": None})["window"] == "all day"

    seed(str(tmp_path / "memory.json"))
    door = memory_door.MemoryDoor(None, str(tmp_path / "bank.json"), str(tmp_path / "memory.json"))
    assert door.knows("what should be done about a big group booked in this cafe?") and not door.knows("what suits a Monday?")
    asked_model = []

    class FakeDaily:
        def questions_for_note(self, note):
            asked_model.append(note["text"])
            return ["What lighting suits a photo shoot in this cafe?"]

        def plan_day(self, *a, **k):
            raise RuntimeError("no model in this test")  # the plan falls back to the baseline

    nb = NoteBook(str(tmp_path))
    nb.add("2024-04-05", "big group of 12 at 3pm-5pm")   # template hits memory word for word: no model questions
    nb.add("2024-04-05", "photographer at 4pm")           # nothing in memory: the model may phrase questions
    ctx = SimpleNamespace(date="2024-04-05", weekday="Friday", capacity=30, state_dir=str(tmp_path),
                          db_path=str(tmp_path / "history.db"), notebook=nb, memory_door=door, daily_agent=FakeDaily(),
                          budget_ok=lambda: True, forecast24=lambda: {"forecast_text": "Fair & Warm"}, calendar=lambda: [],
                          log=lambda m: None)
    out = build_daily_graph(ctx).invoke({})
    assert asked_model == ["photographer at 4pm"]
    questions = [a["question"] for a in out["memory_answers"]]
    assert "what should be done about a big group booked in this cafe?" in questions
    assert "what lighting suits a photo shoot in this cafe?" in questions  # shaped: lower-case start
    assert out["mode"] == "heuristic-fallback"


def test_a_heuristic_day_banks_only_well_formed_questions(tmp_path):
    from runner import NavRun

    NavRun(days=1, seed=3, start_date="2024-04-05", state_dir=str(tmp_path), mode="heuristic",
           notes=["big group of 12 at 3pm-5pm"], messages=["t=390: a school group may come at 4pm"], verbose=False,
           cafe_knobs={"preset": "demo"}).run()
    bank = json.loads((tmp_path / "question_bank.json").read_text())
    assert bank
    for entry in bank:
        ok, why = is_well_formed(entry["question"])
        assert ok, f"{entry['question']!r}: {why}"
    assert any(e["question"] == "what should be done about a big group booked in this cafe?" for e in bank)
    daily = [e for e in bank if e["asked_by"] == "daily"]
    assert daily and "big group of 12" in daily[0]["situation"]  # the instance rides in the situation, not the question
