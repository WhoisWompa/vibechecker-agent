"""The live demo: one page, real runs, watched as they happen.

    uv run python run_live.py

Opens the dashboard. Everything is driven from the page: pick how many days,
press RUN SIMULATION (a real run_navigation run — same runner, same agents),
or press RUN RESEARCH to fire the analysis team over the recorded days.
The cafe streams minute by minute on the left; the agents' activity, in
plain words, on the right.

--pace stretches a simulated day (default ~15 s); model-call pauses on top
are real latency. --memory seeds the experience memory for a
"the room already knows" run. Nothing is staged: the page only shows files
and events the run writes anyway.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ARCH = ROOT / "agent" / "Agent_architecture"
for _p in (ARCH, ARCH / "navigation_team"):
    sys.path.insert(0, str(_p))

import _paths  # noqa: E402,F401
from runner import NavRun  # noqa: E402

PAGE = ROOT / "scripts" / "live_dashboard.html"
MINUTE_COLS = ("occupancy", "entered", "left", "light_set", "temperature_set", "volume", "fan",
               "temperature_sensor", "co2", "noise", "light_sensor", "outside_temperature")
FILE_WHITELIST = ("summary.json", "question_bank.json", "checklist.md", "plans/", "days/", "analysis/", "notes/")


class Feed:
    """Thread-safe append-only event list the page polls with ?after=<n>."""

    def __init__(self) -> None:
        self._items: list[dict] = []
        self._lock = threading.Lock()

    def append(self, kind: str, **fields) -> None:
        with self._lock:
            self._items.append({"i": len(self._items), "kind": kind, **fields})

    def after(self, n: int) -> list[dict]:
        with self._lock:
            return self._items[n:]

    def clear(self) -> None:
        with self._lock:
            self._items = []


class LiveRun(NavRun):
    """The real NavRun with two taps on it: every simulated minute goes to the
    feed (paced so a day is watchable), and every runner log line too."""

    def __init__(self, feed: Feed, pace_ms: float, **kwargs) -> None:
        super().__init__(**kwargs)
        self._feed = feed
        self._pace = pace_ms / 1000.0

    def log(self, msg: str) -> None:
        super().log(msg)
        self._feed.append("log", msg=str(msg))

    def _build_cafe(self):
        cafe = super()._build_cafe()
        feed, pace = self._feed, self._pace
        real_tick = cafe.tick

        def tick(n: int = 1):
            real_tick(n)
            df = cafe.day_df()
            if len(df):
                last = df.iloc[-1]
                row = {}
                for c in MINUTE_COLS:
                    if c in df.columns:
                        v = last[c]
                        # NaN (row 0 has no readings yet) must become null: NaN is not valid JSON
                        row[c] = None if v is None or v != v else round(float(v), 1)
                t = len(df) - 1
                feed.append("minute", t=t, date=cafe.date.isoformat(), weekday=cafe.weekday, **row)
                if t >= 839:
                    feed.append("day_close", date=cafe.date.isoformat())
            if pace:
                time.sleep(pace)

        cafe.tick = tick
        return cafe

    def _after_close(self, day_number: int, date: str) -> None:
        self._feed.append("analysis", date=date, status="start")
        super()._after_close(day_number, date)
        self._feed.append("analysis", date=date, status="end")


_last_date_cache: dict = {"t": 0.0, "v": None}


def last_recorded_date(state: Path) -> str | None:
    now = time.time()
    if now - _last_date_cache["t"] > 5:  # polled twice a second by the page; the answer changes once a day
        days = sorted(p.stem for p in (state / "days").glob("*.json")) if (state / "days").exists() else []
        _last_date_cache.update(t=now, v=days[-1] if days else None)
    return _last_date_cache["v"]


def serve(state: Path, feed: Feed, status: dict, submit) -> ThreadingHTTPServer:
    html = PAGE.read_bytes()

    def tail_jsonl(path: Path, after: int) -> list:
        if not path.exists():
            return []
        lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        out = []
        for i, l in enumerate(lines[after:], start=after):
            try:
                out.append({"i": i, **json.loads(l)})
            except json.JSONDecodeError:
                pass
        return out

    class Handler(BaseHTTPRequestHandler):
        def _send(self, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj) -> None:
            self._send(json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))

        def do_GET(self):  # noqa: N802
            path, _, query = self.path.partition("?")
            q = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
            after = int(q.get("after", 0))
            if path == "/":
                self._send(html, "text/html; charset=utf-8")
            elif path == "/status":
                self._json({**status, "last_date": last_recorded_date(state)})
            elif path == "/feed":
                self._json(feed.after(after))
            elif path == "/messages":
                self._json(tail_jsonl(state / "messages.jsonl", after))
            elif path == "/aevents":
                date = q.get("date", "")
                self._json(tail_jsonl(state / "analysis" / f"run_{date}" / "events.jsonl", after))
            elif path == "/file":
                name = q.get("name", "").replace("%2F", "/")
                ok = any(name == w or (w.endswith("/") and name.startswith(w) and ".." not in name)
                         for w in FILE_WHITELIST)
                f = state / name
                if ok and f.exists():
                    self._send(f.read_bytes(),
                               "application/json" if name.endswith(".json") else "text/plain; charset=utf-8")
                else:
                    self.send_error(404)
            else:
                self.send_error(404)

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                body = {}
            if self.path.startswith("/run"):
                accepted = submit({"cmd": "run", "days": int(body.get("days", 3)),
                                   "fresh": bool(body.get("fresh", True))})
            elif self.path.startswith("/analyze"):
                accepted = submit({"cmd": "analyze"})
            else:
                self.send_error(404)
                return
            self._json({"ok": accepted})

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--state", default="state_nav/live-demo")
    p.add_argument("--seed", type=int, default=3)
    p.add_argument("--start", default="2024-04-01")
    p.add_argument("--preset", default="demo")
    p.add_argument("--note", action="append", default=[], help="owner's note for day 1; repeatable")
    p.add_argument("--memory", default=None, help="seed memory, e.g. a finished run's memory.json")
    p.add_argument("--fast", action="store_true", help="heuristic mode: no model, for rehearsal")
    p.add_argument("--pace", type=float, default=0.0,
                   help="extra ms per simulated minute; at 0 a day takes ~15-20 s (the sim's own speed)")
    p.add_argument("--research-questions", type=int, default=12,
                   help="bank questions per research pass; fewer = a shorter live research segment")
    p.add_argument("--budget-calls", type=int, default=2000)
    p.add_argument("--budget-usd", type=float, default=10.0)
    p.add_argument("--no-open", action="store_true", help="don't open the browser (testing)")
    a = p.parse_args()

    state = Path(a.state)
    feed = Feed()
    pending: dict = {}
    wake = threading.Event()
    status = {"state": "idle", "mode": "heuristic" if a.fast else "model", "start": a.start,
              "state_dir": str(state), "error": ""}

    def submit(cmd: dict) -> bool:
        if status["state"] in ("running", "analysis") or pending:
            return False
        pending.update(cmd)
        wake.set()
        return True

    server = serve(state, feed, status, submit)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"live demo: {url}", flush=True)
    if not a.no_open:
        webbrowser.open(url)

    while True:
        wake.wait()
        wake.clear()
        cmd = dict(pending)
        pending.clear()
        if cmd.get("cmd") == "run":
            feed.clear()
            if cmd.get("fresh") and state.exists():
                for wait in (0, 0.5, 2.0):  # a just-finished run may still be letting go of a file
                    time.sleep(wait)
                    try:
                        shutil.rmtree(state)
                        break
                    except OSError:
                        pass
                else:
                    feed.append("log", msg="could not fully clear the old state folder (a file is in use); "
                                           "running on top of what's there")
            status.update(state="running", error="")
            try:
                lr = LiveRun(feed, a.pace, days=cmd.get("days", 3), seed=a.seed, start_date=a.start,
                             state_dir=str(state), mode="heuristic" if a.fast else "model", notes=a.note,
                             memory_path=a.memory, verbose=True,
                             cafe_knobs={"preset": a.preset} if a.preset else {},
                             budget_calls=a.budget_calls, budget_usd=a.budget_usd, analysis="weekly")
                lr.after_close_kwargs = {"max_questions": a.research_questions}
                lr.run()
                status["state"] = "done"
                feed.append("finished")
            except BaseException:
                status.update(state="crashed", error=traceback.format_exc()[-1500:])
                feed.append("crashed", error=status["error"])
                print(traceback.format_exc(), flush=True)
        elif cmd.get("cmd") == "analyze":
            date = last_recorded_date(state)
            if not date:
                feed.append("log", msg="no recorded day to analyse yet — run the simulation first")
                continue
            status.update(state="analysis", error="")
            feed.append("analysis", date=date, status="start")
            try:
                from after_close import AfterClose
                AfterClose(str(state), max_questions=a.research_questions).run(date)
                status["state"] = "done"
            except BaseException:
                status.update(state="crashed", error=traceback.format_exc()[-1500:])
                print(traceback.format_exc(), flush=True)
            feed.append("analysis", date=date, status="end")


if __name__ == "__main__":
    main()
