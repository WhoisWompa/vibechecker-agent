"""Data handler agent: the single door to stored venue history. Ingest and
inventory are pure code; only query translation uses the LLM."""

import sqlite3
import sys
from datetime import date as date_type
from pathlib import Path

import pandas as pd
from pydantic import BaseModel

_DIR = Path(__file__).parent

# researcher_agent/ (where pipeline.py lives) is not a package -- same
# sys.path shim the workflow uses to import agent modules by filename.
_RESEARCHER_DIR = str(Path(__file__).resolve().parent.parent / "researcher_agent")
if _RESEARCHER_DIR not in sys.path:
    sys.path.insert(0, _RESEARCHER_DIR)

import pipeline  # noqa: E402

# Make sure the agent folder (where memory.py lives) is importable too.
_AGENT_DIR = str(Path(__file__).resolve().parents[2])
if _AGENT_DIR not in sys.path:
    sys.path.insert(0, _AGENT_DIR)

from memory import venue_reference  # noqa: E402

_SETTINGS = ["light_set", "temperature_set", "volume", "fan"]


def _prompt(name: str) -> str:
    return (_DIR / f"{name}.md").read_text(encoding="utf-8")


class SqlQuery(BaseModel):
    sql: str


class GateExplanation(BaseModel):
    reason: str


class GroupPlan(BaseModel):
    unit: str                 # "day" | "episode"
    episode_condition: str = ""  # what defines one episode; empty for day
    side_a: str               # plain-words filter, side the condition holds on
    side_b: str               # plain-words filter for the comparison side


class DataHandlerAgent:
    """Single door to stored history: ingest (write path), inventory, queries."""

    def __init__(self, llm, db_path: str):
        self.llm = llm
        self.db_path = str(db_path)

    # -- ingest: the only write path, no LLM --

    def ingest_day(self, day_df: pd.DataFrame, day_date: date_type) -> None:
        """Append one finished day to the readings table, enriched with
        date, weekday and clock time."""
        df = day_df.copy()
        df.insert(0, "date", day_date.isoformat())
        df.insert(1, "weekday", day_date.strftime("%A"))
        df.insert(2, "time", [f"{8 + i // 60:02d}:{i % 60:02d}" for i in range(len(df))])
        with sqlite3.connect(self.db_path) as conn:
            df.to_sql("readings", conn, if_exists="append", index=False)

    # -- inventory: pure code, feeds the data gate --

    def inventory(self) -> str:
        with sqlite3.connect(self.db_path) as conn:
            days = pd.read_sql("SELECT date, weekday FROM readings GROUP BY date", conn)
            settings = pd.read_sql(
                "SELECT " + ", ".join(
                    f"MIN({c}) AS {c}_min, MAX({c}) AS {c}_max" for c in _SETTINGS
                ) + " FROM readings",
                conn,
            ).iloc[0]
            weather = pd.read_sql(
                "SELECT central_forecast_text, COUNT(DISTINCT date) AS days "
                "FROM readings WHERE central_forecast_text IS NOT NULL "
                "GROUP BY central_forecast_text",
                conn,
            )
        lines = [f"Days of history: {len(days)}"]
        by_weekday = days.groupby("weekday").size()
        lines.append("Days per weekday: " + ", ".join(f"{d}: {n}" for d, n in by_weekday.items()))
        lines.append("Days touched per weather type: " + ", ".join(
            f"{row.central_forecast_text}: {row.days}" for row in weather.itertuples()
        ))
        for c in _SETTINGS:
            lines.append(f"Setting {c} covered: {settings[f'{c}_min']} to {settings[f'{c}_max']}")
        return "\n".join(lines)

    # -- queries: LLM translates, code executes read-only --

    def query(self, request: str) -> pd.DataFrame:
        """Translate the request to SQL and run it. A query that fails to
        execute is re-asked with the error fed back, up to 3 attempts."""
        structured_llm = self.llm.with_structured_output(SqlQuery).with_retry(stop_after_attempt=3)
        base_prompt = _prompt("write_query").format(venue_reference=venue_reference(), request=request)
        prompt = base_prompt
        for attempt in range(3):
            result: SqlQuery = structured_llm.invoke(prompt)
            try:
                return self._run_sql(result.sql)
            except (pd.errors.DatabaseError, sqlite3.Error) as e:
                if attempt == 2:
                    raise
                prompt = base_prompt + (
                    f"\n\nYour previous SQL failed to execute. Failed SQL:\n{result.sql}"
                    f"\n\nDatabase error:\n{e}\n\nWrite a corrected query for the same request."
                )

    def plan_groups(self, question: str) -> GroupPlan:
        structured_llm = self.llm.with_structured_output(GroupPlan).with_retry(stop_after_attempt=3)
        return structured_llm.invoke(_prompt("plan_groups").format(question=question))

    def check_data(self, question: str) -> dict:
        """Data gate: plan the comparison groups, count units per side, and
        decide testability with the deterministic floor (at least 2 sides,
        at least 3 units on each). Only an untestable result spends an LLM
        call — a short plain-words reason why it can't be tested."""
        plan = self.plan_groups(question)
        counts = pipeline.count_units(self, plan)
        testable = len(counts) >= 2 and all(n >= 3 for n in counts.values())
        if testable:
            return {"plan": plan, "testable": True, "reason": ""}
        structured_llm = self.llm.with_structured_output(GateExplanation).with_retry(stop_after_attempt=3)
        result: GateExplanation = structured_llm.invoke(
            _prompt("explain_gate").format(
                question=question,
                unit=plan.unit,
                side_a=plan.side_a,
                side_b=plan.side_b,
                counts=counts,
            )
        )
        return {"plan": plan, "testable": False, "reason": result.reason}

    def _run_sql(self, sql: str) -> pd.DataFrame:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            return pd.read_sql(sql, conn)
        finally:
            conn.close()


if __name__ == "__main__":
    import tempfile
    from datetime import date

    tmp_dir = tempfile.mkdtemp()
    db_path = str(Path(tmp_dir) / "history.db")

    agent = DataHandlerAgent(llm=None, db_path=db_path)

    def make_day(volume: int, weather: str) -> pd.DataFrame:
        return pd.DataFrame({
            "light_set": [500] * 840,
            "temperature_set": [22.0] * 840,
            "volume": [volume] * 840,
            "fan": [2] * 840,
            "occupancy": [i % 50 for i in range(840)],
            "central_forecast_text": [weather] * 840,
        })

    agent.ingest_day(make_day(45, "Cloudy"), date(2024, 1, 1))  # Monday
    agent.ingest_day(make_day(60, "Showers"), date(2024, 1, 2))  # Tuesday

    n = agent._run_sql("SELECT COUNT(*) AS n FROM readings").iloc[0]["n"]
    assert n == 1680, f"expected 1680 rows, got {n}"

    weekdays = agent._run_sql("SELECT DISTINCT weekday FROM readings ORDER BY weekday")
    assert list(weekdays["weekday"]) == ["Monday", "Tuesday"], weekdays

    times = agent._run_sql("SELECT MIN(time) AS min_t, MAX(time) AS max_t FROM readings").iloc[0]
    assert times["min_t"] == "08:00", times["min_t"]
    assert times["max_t"] == "21:59", times["max_t"]

    monday_n = agent._run_sql(
        "SELECT COUNT(*) AS n FROM readings WHERE weekday = 'Monday'"
    ).iloc[0]["n"]
    assert monday_n == 840, monday_n

    inv = agent.inventory()
    assert "Days of history: 2" in inv, inv
    assert "Monday: 1" in inv, inv
    assert "Showers" in inv, inv
    assert "45" in inv and "60" in inv, inv

    try:
        agent._run_sql("DELETE FROM readings")
        raise AssertionError("expected read-only guard to raise on DELETE")
    except AssertionError:
        raise
    except Exception:
        pass

    try:
        agent._run_sql("INSERT INTO readings (date) VALUES ('2024-01-03')")
        raise AssertionError("expected read-only guard to raise on INSERT")
    except AssertionError:
        raise
    except Exception:
        pass

    print("ok")
