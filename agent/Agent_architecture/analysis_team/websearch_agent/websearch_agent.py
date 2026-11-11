"""Websearch agent: answers fact questions, optionally backed by a search
function. Self-contained — talks to no other agent class."""

from pathlib import Path

from pydantic import BaseModel

_DIR = Path(__file__).parent


def _prompt(name: str) -> str:
    return (_DIR / f"{name}.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Structured-output models
# ---------------------------------------------------------------------------

class FactAnswer(BaseModel):
    answer: str
    sources: str


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class WebsearchAgent:
    """Answers fact questions using an optional search function."""

    def __init__(self, llm, search_fn=None):
        self.llm = llm
        self.search_fn = search_fn

    def answer(self, question: str) -> FactAnswer:
        results = self.search_fn(question) if self.search_fn else "No search results available."
        structured_llm = self.llm.with_structured_output(FactAnswer).with_retry(stop_after_attempt=3)
        prompt = _prompt("answer_fact").format(question=question, results=results)
        return structured_llm.invoke(prompt)
