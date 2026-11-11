"""Token and message tracking for every model call the navigation team makes.

Lionel's ask (5 Sep): track the messages and the token usage for each loop so
we can show how efficient the system is, barebone first, then tuned.

One ``Tracker`` is attached to every model the runner builds (through
``llm.get_llm(..., callbacks=[tracker])``). It records, per call: which agent,
which simulated day, which loop (morning plan, an intraday check, ...), the
prompt and the reply, tokens in and out, and wall-clock latency. Records go
to ``tokens.jsonl`` (numbers only) and ``messages.jsonl`` (full text) under
the run's state directory, and ``summary()`` rolls them up per day and per
agent for the eval table.

Cost is estimated from per-million-token rates that can be overridden with
``NAV_INPUT_USD_PER_M`` and ``NAV_OUTPUT_USD_PER_M``; the default is the
published Haiku 4.5 rate at the time of writing.
"""

from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.callbacks import BaseCallbackHandler

INPUT_USD_PER_M = float(os.environ.get("NAV_INPUT_USD_PER_M", "1.0"))
OUTPUT_USD_PER_M = float(os.environ.get("NAV_OUTPUT_USD_PER_M", "5.0"))
# list prices per million tokens by model family (Anthropic first-party rates, Sep 2026; Bedrock is close);
# the env defaults above are the Haiku rates and cover any model not named here
RATES = [("haiku", 1.0, 5.0), ("sonnet-5", 2.0, 10.0), ("sonnet", 3.0, 15.0), ("opus", 5.0, 25.0), ("fable", 10.0, 50.0)]


def rates_for(model: str) -> tuple:
    m = (model or "").lower()
    for key, i, o in RATES:
        if key in m:
            return i, o
    return INPUT_USD_PER_M, OUTPUT_USD_PER_M


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, default=str)
    except Exception:
        return str(content)


class Tracker(BaseCallbackHandler):
    """Counts every model call. Set ``context`` before each loop so the call is labelled."""

    def __init__(self, state_dir: Optional[str] = None, budget_calls: int = 80, budget_usd: float = 1.0) -> None:
        super().__init__()
        self.records: List[Dict[str, Any]] = []
        self.failures: List[Dict[str, Any]] = []
        self.starts = 0                      # every attempt, finished or not
        self.budget_calls = budget_calls     # hard stop for the whole run (Lionel, 5 Sep: no runaway loops)
        self.budget_usd = budget_usd
        self.tripped: Optional[str] = None
        self._starts: Dict[Any, Dict[str, Any]] = {}
        self.day: Optional[int] = None
        self.date: Optional[str] = None
        self.loop: str = ""
        self.purpose: str = ""                      # what the next call is for: "morning plan", "memory match", ...
        self.questions: List[str] = []              # the questions (or events) behind the next call
        self.free_answers: List[Dict[str, Any]] = []  # questions answered without a model (a word-for-word memory hit)
        self.state_dir = Path(state_dir) if state_dir else None
        if self.state_dir:
            self.state_dir.mkdir(parents=True, exist_ok=True)

    # -- labelling --

    def context(self, day: Optional[int] = None, date: Optional[str] = None, loop: Optional[str] = None,
                purpose: Optional[str] = None, questions: Optional[List[str]] = None) -> None:
        if day is not None:
            self.day = day
        if date is not None:
            self.date = date
        if loop is not None:
            self.loop = loop
        if purpose is not None:
            self.purpose = purpose
            self.questions = list(questions or [])  # a new purpose starts with its own questions
        elif questions is not None:
            self.questions = list(questions)

    def free(self, question: str, how: str = "exact") -> None:
        """A question answered with no model call at all (the memory door found a word-for-word match)."""
        self.free_answers.append({"day": self.day, "date": self.date, "loop": self.loop, "question": question, "how": how})

    # -- LangChain hooks --

    def within_budget(self) -> bool:
        """False once the run has spent its call or dollar budget; the reason is kept in ``tripped``."""
        if self.tripped:
            return False
        if self.starts >= self.budget_calls:
            self.tripped = f"call budget spent: {self.starts} of {self.budget_calls} model calls"
        elif sum(r["usd"] for r in self.records) >= self.budget_usd:
            self.tripped = f"dollar budget spent: ${sum(r['usd'] for r in self.records):.4f} of ${self.budget_usd:.2f}"
        return self.tripped is None

    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None,
                            tags=None, metadata=None, **kwargs) -> None:
        self.starts += 1
        prompt = "\n\n".join(_text(m.content) for batch in messages for m in batch)
        agent = (metadata or {}).get("agent") or (tags[0] if tags else "unknown")
        self._starts[run_id] = {"t0": time.time(), "prompt": prompt, "agent": agent,
                                "model": (metadata or {}).get("ls_model_name")
                                or (serialized or {}).get("name", "")}

    def on_llm_start(self, serialized, prompts, *, run_id, parent_run_id=None,
                     tags=None, metadata=None, **kwargs) -> None:
        self.starts += 1
        self._starts[run_id] = {"t0": time.time(), "prompt": "\n\n".join(prompts),
                                "agent": (metadata or {}).get("agent") or "unknown",
                                "model": (serialized or {}).get("name", "")}

    def on_llm_end(self, response, *, run_id, parent_run_id=None, **kwargs) -> None:
        start = self._starts.pop(run_id, {"t0": time.time(), "prompt": "", "agent": "unknown", "model": ""})
        usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        reply = ""
        model = start["model"]
        for gens in getattr(response, "generations", []) or []:
            for g in gens:
                msg = getattr(g, "message", None)
                u = getattr(msg, "usage_metadata", None) or {}
                for k in usage:
                    usage[k] += int(u.get(k) or 0)
                meta = getattr(msg, "response_metadata", None) or {}
                model = meta.get("model") or meta.get("model_name") or model
                text = getattr(g, "text", "") or (_text(msg.content) if msg is not None else "")
                calls = getattr(msg, "tool_calls", None) or []
                if calls:
                    text = (text + "\n" if text else "") + json.dumps([c.get("args") for c in calls], default=str)
                reply += text
        llm_output = getattr(response, "llm_output", None) or {}
        if not usage["total_tokens"] and llm_output.get("usage"):
            u = llm_output["usage"]
            usage["input_tokens"] = int(u.get("input_tokens") or 0)
            usage["output_tokens"] = int(u.get("output_tokens") or 0)
        if not usage["total_tokens"]:
            usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
        rec = {
            "day": self.day, "date": self.date, "loop": self.loop, "agent": start["agent"],
            "purpose": self.purpose, "questions": list(self.questions),
            "model": model, "latency_s": round(time.time() - start["t0"], 3), **usage,
            "usd": round(usage["input_tokens"] * rates_for(model)[0] / 1e6
                         + usage["output_tokens"] * rates_for(model)[1] / 1e6, 6),
        }
        self.records.append(rec)
        self._write("tokens.jsonl", rec)
        self._write("messages.jsonl", {**rec, "prompt": start["prompt"], "reply": reply})

    def on_llm_error(self, error, *, run_id, parent_run_id=None, **kwargs) -> None:
        start = self._starts.pop(run_id, {"t0": time.time(), "agent": "unknown", "prompt": ""})
        rec = {"day": self.day, "date": self.date, "loop": self.loop, "agent": start["agent"],
               "purpose": self.purpose, "questions": list(self.questions),
               "error": repr(error)[:300], "latency_s": round(time.time() - start["t0"], 3)}
        self.failures.append(rec)
        self._write("tokens.jsonl", {**rec, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0})

    # -- reporting --

    def _write(self, name: str, rec: Dict[str, Any]) -> None:
        if not self.state_dir:
            return
        with open(self.state_dir / name, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")

    def summary(self, by: str = "day") -> List[Dict[str, Any]]:
        """Roll-up per ``by`` ("day", "agent", "loop", or "day,agent")."""
        keys = [k.strip() for k in by.split(",")]
        agg: Dict[tuple, Dict[str, Any]] = defaultdict(lambda: {
            "calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "usd": 0.0, "latency_s": 0.0})
        for r in self.records:
            key = tuple(r.get(k) for k in keys)
            a = agg[key]
            a["calls"] += 1
            for k in ("input_tokens", "output_tokens", "total_tokens", "usd", "latency_s"):
                a[k] += r.get(k, 0) or 0
        rows = []
        for key, a in sorted(agg.items(), key=lambda kv: tuple(str(x) for x in kv[0])):
            row = dict(zip(keys, key))
            row.update({k: (round(v, 4) if isinstance(v, float) else v) for k, v in a.items()})
            row["mean_latency_s"] = round(a["latency_s"] / a["calls"], 2) if a["calls"] else 0.0
            rows.append(row)
        return rows

    def by_question(self) -> List[Dict[str, Any]]:
        """What each question cost. A call that served several questions (the door's batched match and
        answer calls) is split evenly between them; answers found without a model count as free."""
        agg: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
            "calls_share": 0.0, "total_tokens": 0.0, "usd": 0.0, "purposes": set(), "free_answers": 0})
        for r in self.records:
            qs = r.get("questions") or []
            if not qs:
                continue
            share = 1.0 / len(qs)
            for q in qs:
                a = agg[q]
                a["calls_share"] += share
                a["total_tokens"] += (r.get("total_tokens") or 0) * share
                a["usd"] += (r.get("usd") or 0) * share
                a["purposes"].add(r.get("purpose") or "")
        for f in self.free_answers:
            agg[f["question"]]["free_answers"] += 1
        rows = []
        for q, a in sorted(agg.items(), key=lambda kv: -kv[1]["usd"]):
            rows.append({"question": q, "calls_share": round(a["calls_share"], 2), "total_tokens": int(round(a["total_tokens"])),
                         "usd": round(a["usd"], 5), "purposes": sorted(p for p in a["purposes"] if p),
                         "free_answers": a["free_answers"]})
        return rows

    def totals(self) -> Dict[str, Any]:
        t = {"calls": len(self.records), "attempts": self.starts, "failures": len(self.failures),
             "budget_tripped": self.tripped,
             "input_tokens": sum(r["input_tokens"] for r in self.records),
             "output_tokens": sum(r["output_tokens"] for r in self.records),
             "usd": round(sum(r["usd"] for r in self.records), 4)}
        t["total_tokens"] = t["input_tokens"] + t["output_tokens"]
        return t

    def table(self, by: str = "day,agent") -> str:
        rows = self.summary(by)
        if not rows:
            return "(no model calls)"
        keys = [k.strip() for k in by.split(",")]
        head = keys + ["calls", "in", "out", "usd", "lat"]
        lines = ["  ".join(f"{h:>10}" for h in head)]
        for r in rows:
            vals = [str(r.get(k)) for k in keys] + [str(r["calls"]), str(r["input_tokens"]),
                                                     str(r["output_tokens"]), f"{r['usd']:.4f}",
                                                     f"{r['mean_latency_s']:.1f}s"]
            lines.append("  ".join(f"{v:>10}" for v in vals))
        return "\n".join(lines)
