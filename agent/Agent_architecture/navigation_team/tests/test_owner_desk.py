"""The owner's sentence -> a calendar event the morning read parses back (no model, no network)."""

import sys
from pathlib import Path

NAV = Path(__file__).resolve().parents[1]
if str(NAV) not in sys.path:
    sys.path.insert(0, str(NAV))

from calendar_notes import CalendarSource, event_to_note  # noqa: E402
from owner_desk import EventRequest, OwnerDesk, describe, link_in, parse_request, tidy, to_event  # noqa: E402


class FakeLLM:
    def __init__(self, req):
        self.req, self.prompts = req, []

    def with_structured_output(self, schema):
        assert schema is EventRequest
        return self

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return self.req


def test_timed_request_becomes_a_timed_event_and_reads_back_as_the_same_note():
    llm = FakeLLM(EventRequest(title="big group of 12 booked", date="2024-04-12", start="15:00", end="17:00"))
    req = parse_request(llm, "book a group of 12 next Friday 3 to 5pm", "2024-04-05")
    assert "Friday 2024-04-05" in llm.prompts[0] and "book a group of 12" in llm.prompts[0]
    ev = to_event(req, "Vibechecker owner")
    assert ev == {"calendarId": "Vibechecker owner", "summary": "big group of 12 booked",
                  "start": "2024-04-12T15:00:00", "end": "2024-04-12T17:00:00", "timeZone": "Asia/Singapore"}
    # what the morning read would make of it
    note = event_to_note({"id": "x", "summary": ev["summary"], "start": {"dateTime": ev["start"] + "+08:00"},
                          "end": {"dateTime": ev["end"] + "+08:00"}})
    assert note["kind"] == "fact" and (note["from"], note["to"]) == (420, 540)
    assert describe(req) == "Fri 12 Apr 2024, 15:00-17:00: big group of 12 booked"


def test_all_day_and_override_requests():
    all_day = tidy(EventRequest(title="closing early, last order 8pm", date="2024-04-12", reason="staff dinner"))
    ev = to_event(all_day, "Vibechecker owner")
    assert all_day.all_day and ev["start"] == "2024-04-12" and ev["end"] == "2024-04-13" and ev["description"] == "staff dinner"

    override = tidy(EventRequest(title="quieter evenings", date="2024-04-13", start="18:00", end="22:00",
                                 settings={"light": 300, "volume": 45, "mood": 9}, reason="quieter evenings"))
    ev = to_event(override, "Vibechecker owner")
    assert ev["summary"] == "override light=300 volume=45: quieter evenings" and "description" not in ev
    note = event_to_note({"id": "y", "summary": ev["summary"], "start": {"dateTime": ev["start"] + "+08:00"},
                          "end": {"dateTime": ev["end"] + "+08:00"}})
    assert note["kind"] == "override" and note["settings"] == {"light": 300.0, "volume": 45.0} and (note["from"], note["to"]) == (600, 840)


def test_tidy_fixes_what_the_model_leaves_loose():
    r = tidy(EventRequest(title=" delivery ", date="2024-04-12", start="07:30"))
    assert (r.start, r.end) == ("08:00", "08:30") and r.title == "delivery"
    r = tidy(EventRequest(title="late clean", date="2024-04-12", start="21:30", end="23:00"))
    assert (r.start, r.end) == ("21:30", "22:00")
    r = tidy(EventRequest(title="x", date="2024-04-12", start="12:00", end="11:00"))
    assert r.end == "22:00"
    r = tidy(EventRequest(title="x", date="2024-04-12", start="12:00", settings={"mood": 1}))
    assert r.settings is None and r.end == "13:00"


def test_desk_creates_only_through_the_injected_creator(tmp_path):
    creds = tmp_path / "c.json"
    creds.write_text("{}")
    calls = []

    def creator(args):
        calls.append(args)
        return '{"event": {"id": "abc", "htmlLink": "https://www.google.com/calendar/event?eid=abc"}}'

    desk = OwnerDesk(CalendarSource("Vibechecker owner", credentials_path=str(creds)),
                     FakeLLM(EventRequest(title="live music", date="2024-04-13", start="19:00", end="21:00", unsure="which band")),
                     creator=creator)
    req = desk.propose("live music on Saturday from 7", "2024-04-05")
    assert "not sure about: which band" in describe(req)
    reply = desk.create(req)
    assert calls[0]["summary"] == "live music" and calls[0]["calendarId"] == "Vibechecker owner"
    assert link_in(reply) == "https://www.google.com/calendar/event?eid=abc"
    assert link_in("Event created: https://www.google.com/calendar/event?eid=zzz done") == "https://www.google.com/calendar/event?eid=zzz"
    assert link_in("nothing here") is None
