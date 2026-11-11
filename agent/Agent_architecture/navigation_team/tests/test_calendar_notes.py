"""Calendar events -> owner's notes, offline (no server, no network)."""

import json
import sys
from pathlib import Path

import pytest

NAV = Path(__file__).resolve().parents[1]
if str(NAV) not in sys.path:
    sys.path.insert(0, str(NAV))

from calendar_notes import CalendarSource, event_to_note, parse_events  # noqa: E402
from notes import NoteBook  # noqa: E402


def _event(summary, start=None, end=None, description=None, all_day=None, id="ev1"):
    ev = {"id": id, "summary": summary}
    if description:
        ev["description"] = description
    if all_day:
        ev["start"], ev["end"] = {"date": all_day}, {"date": all_day}
    else:
        ev["start"], ev["end"] = {"dateTime": start, "timeZone": "Asia/Singapore"}, {"dateTime": end, "timeZone": "Asia/Singapore"}
    return ev


def test_timed_event_becomes_a_fact_note_with_its_window():
    n = event_to_note(_event("big group of 12", "2024-04-05T15:00:00+08:00", "2024-04-05T17:00:00+08:00"))
    assert n["kind"] == "fact" and n["source"] == "calendar" and n["event_id"] == "ev1"
    assert (n["from"], n["to"]) == (420, 540)  # 3pm-5pm in minutes since 8am
    assert n["text"] == "big group of 12"


def test_description_is_appended_and_utc_times_are_converted():
    n = event_to_note(_event("closing early", "2024-04-05T11:00:00Z", "2024-04-05T14:00:00Z", description="last order 8pm"))
    assert n["text"] == "closing early. last order 8pm"
    assert (n["from"], n["to"]) == (660, 840)  # 7pm-10pm local


def test_all_day_event_covers_the_whole_day():
    fact = event_to_note(_event("school holiday crowd", all_day="2024-04-05"))
    assert (fact["from"], fact["to"]) == (None, None)
    override = event_to_note(_event("override light=300 volume=45: quieter evenings", all_day="2024-04-05"))
    assert override["kind"] == "override" and (override["from"], override["to"]) == (0, 840)
    assert override["settings"] == {"light": 300.0, "volume": 45.0}


def test_override_event_keeps_its_own_window_and_events_outside_hours_are_dropped():
    o = event_to_note(_event("override 6pm-10pm light=300: dim", "2024-04-05T18:00:00+08:00", "2024-04-05T22:00:00+08:00"))
    assert o["kind"] == "override" and (o["from"], o["to"]) == (600, 840)
    assert event_to_note(_event("late clean", "2024-04-05T23:00:00+08:00", "2024-04-05T23:30:00+08:00")) is None
    assert event_to_note(_event("", "2024-04-05T10:00:00+08:00", "2024-04-05T11:00:00+08:00")) is None


def test_parse_events_reads_text_blocks_and_raises_on_server_errors():
    payload = {"events": [{"id": "a", "summary": "x", "start": {"date": "2024-04-05"}, "end": {"date": "2024-04-05"}}], "totalCount": 1}
    assert parse_events([{"type": "text", "text": json.dumps(payload)}]) == payload["events"]
    assert parse_events(json.dumps(payload["events"])) == payload["events"]
    with pytest.raises(RuntimeError):
        parse_events([{"type": "text", "text": "MCP error -32600: Authentication tokens are no longer valid."}])


def test_notebook_files_a_calendar_note_once(tmp_path):
    nb = NoteBook(str(tmp_path))
    n = event_to_note(_event("big group of 12", "2024-04-05T15:00:00+08:00", "2024-04-05T17:00:00+08:00"))
    assert nb.add_prepared("2024-04-05", n) is True
    assert nb.add_prepared("2024-04-05", dict(n)) is False  # same event id
    assert nb.add_prepared("2024-04-05", {**n, "event_id": None}) is False  # same text and window
    assert len(nb.for_date("2024-04-05")) == 1
    nb.add("2024-04-05", "big group of 12 at 3pm-5pm")  # a hand-typed note of the same thing is a different source
    assert len(nb.for_date("2024-04-05")) == 2


def test_calendar_source_needs_credentials_and_builds_a_stdio_connection(monkeypatch, tmp_path):
    monkeypatch.delenv("GOOGLE_OAUTH_CREDENTIALS", raising=False)
    monkeypatch.setattr("calendar_notes._load_env", lambda: None)
    with pytest.raises(ValueError):
        CalendarSource("Vibechecker owner")
    creds = tmp_path / "client.json"
    creds.write_text("{}")
    src = CalendarSource("Vibechecker owner", credentials_path=str(creds))
    conn = src.connection()
    assert conn["transport"] == "stdio" and conn["command"] == "npx" and "@cocal/google-calendar-mcp" in conn["args"]
    assert conn["env"]["GOOGLE_OAUTH_CREDENTIALS"] == str(creds)
    lo, hi = src._day_bounds("2024-04-05")
    assert lo == "2024-04-05T00:00:00+08:00" and hi == "2024-04-05T23:59:59+08:00"


def test_runner_files_calendar_notes_each_morning(tmp_path, monkeypatch):
    from runner import NavRun

    class FakeCalendar:
        calendar = "Vibechecker owner"

        def notes_for(self, date):
            return [event_to_note(_event("big group of 12", f"{date}T15:00:00+08:00", f"{date}T17:00:00+08:00"))]

    run = NavRun(days=1, seed=1, start_date="2024-04-05", state_dir=str(tmp_path), mode="heuristic", verbose=False)
    run.calendar = FakeCalendar()
    run._calendar_notes("2024-04-05")
    run._calendar_notes("2024-04-05")  # the second morning of the same date files nothing new
    notes = run.notebook.for_date("2024-04-05")
    assert len(notes) == 1 and notes[0]["source"] == "calendar" and notes[0]["from"] == 420
