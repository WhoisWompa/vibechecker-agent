"""What the calendar says about a date. Public knowledge, so the agent may use it.

Looked up by the date the cafe says it is: in the simulation that is a 2024
date (Lionel's rig replays 2024), in a real deployment it is today. The same
function serves both, which is the rule for every outside signal in this team.

Public holidays are MOM's gazetted lists; when a Sunday holiday is observed on
the Monday, both days are listed. Years bundled here: 2024, 2025, 2026. A year
that is not bundled is fetched once from MOM's page and cached for the run; if
that fails the calendar simply reports nothing, and the agent flies on the
crowd it can see.

School holidays follow MOE's calendar. 2024 is exact; 2025 and 2026 are the
usual pattern and should be checked against MOE before a live deployment.
"""

from __future__ import annotations

import datetime
import re
import urllib.request
from typing import Dict, List, Tuple

MOM_URL = "https://www.mom.gov.sg/employment-practices/public-holidays"

PUBLIC_HOLIDAYS: Dict[int, Dict[str, str]] = {
    2024: {
        "2024-01-01": "New Year's Day", "2024-02-10": "Chinese New Year", "2024-02-11": "Chinese New Year",
        "2024-02-12": "Chinese New Year (observed)", "2024-03-29": "Good Friday",
        "2024-04-10": "Hari Raya Puasa", "2024-05-01": "Labour Day", "2024-05-22": "Vesak Day",
        "2024-06-17": "Hari Raya Haji", "2024-08-09": "National Day", "2024-10-31": "Deepavali",
        "2024-12-25": "Christmas Day",
    },
    2025: {
        "2025-01-01": "New Year's Day", "2025-01-29": "Chinese New Year", "2025-01-30": "Chinese New Year",
        "2025-03-31": "Hari Raya Puasa", "2025-04-18": "Good Friday", "2025-05-01": "Labour Day",
        "2025-05-03": "Polling Day", "2025-05-12": "Vesak Day", "2025-06-07": "Hari Raya Haji",
        "2025-08-09": "National Day", "2025-10-20": "Deepavali", "2025-12-25": "Christmas Day",
    },
    2026: {
        "2026-01-01": "New Year's Day", "2026-02-17": "Chinese New Year", "2026-02-18": "Chinese New Year",
        "2026-03-21": "Hari Raya Puasa", "2026-04-03": "Good Friday", "2026-05-01": "Labour Day",
        "2026-05-27": "Hari Raya Haji", "2026-05-31": "Vesak Day", "2026-06-01": "Vesak Day (observed)",
        "2026-08-09": "National Day", "2026-08-10": "National Day (observed)",
        "2026-11-08": "Deepavali", "2026-11-09": "Deepavali (observed)", "2026-12-25": "Christmas Day",
    },
}

EVES: Dict[str, str] = {
    "2024-02-09": "Chinese New Year Eve", "2024-12-24": "Christmas Eve", "2024-12-31": "New Year's Eve",
    "2025-01-28": "Chinese New Year Eve", "2025-12-24": "Christmas Eve", "2025-12-31": "New Year's Eve",
    "2026-02-16": "Chinese New Year Eve", "2026-12-24": "Christmas Eve", "2026-12-31": "New Year's Eve",
}

# (start, end) inclusive
SCHOOL_HOLIDAYS: Dict[int, List[Tuple[str, str, str]]] = {
    2024: [("2024-03-09", "2024-03-17", "March school holidays"),
           ("2024-05-25", "2024-06-23", "June school holidays"),
           ("2024-09-07", "2024-09-15", "September school holidays"),
           ("2024-11-16", "2024-12-31", "year-end school holidays")],
    2025: [("2025-03-15", "2025-03-23", "March school holidays"),
           ("2025-05-31", "2025-06-29", "June school holidays"),
           ("2025-09-06", "2025-09-14", "September school holidays"),
           ("2025-11-22", "2025-12-31", "year-end school holidays")],
    2026: [("2026-03-14", "2026-03-22", "March school holidays"),
           ("2026-05-30", "2026-06-28", "June school holidays"),
           ("2026-09-05", "2026-09-13", "September school holidays"),
           ("2026-11-21", "2026-12-31", "year-end school holidays")],
}

_fetched: Dict[int, Dict[str, str]] = {}


def _fetch_year(year: int) -> Dict[str, str]:
    """Best-effort read of MOM's page for a year not bundled. Empty on any failure."""
    if year in _fetched:
        return _fetched[year]
    out: Dict[str, str] = {}
    try:
        req = urllib.request.Request(MOM_URL, headers={"User-Agent": "Mozilla/5.0"})
        html = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "replace")
        text = re.sub(r"<[^>]+>", " ", html)
        months = {m: i for i, m in enumerate(
            ["January", "February", "March", "April", "May", "June", "July", "August",
             "September", "October", "November", "December"], 1)}
        for name, day, month in re.findall(
                r"([A-Z][A-Za-z' ]{3,40}?)\s+(?:\w+\s+)?(\d{1,2})\s+(January|February|March|April|May|June|July|"
                r"August|September|October|November|December)\s+" + str(year), text):
            date = f"{year}-{months[month]:02d}-{int(day):02d}"
            out.setdefault(date, name.strip())
    except Exception:
        out = {}
    _fetched[year] = out
    return out


def holidays_for_year(year: int) -> Dict[str, str]:
    return PUBLIC_HOLIDAYS.get(year) or _fetch_year(year)


def events_for(date: str) -> List[str]:
    """Human-readable calendar events for an ISO date, empty if an ordinary day."""
    out: List[str] = []
    try:
        d = datetime.date.fromisoformat(date)
    except ValueError:
        return out
    hols = holidays_for_year(d.year)
    if date in hols:
        out.append(f"public holiday: {hols[date]}")
    if date in EVES:
        out.append(f"holiday eve: {EVES[date]}")
    for start, end, name in SCHOOL_HOLIDAYS.get(d.year, []):
        if datetime.date.fromisoformat(start) <= d <= datetime.date.fromisoformat(end):
            out.append(name)
    if d.weekday() >= 5:
        out.append("weekend")
    return out


def crowd_hint(events: List[str]) -> str:
    """What the calendar implies for the crowd, in words the plan can be read against."""
    if any(e.startswith("public holiday") for e in events):
        return "public holiday: expect a bigger, weekend-shaped crowd (brunch rather than lunch)"
    if any(e.startswith("holiday eve") for e in events):
        return "holiday eve: expect a somewhat bigger crowd on the normal weekday rhythm"
    if any("school holidays" in e for e in events):
        return "school holidays: expect more daytime footfall, families and students"
    return ""
