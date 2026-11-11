"""Event log for a research run.

Module state is a no-op by default: importing workflow code without a run
configured changes nothing. `run_research.py` calls start_run() to turn
logging on; workflow code labels events via set_context()/event(). Every LLM
exchange is captured by `handler`, a LangChain callback attached to every
model built by llm.get_llm().
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.callbacks import BaseCallbackHandler

_file = None
_hypothesis: str | None = None
_conversation: str | None = None


def start_run(log_dir: Path):
    """Create log_dir and open events.jsonl for append. Turns logging on."""
    global _file
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    _file = open(log_dir / "events.jsonl", "a", encoding="utf-8")


def end_run():
    """Close the event log file and turn logging off (start_run's counterpart)."""
    global _file
    if _file is not None:
        _file.close()
        _file = None


def set_context(hypothesis: str | None, conversation: str | None):
    """Set the labels stamped onto every following event (single-threaded run)."""
    global _hypothesis, _conversation
    _hypothesis = hypothesis
    _conversation = conversation


token_totals = {"input": 0, "output": 0, "total": 0}


def event(role: str, text: str, tokens: dict | None = None):
    """Append one JSON line under the current context. Roles: prompt/reply/note."""
    if _file is None:
        return
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "hypothesis": _hypothesis,
        "conversation": _conversation,
        "role": role,
        "text": text,
    }
    if tokens is not None:
        record["tokens"] = tokens
    line = json.dumps(record)
    _file.write(line + "\n")
    _file.flush()


def _extract_tokens(response) -> dict:
    """Token usage from an LLMResult: message.usage_metadata first, then
    llm_output token_usage; zeros if neither is present."""
    inp = out = tot = 0
    for gens in response.generations:
        for g in gens:
            usage = getattr(getattr(g, "message", None), "usage_metadata", None)
            if usage:
                inp += usage.get("input_tokens", 0)
                out += usage.get("output_tokens", 0)
                tot += usage.get("total_tokens", 0)
    if inp == out == tot == 0:
        usage = (getattr(response, "llm_output", None) or {}).get("token_usage") or {}
        inp = usage.get("prompt_tokens", usage.get("input_tokens", 0))
        out = usage.get("completion_tokens", usage.get("output_tokens", 0))
        tot = usage.get("total_tokens", inp + out)
    return {"input": inp, "output": out, "total": tot}


class LogHandler(BaseCallbackHandler):
    """Logs every LLM call's full prompt and reply under the current context."""

    def on_llm_start(self, serialized, prompts, **kwargs):
        event("prompt", "\n\n".join(prompts))

    def on_chat_model_start(self, serialized, messages, **kwargs):
        parts = []
        for batch in messages:
            for m in batch:
                parts.append(m.pretty_repr() if hasattr(m, "pretty_repr") else str(m))
        event("prompt", "\n\n".join(parts))

    def on_llm_end(self, response, **kwargs):
        parts = []
        for gens in response.generations:
            for g in gens:
                text = g.text
                if not text and getattr(g, "message", None) is not None:
                    text = str(g.message.content or getattr(g.message, "tool_calls", ""))
                parts.append(text or "")
        tokens = _extract_tokens(response)
        for k in token_totals:
            token_totals[k] += tokens[k]
        event("reply", "\n\n".join(p for p in parts if p) or "(empty reply)", tokens)


handler = LogHandler()
