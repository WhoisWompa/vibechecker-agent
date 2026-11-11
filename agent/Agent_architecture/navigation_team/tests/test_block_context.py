"""Block context for the intraday agent: stretches, block identity, the previous stretch, the change cap."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

NAV = Path(__file__).resolve().parents[1]
for p in (NAV, NAV / "intraday_agent"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from blocks import (CHANGE_CAP, DAY_MINUTES, cap_changes, describe_where, settings_at, splice, stamp_origin,  # noqa: E402
                    stretch_of, where_now)
from intraday_agent import _cool, apply_patch, build_intraday_graph, describe_stretch, previous_stretch  # noqa: E402


def _plan():
    s = {"light": 500.0, "temperature": 23.0, "volume": 50.0, "fan": 2}
    blocks = [{"from": 0, "to": 240, "settings": dict(s), "reasons": "morning", "assumes": {"weather": "fair", "crowd": "quiet"}},
              {"from": 240, "to": 360, "settings": dict(s, fan=3), "reasons": "lunch", "assumes": {"weather": "fair", "crowd": "busy"}},
              {"from": 360, "to": DAY_MINUTES, "settings": dict(s, light=400.0), "reasons": "afternoon on", "assumes": {"weather": "fair", "crowd": "moderate"}}]
    stamp_origin(blocks, "daily")
    return blocks


def _day_df(n=320):
    rows = []
    for t in range(n):
        rows.append({"light_set": 500.0, "temperature_set": 23.0 if t < 255 else 23.5, "volume": 50.0, "fan": 2 if t < 240 else 3,
                     "temperature_sensor": 25.0, "light_sensor": 600.0, "co2": 700.0, "noise": 60.0, "occupancy": 10 if t >= 240 else 4,
                     "entered": 1 if t % 5 == 0 else 0, "left": 1 if t % 7 == 0 else 0})
    return pd.DataFrame(rows)


def test_stretch_and_birth_window_survive_a_push():
    blocks = _plan()
    assert [b["stretch"] for b in blocks] == [0, 2, 3] and blocks[1]["origin"]["by"] == "daily"
    # a push at 13:10 (t=310) cuts the lunch block and patches 310-360 with a new window
    remaining = [b if b["from"] >= 310 else {**b, "from": 310} for b in blocks if b["to"] > 310]
    rest = apply_patch(remaining, [{"from": 310, "to": 360, "settings": {"light": 500.0, "temperature": 22.5, "volume": 50.0, "fan": 3},
                                    "reasons": "cooler", "assumes": {"weather": "fair", "crowd": "busy"}}], 310)
    new = splice(blocks, 310, rest)
    tail = next(b for b in new if b["from"] == 310)
    assert (tail["origin_from"], tail["origin_to"], tail["stretch"]) == (240, 360, 2)  # inherited from the block it cut
    head = next(b for b in new if b["to"] == 310)
    assert (head["origin_from"], head["origin_to"]) == (240, 360)


def test_where_now_at_13_10():
    blocks = _plan()
    w = where_now(blocks, 310)
    assert (w["stretch"], w["stretch_from"], w["stretch_to"]) == (2, 240, 360)
    assert (w["minutes_into_stretch"], w["minutes_left_in_stretch"]) == (70, 50)
    assert (w["block_origin_from"], w["block_origin_to"], w["block_set_by"]) == (240, 360, "daily")
    assert describe_where(w).startswith("13:10: 70 min into the 12:00-14:00 stretch, 50 min left. The block in force (12:00-14:00) is from the morning plan")
    blocks[1]["origin"] = {"by": "heuristic", "at": 255, "note": "temp drift -> setpoint -0.5"}
    assert "made by a heuristic push at 12:15: temp drift" in describe_where(where_now(blocks, 310))


def test_previous_stretch_is_built_from_the_day():
    blocks = _plan()
    pushes = [{"t": 255, "mode": "heuristic", "note": "temp drift -> setpoint +0.5"}, {"t": 100, "mode": "model", "note": "earlier"}]
    events = [{"t": 255, "kind": "temp_drift", "detail": "the room has sat 2.1 C above"}, {"t": 100, "kind": "forecast", "detail": "x"}]
    assert previous_stretch(_day_df(), blocks, pushes, events, 100) is None  # first stretch: nothing before it
    ps = previous_stretch(_day_df(), blocks, pushes, events, 370)  # 14:10: the previous stretch is 12:00-14:00
    assert (ps["stretch"], ps["window"]) == (2, "12:00-14:00")
    assert [b["from"] for b in ps["planned"]] == [240]
    assert ps["applied"]["fan"] == 3 and 23.0 < ps["applied"]["temperature"] < 23.5 and ps["applied"]["light"] == 500
    assert ps["room"]["occupancy"] == 10 and ps["room"]["light"] == 600
    assert [d["kind"] for d in ps["discrepancies"]] == ["temp_drift"] and [p["t"] for p in ps["pushes"]] == [255]
    text = describe_stretch(ps)
    assert "12:00-14:00 (the previous stretch)" in text and "12:15 (heuristic) temp drift" in text and "planned: 12:00-14:00" in text
    assert describe_stretch(None).startswith("none yet")


def test_change_cap_holds_only_what_the_replan_moved_and_spares_overrides():
    before = _plan()
    now = 370
    rest = [{"from": 370, "to": 600, "settings": {"light": 250.0, "temperature": 21.0, "volume": 40.0, "fan": 5},
             "reasons": "big cooldown", "assumes": {"weather": "fair", "crowd": "busy"}},
            {"from": 600, "to": 700, "settings": {"light": 400.0, "temperature": 23.0, "volume": 50.0, "fan": 2},
             "reasons": "unchanged evening", "assumes": {"weather": "fair", "crowd": "moderate"}},
            {"from": 700, "to": DAY_MINUTES, "settings": {"light": 300.0, "temperature": 26.0, "volume": 45.0, "fan": 0},
             "reasons": "owner's override: warm and dim", "assumes": {"weather": "fair", "crowd": "quiet"}}]
    reference = {"light": 500.0, "temperature": 23.2, "volume": 50.0, "fan": 3}  # what the previous stretch ran
    held = cap_changes(rest, before, now, reference)
    assert rest[0]["settings"] == {"light": 400.0, "temperature": 22.2, "volume": 46.0, "fan": 4}
    assert len(held) == 4 and all("held to" in h for h in held)
    assert rest[1]["settings"]["light"] == 400.0 and rest[1]["settings"]["fan"] == 2  # the replan did not move these
    assert rest[2]["settings"]["temperature"] == 26.0 and rest[2]["settings"]["fan"] == 0  # override exempt
    assert CHANGE_CAP["temperature"] == 1.0

    # the default anchor: what the plan already said for that time, so a planned step between stretches
    # is not dragged back (the evening block planned fan 2 and 400 lux; a replan asking fan 4 is held to 3)
    before = _plan()
    rest = [{"from": 370, "to": 600, "settings": {"light": 250.0, "temperature": 21.0, "volume": 50.0, "fan": 4},
             "reasons": "big cooldown", "assumes": {"weather": "fair", "crowd": "busy"}}]
    held = cap_changes(rest, before, 370)
    assert rest[0]["settings"] == {"light": 300.0, "temperature": 22.0, "volume": 50.0, "fan": 3}  # from the plan's 400 / 23 / 50 / 2
    assert len(held) == 3 and "of the plan's" in held[0]


def test_a_kind_fires_at_most_once_per_stretch():
    fired = set()
    assert _cool(fired, "forecast:rain", 250, "forecast") is True
    assert _cool(fired, "forecast:rain", 320, "forecast") is False   # 70 min later: cooldown over, same stretch
    assert _cool(fired, "forecast:rain", 370, "forecast") is True    # next stretch
    assert _cool(fired, "co2:limit", 250, "co2:limit") and _cool(fired, "co2:limit", 320, "co2:limit")  # hourly escalation kept
    assert stretch_of(839) == 6 and stretch_of(0) == 0


def test_graph_caps_a_heuristic_move_against_the_previous_stretch():
    pushes, log = [], []
    blocks = _plan()
    ctx = SimpleNamespace(capacity=30, date="2024-04-05", blocks=blocks, note_blocks=[], fired=set(),
                          memory_door=SimpleNamespace(ask_all=lambda qs, **k: [{"question": q, "answer": "no evidence yet", "known": False} for q in qs]),
                          intraday_agent=None, log=log.append, day_df=lambda: _day_df(400), weather=lambda: {},
                          model_calls_left=lambda: 0, budget_ok=lambda: True, owner_messages=lambda t: [],
                          push=lambda rest, t, note, mode: pushes.append({"t": t, "mode": mode, "note": note}),
                          pushes=pushes, day_events=[],
                          fresh_notes=lambda t: [{"kind": "override", "text": "override light=100: dark", "from": 400, "to": 500,
                                                  "settings": {"light": 100.0}, "reason": "dark", "source": "owner"}] if t == 375 else [])
    out = build_intraday_graph(ctx).invoke({"t": 375})
    assert out["pushed"] is True and settings_at(blocks, 450)["light"] == 100.0  # an owner override is not capped
    assert any(d["kind"] == "note" for d in ctx.day_events)
    made = next(b for b in blocks if b["from"] == 400)
    assert made["origin"]["by"] == "heuristic" and made["origin"]["at"] == 375 and made["stretch"] == 3
