"""The master record: every minute of every day, with the intent behind it.

The intraday agent's log of the day goes through the data agent's ingest
(decisions rule 45: one SQLite table ``readings``, one row per played
minute, date / weekday / time added on the way in). Before it goes in, the
navigation team adds what the sensors cannot say: which plan block was in
force, why, and whether the daily plan or an intraday adjustment set it.
Without those columns the analysis team cannot tell "22 C because planned"
from "22 C because rain fired".
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

_ARCH = Path(__file__).resolve().parents[1]
for p in (str(_ARCH), str(_ARCH / "analysis_team" / "data_handler_agent")):
    if p not in sys.path:
        sys.path.insert(0, p)

from data_handler_agent import DataHandlerAgent  # noqa: E402

_NAV = Path(__file__).resolve().parent
if str(_NAV) not in sys.path:
    sys.path.insert(0, str(_NAV))
from blocks import stretch_of  # noqa: E402


class Provenance:
    """Per-minute: which block, its reason, who set it, and which 2-hour stretch it is."""

    def __init__(self, n: int = 840) -> None:
        self.block_from: List[int] = [0] * n
        self.reason: List[str] = [""] * n
        self.set_by: List[str] = ["daily"] * n
        self.stretch: List[int] = [stretch_of(t) for t in range(n)]
        self.n = n

    def stamp(self, blocks: List[Dict[str, Any]], from_t: int, set_by: str) -> None:
        for b in blocks:
            for t in range(max(from_t, b["from"]), min(self.n, b["to"])):
                self.block_from[t] = b["from"]
                self.reason[t] = b["reasons"]
                self.set_by[t] = set_by


def enrich(day_df: pd.DataFrame, date, prov: Provenance) -> pd.DataFrame:
    df = day_df.copy().reset_index(drop=True)
    n = len(df)
    df["month"] = date.strftime("%B")
    df["block_from"] = prov.block_from[:n]
    df["block_reason"] = prov.reason[:n]
    df["set_by"] = prov.set_by[:n]
    df["stretch"] = prov.stretch[:n]
    return df


def _add_missing_columns(db_path: str, df: pd.DataFrame) -> None:
    """A record written before a column existed gets the column added (empty for old rows)."""
    import sqlite3
    p = Path(db_path)
    if not p.exists():
        return
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("PRAGMA table_info(readings)").fetchall()
        if not rows:
            return
        have = {r[1] for r in rows}
        for col in df.columns:
            if col not in have:
                kind = "INTEGER" if str(df[col].dtype).startswith("int") else ("REAL" if str(df[col].dtype).startswith("float") else "TEXT")
                conn.execute(f'ALTER TABLE readings ADD COLUMN "{col}" {kind}')


def record_day(db_path: str, day_df: pd.DataFrame, date, prov: Provenance) -> int:
    """Append the finished day to the master record. Returns rows written."""
    df = enrich(day_df, date, prov)
    _add_missing_columns(db_path, df)
    DataHandlerAgent(llm=None, db_path=db_path).ingest_day(df, date)
    return len(df)
