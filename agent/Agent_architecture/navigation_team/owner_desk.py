"""The owner's desk: a sentence from the owner becomes an event on the cafe's calendar.

    "book a group of 12 next Friday 3 to 5pm"
    "we close early on the 12th, last order 8pm"
    "from 6pm to 10pm on Saturday keep light at 300 and volume at 45, quieter evenings"

One model call reads the sentence into an EventRequest (title, date, window,
exact settings if the owner named any). Plain code turns that into the
calendar server's create-event call, on the "Vibechecker owner" calendar,
through the same MCP door ``calendar_notes.py`` reads from. The event's title
is written the way the owner would have typed a note, so the next morning's
read turns it back into the same note. Nothing is written until the caller
confirms; the desk only ever creates, never edits or deletes.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from pydantic import BaseModel

from calendar_notes import CalendarSource, TIMEZONE
from notes import parse_window

_DIR = Path(__file__).parent
OPEN, CLOSE = "08:00", "22:00"
LEVERS = ("light", "temperature", "volume", "fan")


class EventRequest(BaseModel):
    """What the owner asked for, as the model read it."""
    title: str                              # the note in the owner's words, without dates or clock times
    date: str                               # YYYY-MM-DD
    start: Optional[str] = None             # HH:MM local; None with all_day
    end: Optional[str] = None               # HH:MM local; None with all_day
    all_day: bool = False
    settings: Optional[Dict[str, float]] = None   # only levers the owner gave numbers for
    reason: str = ""                        # the owner's why, if said
    unsure: str = ""                        # anything the model could not pin down; empty when confident


def _prompt(name: str) -> str:
    return (_DIR / f"{name}.md").read_text(encoding="utf-8")


def parse_request(llm, text: str, as_of: str) -> EventRequest:
    """One model call: the owner's sentence -> an EventRequest, dates resolved against ``as_of``."""
    day = dt.date.fromisoformat(as_of)
    prompt = _prompt("owner_desk").format(text=text.strip(), as_of=as_of, weekday=day.strftime("%A"),
                                         open=OPEN, close=CLOSE)
    req: EventRequest = llm.with_structured_output(EventRequest).invoke(prompt)
    return tidy(req)


def tidy(req: EventRequest) -> EventRequest:
    """Plain-code corrections after the model: a start without an end lasts an hour, times stay within
    opening hours, settings keep only real levers, an all-day request carries no times."""
    r = req.model_copy()
    if r.all_day or not r.start:
        r.all_day, r.start, r.end = True, None, None
    else:
        if not r.end:
            h, m = (int(x) for x in r.start.split(":"))
            r.end = f"{min(21, h + 1):02d}:{m:02d}" if h + 1 < 22 else CLOSE
        r.start, r.end = max(r.start, OPEN), min(r.end, CLOSE)
        if r.end <= r.start:
            r.end = CLOSE
    if r.settings:
        r.settings = {k: (int(v) if k == "fan" else float(v)) for k, v in r.settings.items() if k in LEVERS}
        if not r.settings:
            r.settings = None
    r.title = re.sub(r"\s+", " ", r.title).strip().rstrip(".")
    return r


def summary_text(req: EventRequest) -> str:
    """The event title, written the way an owner's note is written so the morning read parses it back."""
    if req.settings:
        levers = " ".join(f"{k}={v:g}" for k, v in req.settings.items())
        return f"override {levers}: {req.reason or req.title}"
    return req.title


def to_event(req: EventRequest, calendar: str, tz: str = TIMEZONE) -> Dict[str, Any]:
    """The create-event call for the calendar server."""
    if req.all_day:
        start = req.date
        end = (dt.date.fromisoformat(req.date) + dt.timedelta(days=1)).isoformat()  # exclusive end for all-day
    else:
        start, end = f"{req.date}T{req.start}:00", f"{req.date}T{req.end}:00"
    args: Dict[str, Any] = {"calendarId": calendar, "summary": summary_text(req), "start": start, "end": end, "timeZone": tz}
    if req.reason and not req.settings:
        args["description"] = req.reason
    return args


def describe(req: EventRequest) -> str:
    """One line for the owner to confirm."""
    day = dt.date.fromisoformat(req.date).strftime("%a %d %b %Y")
    when = "all day" if req.all_day else f"{req.start}-{req.end}"
    line = f"{day}, {when}: {summary_text(req)}"
    if req.unsure:
        line += f"\n  not sure about: {req.unsure}"
    return line


class OwnerDesk:
    """Reads the owner's sentence and, once confirmed, writes the event."""

    def __init__(self, source: CalendarSource, llm, creator: Optional[Callable[[Dict[str, Any]], str]] = None) -> None:
        self.source = source
        self.llm = llm
        self.creator = creator  # tests inject one; otherwise the calendar server's create-event tool

    def propose(self, text: str, as_of: str) -> EventRequest:
        return parse_request(self.llm, text, as_of)

    def event_for(self, req: EventRequest) -> Dict[str, Any]:
        return to_event(req, self.source.calendar, self.source.tz)

    async def _create_async(self, args: Dict[str, Any]) -> str:
        from langchain_mcp_adapters.client import MultiServerMCPClient
        client = MultiServerMCPClient({"calendar": self.source.connection()})
        tools = {t.name: t for t in await client.get_tools()}
        if "create-event" not in tools:
            raise RuntimeError("the calendar server exposes no create-event tool")
        result = await tools["create-event"].ainvoke(args)
        content = getattr(result, "content", result)
        text = "\n".join(str(b.get("text", "")) for b in content if isinstance(b, dict)) if isinstance(content, list) else str(content)
        if text.startswith("MCP error") or re.match(r"^(error|authentication)", text, re.I):
            raise RuntimeError(text[:300])
        return text

    def create(self, req: EventRequest) -> str:
        """Write the event. Returns what the server said (a JSON text with the event, or a duplicate warning)."""
        args = self.event_for(req)
        if self.creator is not None:
            return self.creator(args)
        coro = asyncio.wait_for(self._create_async(args), self.source.timeout)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        with concurrent.futures.ThreadPoolExecutor(1) as ex:
            return ex.submit(asyncio.run, coro).result()


def link_in(server_reply: str) -> Optional[str]:
    """The event's Google Calendar link, if the server's reply carries one."""
    try:
        data = json.loads(server_reply)
        ev = data.get("event") or data
        return ev.get("htmlLink")
    except Exception:
        m = re.search(r"https://www\.google\.com/calendar/event\?eid=\S+", server_reply)
        return m.group(0) if m else None
