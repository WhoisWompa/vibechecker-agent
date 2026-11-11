"""Owner's notes from a Google Calendar, read through the calendar MCP server.

The owner keeps one calendar for the cafe. Each morning the navigation team
asks that calendar for the day's events and turns each one into an owner's
note, the same note the dated file and the ``--note`` flag produce:

    event title                     -> the note's text
    event start and end             -> the note's window (minutes since 8am)
    an all-day event                -> the whole day
    a title starting with "override"
      carrying lever=value pairs    -> an override note with exact settings
    the event description           -> appended to the text (the owner's detail)

The calendar is reached through the MCP server ``@cocal/google-calendar-mcp``
(started with ``npx``) and LangChain's MCP client, so the same door would
serve any other calendar server. No model call is made here: the fetch is
plain code and the notes then flow through the usual checklist and planner.

Needs: ``uv sync --extra calendar`` and ``GOOGLE_OAUTH_CREDENTIALS`` in
``agent/.env`` (see CALENDAR_SETUP.md). If the calendar cannot be reached the
caller logs it and the day runs on the notes already on file.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from notes import DAY_MINUTES, make_note

SERVER_PACKAGE = "@cocal/google-calendar-mcp"
TIMEZONE = "Asia/Singapore"
OPEN_MINUTE = 8 * 60
ENV_PATH = Path(__file__).resolve().parents[2] / ".env"  # agent/.env, the same file llm.py reads


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_PATH)
    except Exception:  # dotenv is optional here; the variables may already be in the environment
        pass


# ---------------------------------------------------------------------------
# Events -> notes (plain code, no network; tested offline)
# ---------------------------------------------------------------------------

def _minute_of(iso: str, tz: str) -> int:
    """An ISO 8601 timestamp -> minutes since 8am local, unclamped."""
    when = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if when.tzinfo is not None:
        when = when.astimezone(ZoneInfo(tz))
    return when.hour * 60 + when.minute - OPEN_MINUTE


def event_to_note(event: Dict[str, Any], tz: str = TIMEZONE) -> Optional[Dict[str, Any]]:
    """One calendar event -> one owner's note, or None if it has no title or lies outside opening hours."""
    title = str(event.get("summary") or "").strip()
    if not title:
        return None
    detail = str(event.get("description") or "").strip()
    text = title if not detail else f"{title}. {detail}"
    start, end = event.get("start") or {}, event.get("end") or {}
    all_day = "dateTime" not in start
    if all_day:
        lo, hi = None, None
    else:
        lo = _minute_of(start["dateTime"], tz)
        hi = _minute_of(end["dateTime"], tz) if end.get("dateTime") else lo + 60
        if hi <= 0 or lo >= DAY_MINUTES:
            return None  # the event is over before we open, or starts after we close
        lo, hi = max(0, lo), min(DAY_MINUTES, hi)
        if hi <= lo:
            hi = min(DAY_MINUTES, lo + 15)
    try:
        note = make_note(text, source="calendar")
    except ValueError:  # "override" without lever=value pairs: keep it as a fact the planner reads
        note = make_note(text.replace("override", "note", 1), source="calendar")
    if note["kind"] == "override":
        note["from"], note["to"] = (0, DAY_MINUTES) if all_day else (lo, hi)
    else:
        note["from"], note["to"] = lo, hi
    if event.get("id"):
        note["event_id"] = str(event["id"])
    return note


def parse_events(result: Any) -> List[Dict[str, Any]]:
    """What the list-events tool returned -> the list of event dicts.

    The MCP client hands back either a ToolMessage (structured content in its
    artifact, JSON text in its content) or a plain list of text blocks. An
    error from the server arrives as text starting with "MCP error"."""
    artifact = getattr(result, "artifact", None)
    if isinstance(artifact, dict) and isinstance(artifact.get("structured_content"), dict):
        return list(artifact["structured_content"].get("events") or [])
    content = getattr(result, "content", result)
    if isinstance(content, list):
        text = "\n".join(str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text")
    else:
        text = str(content)
    text = text.strip()
    if not text:
        return []
    if text.startswith("MCP error") or re.match(r"^(error|authentication)", text, re.I):
        raise RuntimeError(text[:300])
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise RuntimeError(f"calendar server answered with text, not JSON: {text[:200]}")
    if isinstance(data, dict):
        return list(data.get("events") or [])
    return list(data) if isinstance(data, list) else []


# ---------------------------------------------------------------------------
# The door to the calendar (MCP over stdio)
# ---------------------------------------------------------------------------

class CalendarSource:
    """Reads one calendar's events for a date through the calendar MCP server."""

    def __init__(self, calendar: str, credentials_path: Optional[str] = None, token_path: Optional[str] = None,
                 tz: str = TIMEZONE, command: str = "npx", args: Optional[List[str]] = None,
                 timeout: float = 90.0) -> None:
        _load_env()
        creds = credentials_path or os.environ.get("GOOGLE_OAUTH_CREDENTIALS")
        if not creds:
            raise ValueError("GOOGLE_OAUTH_CREDENTIALS is not set: put the path to the OAuth client JSON in agent/.env "
                             "(see navigation_team/CALENDAR_SETUP.md)")
        self.calendar = calendar
        self.tz = tz
        self.command = command
        self.args = list(args) if args else ["-y", SERVER_PACKAGE]
        self.timeout = timeout
        self.env = {**os.environ, "GOOGLE_OAUTH_CREDENTIALS": str(Path(creds).expanduser())}
        token = token_path or os.environ.get("GOOGLE_CALENDAR_MCP_TOKEN_PATH")
        if token:
            self.env["GOOGLE_CALENDAR_MCP_TOKEN_PATH"] = str(Path(token).expanduser())

    def connection(self) -> Dict[str, Any]:
        return {"transport": "stdio", "command": self.command, "args": self.args, "env": self.env}

    def _day_bounds(self, date: str) -> tuple[str, str]:
        zone = ZoneInfo(self.tz)
        day = dt.date.fromisoformat(date)
        lo = dt.datetime.combine(day, dt.time(0, 0), tzinfo=zone)
        hi = dt.datetime.combine(day, dt.time(23, 59, 59), tzinfo=zone)
        return lo.isoformat(), hi.isoformat()

    async def _events_async(self, date: str) -> List[Dict[str, Any]]:
        try:
            from langchain_mcp_adapters.client import MultiServerMCPClient
        except ImportError as exc:
            raise RuntimeError("langchain-mcp-adapters is not installed: run `uv sync --extra calendar`") from exc
        client = MultiServerMCPClient({"calendar": self.connection()})
        tools = await client.get_tools()
        tool = next((t for t in tools if t.name == "list-events"), None)
        if tool is None:
            raise RuntimeError(f"the calendar server exposes no list-events tool (has {[t.name for t in tools]})")
        lo, hi = self._day_bounds(date)
        result = await tool.ainvoke({"calendarId": self.calendar, "timeMin": lo, "timeMax": hi, "timeZone": self.tz})
        return parse_events(result)

    def events_for(self, date: str) -> List[Dict[str, Any]]:
        """The calendar's events on ``date`` (YYYY-MM-DD), synchronously, within the timeout."""
        coro = asyncio.wait_for(self._events_async(date), self.timeout)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        with concurrent.futures.ThreadPoolExecutor(1) as ex:  # called from inside a running loop (a notebook)
            return ex.submit(asyncio.run, coro).result()

    def notes_for(self, date: str) -> List[Dict[str, Any]]:
        """The day's events as owner's notes, in start order."""
        notes = [n for n in (event_to_note(e, self.tz) for e in self.events_for(date)) if n]
        return sorted(notes, key=lambda n: (n["from"] is not None, n["from"] or 0))
