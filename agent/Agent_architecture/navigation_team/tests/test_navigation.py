"""Navigation team unit tests: no model, no network. The last test drives the
real cafe for one day on the heuristic path."""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

NAV = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NAV))
import _paths  # noqa: E402,F401

import baseline  # noqa: E402
import checklist  # noqa: E402
import notes as notes_mod  # noqa: E402
from blocks import (DAY_MINUTES, _validate_coverage, crowd_category, schedule_df, settings_at,  # noqa: E402
                    splice, weather_category)
from daily_agent import apply_overrides, heuristic_plan  # noqa: E402
from intraday_agent import check, heuristic_replan  # noqa: E402
from tracking import Tracker  # noqa: E402


def _blocks():
    return baseline.seed_baseline({"forecast_text": "Fair", "temperature_high": 31})


def test_seed_baseline_covers_the_day():
    b = _blocks()
    _validate_coverage(b, 0, DAY_MINUTES)
    assert b[-1]["settings"]["light"] < b[0]["settings"]["light"]  # evenings dimmer


def test_rain_and_heat_adjust_the_seed():
    rain = baseline.seed_baseline({"forecast_text": "Thundery Showers", "temperature_high": 30})
    fair = baseline.seed_baseline({"forecast_text": "Fair", "temperature_high": 30})
    hot = baseline.seed_baseline({"forecast_text": "Fair", "temperature_high": 34})
    assert rain[0]["settings"]["temperature"] > fair[0]["settings"]["temperature"]
    assert hot[0]["settings"]["temperature"] < fair[0]["settings"]["temperature"]
    assert rain[0]["assumes"]["weather"] == "rain"


def test_schedule_ramps_and_lands():
    b = _blocks()
    df = schedule_df(b, 0)
    assert len(df) == DAY_MINUTES and list(df.columns) == ["light", "temperature", "volume", "fan"]
    # the evening block (from 600) is 100 lux lower; the ramp takes ~17 minutes at 6 lux/min
    assert df.iloc[601]["light"] > b[5]["settings"]["light"]
    assert df.iloc[640]["light"] == b[5]["settings"]["light"]
    assert df["temperature"].between(18, 30).all()


def test_notes_parse_windows_and_overrides():
    n = notes_mod.make_note("big group of 12 at 3pm-5pm")
    assert n["kind"] == "fact" and (n["from"], n["to"]) == (420, 540)
    o = notes_mod.make_note("override 6pm-10pm light=300 volume=45: quieter evenings")
    assert o["kind"] == "override" and o["settings"] == {"light": 300.0, "volume": 45.0}
    assert (o["from"], o["to"]) == (600, 840) and o["reason"] == "quieter evenings"
    assert notes_mod.parse_window("after 6pm") == (600, 840)
    assert notes_mod.parse_window("at lunch") == (240, 360)
    assert notes_mod.parse_spec("2024-01-05: closing early")[0] == "2024-01-05"


def test_notebook_roundtrip(tmp_path):
    nb = notes_mod.NoteBook(str(tmp_path))
    nb.add("2024-01-05", "big group at 3pm")
    nb.add("2024-01-05", "override after 8pm fan=1")
    got = nb.for_date("2024-01-05")
    assert [n["kind"] for n in got] == ["fact", "override"]
    assert nb.for_date("2024-01-06") == []


def test_checklist_turns_notes_into_questions(tmp_path):
    habits = checklist.standing_habits(str(tmp_path))
    assert habits and (tmp_path / "checklist.md").exists()
    items = checklist.build_items("2024-01-05", "Friday", ["public holiday: X"],
                                 {"forecast_text": "Showers", "temperature_low": 24, "temperature_high": 33},
                                 [notes_mod.make_note("big group at 3pm"), notes_mod.make_note("override after 8pm fan=1")])
    qs = [i["question"] for i in items]
    assert any("Friday" in q for q in qs if q)
    assert any("rains" in q for q in qs if q)
    assert any("big group" in q for q in qs if q)
    assert items[-1]["question"] is None  # overrides need no question
    assert "Rain" in checklist.render(habits, items)


def test_overrides_cut_into_the_plan():
    b = _blocks()
    plan = heuristic_plan(b, [notes_mod.make_note("override 6pm-8pm light=300 volume=45: quiet dinner")], "Cloudy")
    blocks = plan["settings_blocks"]
    _validate_coverage(blocks, 0, DAY_MINUTES)
    assert settings_at(blocks, 650)["light"] == 300 and settings_at(blocks, 650)["volume"] == 45
    assert settings_at(blocks, 650)["fan"] == settings_at(b, 650)["fan"]  # unnamed levers come from the base
    assert settings_at(blocks, 590)["light"] != 300
    assert all(bl["assumes"]["weather"] in ("fair", "cloudy", "rain") for bl in blocks)


def test_categories():
    assert weather_category("Thundery Showers") == "rain"
    assert weather_category("Partly Cloudy (Day)") == "cloudy"
    assert weather_category("Fair (Day)") == "fair"
    assert crowd_category(0.1) == "quiet" and crowd_category(0.4) == "moderate" and crowd_category(0.8) == "busy"


def test_check_fires_once_per_block_and_heuristic_adjusts():
    b = _blocks()
    block = b[1]
    fired = set()
    snap = {"t": 195, "weather": "rain", "forecast_2h": "Light Rain", "crowd": "moderate",
            "occupancy_frac": 0.4, "occupancy_mean_15": 12, "capacity": 30, "co2": 600}
    ev = check(snap, block, fired)
    assert [e["kind"] for e in ev] == ["forecast"]
    assert check(snap, block, fired) == []  # same discrepancy does not fire twice
    snap2 = {**snap, "weather": "rain", "crowd": "busy", "occupancy_frac": 0.8, "co2": 1100, "t": 210}
    ev2 = check(snap2, block, fired)
    assert [e["kind"] for e in ev2] == ["co2"] and "limit" in ev2[0]["detail"]  # crowd seen once, not fired yet
    ev2 += check({**snap2, "t": 225, "co2": 600}, block, fired)  # second consecutive check: fires
    assert sorted(e["kind"] for e in ev2) == ["co2", "crowd"]
    early = check({**snap2, "t": 15, "co2": 600, "weather": "fair"}, b[0], set())
    assert early == []  # warm-up: no crowd verdicts in the first half hour
    rest, note = heuristic_replan(ev + ev2, 225, b)
    _validate_coverage(rest, 225, DAY_MINUTES)
    assert rest[0]["settings"]["fan"] >= block["settings"]["fan"] + 1
    assert rest[0]["assumes"]["weather"] == "rain"
    assert "forecast -> rain" in note
    merged = splice(b, 225, rest)
    _validate_coverage(merged, 0, DAY_MINUTES)


def test_tracker_sums_usage():
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, LLMResult

    tr = Tracker()
    tr.context(day=1, date="2024-01-05", loop="morning")
    tr.on_chat_model_start({}, [[AIMessage(content="hi")]], run_id="r1", metadata={"agent": "daily"})
    msg = AIMessage(content="ok", usage_metadata={"input_tokens": 120, "output_tokens": 30, "total_tokens": 150})
    tr.on_llm_end(LLMResult(generations=[[ChatGeneration(message=msg)]]), run_id="r1")
    assert tr.totals()["total_tokens"] == 150
    row = tr.summary("day,agent")[0]
    assert row["agent"] == "daily" and row["calls"] == 1 and row["input_tokens"] == 120
    assert "daily" in tr.table()


@pytest.mark.slow
def test_one_heuristic_day_on_the_real_cafe(tmp_path):
    from runner import NavRun

    run = NavRun(days=1, seed=3, start_date="2024-01-05", state_dir=str(tmp_path / "state"), mode="heuristic",
                 notes=["big group of 12 at 3pm-5pm", "override 8pm-10pm volume=45: quiet close"],
                 messages=["t=390: a school group may come at 4pm"], verbose=False)
    out = run.run()
    assert len(out) == 1 and out[0]["mode"] == "heuristic"
    plans = tmp_path / "state" / "plans"
    assert (plans / "2024-01-05.json").exists() and (plans / "2024-01-05.md").exists()
    with sqlite3.connect(str(tmp_path / "state" / "history.db")) as conn:
        n, = conn.execute("SELECT COUNT(*) FROM readings").fetchone()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(readings)")]
    assert n == DAY_MINUTES
    for c in ("date", "weekday", "time", "month", "block_from", "block_reason", "set_by"):
        assert c in cols
    bank = json.loads((tmp_path / "state" / "question_bank.json").read_text())
    assert bank and all(q["status"] == "open" for q in bank)
    assert any(q["asked_by"] == "daily" for q in bank)
    assert any(q["asked_by"] == "intraday" for q in bank)  # the t=390 owner message woke it
    summary = json.loads((tmp_path / "state" / "summary.json").read_text())
    assert summary["tokens"]["calls"] == 0
    assert out[0]["rows_recorded"] == DAY_MINUTES


def test_history_baseline_uses_medians_and_observed_crowd(tmp_path):
    import sqlite3 as sq

    db = tmp_path / "history.db"
    with sq.connect(str(db)) as conn:
        conn.execute("CREATE TABLE readings (date TEXT, weekday TEXT, time TEXT, light_set REAL, "
                     "temperature_set REAL, volume REAL, fan INTEGER, occupancy REAL)")
        rows = []
        for day, light in (("2024-01-05", 400), ("2024-01-12", 600)):
            for t in range(DAY_MINUTES):
                occ = 24 if 240 <= t < 360 else 3  # busy lunch, quiet otherwise
                rows.append((day, "Friday", f"{8 + t // 60:02d}:{t % 60:02d}", light, 23.0, 50.0, 2, occ))
        conn.executemany("INSERT INTO readings VALUES (?,?,?,?,?,?,?,?)", rows)
    blocks, source = baseline.baseline_blocks(str(db), "Friday", {"forecast_text": "Fair"}, capacity=30)
    assert source == "history"
    _validate_coverage(blocks, 0, DAY_MINUTES)
    assert blocks[0]["settings"]["light"] in (400, 600)  # a median of two values picks one of them
    assert blocks[2]["assumes"]["crowd"] == "busy" and blocks[0]["assumes"]["crowd"] == "quiet"
    assert baseline.baseline_blocks(str(db), "Monday", {"forecast_text": "Fair"})[1] == "seed"  # <3 days, no Monday


def test_budget_trips_and_stops_model_calls():
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, LLMResult

    tr = Tracker(budget_calls=2, budget_usd=100.0)
    assert tr.within_budget()
    for i in range(2):
        tr.on_chat_model_start({}, [[AIMessage(content="x")]], run_id=f"r{i}", metadata={"agent": "intraday"})
        tr.on_llm_end(LLMResult(generations=[[ChatGeneration(message=AIMessage(
            content="ok", usage_metadata={"input_tokens": 10, "output_tokens": 1, "total_tokens": 11}))]]), run_id=f"r{i}")
    assert not tr.within_budget() and "call budget" in tr.tripped
    assert tr.totals()["attempts"] == 2 and tr.totals()["budget_tripped"]
    # a failed attempt still counts as an attempt
    tr2 = Tracker(budget_calls=1)
    tr2.on_chat_model_start({}, [[AIMessage(content="x")]], run_id="f1", metadata={"agent": "daily"})
    tr2.on_llm_error(RuntimeError("boom"), run_id="f1")
    assert not tr2.within_budget()


def test_undated_message_belongs_to_day_one(tmp_path):
    from runner import NavRun

    run = NavRun(days=1, state_dir=str(tmp_path), mode="fixed", verbose=False,
                 messages=["t=390: today only", "2024-01-06 t=100: dated"])
    assert run._messages_for("2024-01-05", "2024-01-05") == {390: ["today only"]}
    assert run._messages_for("2024-01-06", "2024-01-05") == {100: ["dated"]}
    assert run._messages_for("2024-01-07", "2024-01-05") == {}


def test_comfort_bounds_hold_even_for_overrides():
    from blocks import enforce_bounds

    b = _blocks()
    plan = heuristic_plan(b, [notes_mod.make_note("override 6pm-8pm temperature=18 volume=85: freezing disco")], "Fair")
    s = settings_at(plan["settings_blocks"], 650)
    assert s["temperature"] == 21 and s["volume"] == 70
    moved = enforce_bounds([{"from": 0, "settings": {"light": 500, "temperature": 30, "volume": 30, "fan": 2}}])
    assert len(moved) == 2


def test_new_checks_drift_leaving_empty():
    b = _blocks()
    base = {"weather": "fair", "forecast_2h": "Fair", "crowd": "busy", "occupancy_frac": 0.7,
            "occupancy_mean_15": 12, "capacity": 30, "co2": 850, "settings": {"temperature": 23.5, "light": 500, "volume": 50, "fan": 2}}
    fired = set()
    ev = check({**base, "t": 300, "temp_drift": 2.6}, b[2], fired)
    assert sorted(e["kind"] for e in ev) == ["co2", "temp_drift"] and "target" in [e for e in ev if e["kind"] == "co2"][0]["detail"]
    ev = check({**base, "t": 315, "temp_drift": 0.3, "leave_rate_60": 0.09, "leave_rate_prev": 0.04,
                "occ_min_60": 400, "occ_min_prev": 400}, b[2], fired)
    assert [e["kind"] for e in ev] == ["leaving"]
    ev = check({**base, "t": 795, "crowd": "quiet", "occupancy_frac": 0.0, "occupancy_mean_15": 0, "co2": 500}, b[6], set())
    assert [e["kind"] for e in ev] == ["empty"]
    # cooldown: a push starts a new block at "now", the same discrepancy must not fire again
    fired = set()
    assert [e["kind"] for e in check({**base, "t": 300, "temp_drift": 2.6, "co2": 500}, b[2], fired)] == ["temp_drift"]
    assert check({**base, "t": 315, "temp_drift": 2.6, "co2": 500}, {**b[2], "from": 300}, fired) == []
    assert check({**base, "t": 45, "temp_drift": 4.0, "co2": 500}, b[0], set()) == []  # room still settling at open
    rest, note = heuristic_replan(ev, 795, b)
    assert rest[0]["settings"]["fan"] == 0 and rest[0]["settings"]["volume"] == 40 and "energy" in note
    rest, note = heuristic_replan([{"kind": "temp_drift", "t": 300, "detail": "the room has sat 2.6 C above the 23.5 C setpoint"}], 300, b)
    assert rest[0]["settings"]["temperature"] == b[2]["settings"]["temperature"] - 0.5


def test_patch_overlays_only_the_changed_windows():
    from intraday_agent import apply_patch

    b = _blocks()
    now = 390
    remaining = [x if x["from"] >= now else {**x, "from": now} for x in b if x["to"] > now]
    change = {"from": 420, "to": 540, "settings": {"light": 550, "temperature": 23.5, "volume": 55, "fan": 3},
              "reasons": "school group", "assumes": {"weather": "fair", "crowd": "busy"}}
    rest = apply_patch(remaining, [change], now)
    _validate_coverage(rest, now, DAY_MINUTES)
    assert settings_at(rest, 450)["light"] == 550 and settings_at(rest, 400) == settings_at(b, 400)
    assert settings_at(rest, 600) == settings_at(b, 600)  # untouched stretches survive
    assert apply_patch(remaining, [], now) == remaining


def test_memory_door_batches_questions(tmp_path, monkeypatch):
    import memory_door
    from seed_memory import seed

    mem_path = tmp_path / "memory.json"
    seed(str(mem_path))
    calls = []

    class FakeStructured:
        def __init__(self, schema):
            self.schema = schema

        def with_retry(self, **kwargs):  # Lionel's agents chain .with_retry(stop_after_attempt=3)
            return self

        def invoke(self, prompt):
            calls.append(self.schema.__name__)
            if self.schema is memory_door._MatchAll:
                # question 0 matches r1 (big group, True), question 1 matches r3 (hot, inconclusive), question 2 nothing
                return memory_door._MatchAll(verdicts=[
                    memory_door._Verdict(question_index=0, node_id="r1", verdict="same"),
                    memory_door._Verdict(question_index=1, node_id="r3", verdict="same"),
                    memory_door._Verdict(question_index=2, node_id="r2", verdict="different")])
            if self.schema is memory_door._AnswerAll:
                return memory_door._AnswerAll(answers=[memory_door._Answer(question_index=0, answer="Turn the music down and the air a touch cooler; tested true.")])
            from memory_agent import Level1Judgment, Level2Judgment
            if self.schema is Level1Judgment:  # Lionel's memory agent, level 1: the flat match is not enough
                return Level1Judgment(sufficient=False, answer="")
            if self.schema is Level2Judgment:  # level 2: the hypothesis subgraph says it is still open
                return Level2Judgment(outcome="inconclusive", answer="Tested twice, not settled yet.")
            raise AssertionError(self.schema)

    class FakeLLM:
        def with_structured_output(self, schema):
            return FakeStructured(schema)

    import memory_agent
    monkeypatch.setattr(memory_door, "top_candidates", lambda q, cands, k=5: cands[:3])
    monkeypatch.setattr(memory_agent, "top_matches", lambda q, cands, k=5: [c for c in cands if c["node_id"] == "r3"])
    door = memory_door.MemoryDoor(FakeLLM(), str(tmp_path / "bank.json"), str(mem_path))
    out = door.ask_all(["what should be done about a big group?", "what works on a very hot day?",
                        "what is a Friday like here?"], "daily", "2024-04-05", 0, "test")
    assert [o["known"] for o in out] == [True, True, False]
    assert "music" in out[0]["answer"]
    assert calls.count("_MatchAll") == 1 and calls.count("_AnswerAll") == 1  # two calls for three questions
    bank = door.bank.load()
    assert [q["tag"] for q in bank] == ["urgency bump", "new gap"]


def _readings_db(path, days):
    """days: list of (date, light, temperature_sensor, occ, left_per_minute)."""
    import sqlite3 as sq

    with sq.connect(str(path)) as conn:
        conn.execute("CREATE TABLE readings (date TEXT, weekday TEXT, time TEXT, light_set REAL, temperature_set REAL, "
                     "volume REAL, fan INTEGER, occupancy REAL, left REAL, temperature_sensor REAL)")
        rows = []
        for day, light, felt, occ, left in days:
            for t in range(DAY_MINUTES):
                rows.append((day, "Friday", f"{8 + t // 60:02d}:{t % 60:02d}", light, 23.0, 50.0, 2, occ, left, felt))
        conn.executemany("INSERT INTO readings VALUES (?,?,?,?,?,?,?,?,?,?)", rows)


def test_history_baseline_copies_the_days_where_guests_stayed_longest(tmp_path):
    db = tmp_path / "history.db"
    # three Fridays: guests stayed longest under light 600 and a 24 C room; the plain median would say 500
    _readings_db(db, [("2024-01-05", 400, 22.0, 10, 1.0), ("2024-01-12", 500, 23.0, 10, 0.5),
                      ("2024-01-19", 600, 24.0, 10, 0.1)])
    blocks, source = baseline.baseline_blocks(str(db), "Friday", {"forecast_text": "Fair"}, capacity=30)
    assert source == "history"
    # TOP_DAYS is 3, so all three vote, weighted by stay: the longest stay wins the weighted median
    assert blocks[2]["settings"]["light"] == 600
    # the vote is on the room guests felt (24 C), turned into a setpoint by the learned offset (room ran 0 C off),
    # then today's probe: three days on record is an odd day, so the whole day tries PROBE_STEP cooler
    assert blocks[2]["settings"]["temperature"] == round(24.0 - baseline.PROBE_STEP, 1)
    assert blocks[2]["probe"] == {"temperature": -baseline.PROBE_STEP}
    assert "stayed longest" in blocks[2]["reasons"]


def test_history_baseline_ignores_near_empty_stretches(tmp_path):
    db = tmp_path / "history.db"
    _readings_db(db, [("2024-01-05", 900, 22.0, 0.1, 0.0)])  # 12 occupant-minutes per stretch: nobody was there
    blocks, source = baseline.baseline_blocks(str(db), "Friday", {"forecast_text": "Fair"}, capacity=30)
    assert source == "history"
    assert blocks[0]["settings"]["light"] == baseline.SEED["light"]
    assert "seed room" in blocks[0]["reasons"]


def test_cap_drift_steps_from_yesterday():
    today = [{"from": 0, "to": 120, "settings": {"light": 800.0, "temperature": 25.0, "volume": 50.0, "fan": 4},
              "reasons": "baseline", "assumes": {}}]
    yesterday = [{"settings": {"light": 500.0, "temperature": 23.0, "volume": 50.0, "fan": 2}}]
    out = baseline.cap_drift(today, yesterday)
    assert out[0]["settings"] == {"light": 550.0, "temperature": 23.5, "volume": 50.0, "fan": 3}
    assert "stepped from yesterday" in out[0]["reasons"]
    fresh = [{"from": 0, "to": 120, "settings": {"light": 800.0, "temperature": 25.0, "volume": 50.0, "fan": 4}, "reasons": ""}]
    assert baseline.cap_drift(fresh, None)[0]["settings"]["light"] == 800.0  # no yesterday: nothing to step from
