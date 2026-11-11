"""Double-clickable entry point: runs one full research pass while streaming
every LLM exchange to a live web page (agent/live_view.html), and persists the
event log to agent/logs/run_<timestamp>/events.jsonl for replay.

Optional command-line argument: N = use only the first N questions.
"""

import json
import os
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_AGENT_DIR = Path(__file__).resolve().parent
for _p in (str(_AGENT_DIR), str(_AGENT_DIR / "analysis_team")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import run_log  # noqa: E402

ROOT = _AGENT_DIR.parents[1]  # vibechecker/: this copy sits at agent/Agent_architecture/
# Lionel's fake-data demo inputs. The joined loop does not use this entry point: run_analysis.py at
# the project root runs the same research pass over a navigation state folder (bank + history.db).
DB_PATH = ROOT / "fake data" / "history.db"
QUESTIONS_PATH = ROOT / "fake data" / "questions.md"
MEMORY_PATH = ROOT / "fake data" / "experience_memory.json"


def clean_memory():
    """Demo mode: a fake-data run must not leave experience memory behind.
    Remove this (and its call sites) when switching to real persistent runs."""
    MEMORY_PATH.unlink(missing_ok=True)


def parse_questions(path) -> list:
    """Every markdown line starting with '- ' is one question."""
    return [
        line[2:].strip()
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.startswith("- ")
    ]


def start_server(log_dir: Path, questions: list):
    """Background HTTP server on a free localhost port:
    / -> live_view.html, /events -> events.jsonl as a JSON array,
    /questions -> the question list, /status -> {"state": ...},
    /run -> sets run_event (starts a run over the full question list).
    Returns (server, run_event, status)."""
    html = (_AGENT_DIR / "live_view.html").read_bytes()
    events_path = Path(log_dir) / "events.jsonl"
    run_event = threading.Event()
    status = {"state": "idle"}

    def stop_run():
        status["state"] = "stopped"
        run_log.event("note", "run stopped by user via Stop button")
        t = threading.Timer(0.3, lambda: os._exit(0))
        t.daemon = True
        t.start()

    class Handler(BaseHTTPRequestHandler):
        def _handle_run(self):
            run_event.set()
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/":
                body, ctype = html, "text/html; charset=utf-8"
            elif self.path == "/status":
                body = json.dumps(status).encode("utf-8")
                ctype = "application/json"
            elif self.path == "/questions":
                body = json.dumps(questions).encode("utf-8")
                ctype = "application/json"
            elif self.path.startswith("/run"):
                self._handle_run()
                return
            elif self.path == "/stop":
                body, ctype = b'{"ok": true}', "application/json"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                stop_run()
                return
            elif self.path == "/events":
                events = []
                if events_path.exists():
                    for line in events_path.read_text(encoding="utf-8").splitlines():
                        if line.strip():
                            events.append(json.loads(line))
                body = json.dumps(events).encode("utf-8")
                ctype = "application/json"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path.startswith("/run"):
                self._handle_run()
            elif self.path == "/stop":
                body = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                stop_run()
            else:
                self.send_error(404)

        def log_message(self, *args):
            pass  # keep the terminal clean

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, run_event, status


def main():
    questions = parse_questions(QUESTIONS_PATH)
    if len(sys.argv) > 1:
        questions = questions[: int(sys.argv[1])]

    clean_memory()
    print("demo mode: memory file cleaned at start and after every run")

    log_dir = _AGENT_DIR / "logs" / f"run_{time.strftime('%Y%m%d_%H%M%S')}"
    run_log.start_run(log_dir)
    server, run_event, status = start_server(log_dir, questions)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"live view: {url}")
    print(f"event log: {log_dir}")
    webbrowser.open(url)

    from workflow import run_research_pass

    while True:
        status["state"] = "idle"
        print("waiting for Start to be pressed on the page...")
        run_event.wait()
        run_event.clear()
        status["state"] = "running"
        try:
            summary = run_research_pass(questions, str(DB_PATH), str(MEMORY_PATH))
            run_log.event("note", f"run finished: {summary}")
            print(summary)
            t = run_log.token_totals
            print(f"tokens: {t['input']} in / {t['output']} out / {t['total']} total")
        except BaseException:
            status["state"] = "crashed"
            run_log.set_context(None, None)
            run_log.event("note", "run crashed:\n" + traceback.format_exc())
            print(traceback.format_exc())
        finally:
            clean_memory()


if __name__ == "__main__":
    main()
