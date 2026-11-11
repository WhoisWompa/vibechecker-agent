"""Hypothesis agent: proposes hypotheses and their reasoning graphs; repairs
on supervisor feedback. Self-contained — talks to no other agent class."""

import sys
from pathlib import Path

from pydantic import BaseModel

_DIR = Path(__file__).parent


def _prompt(name: str) -> str:
    return (_DIR / f"{name}.md").read_text(encoding="utf-8")


# Make sure the agent folder (where memory.py lives) is importable.
_AGENT_DIR = str(Path(__file__).resolve().parents[2])
if _AGENT_DIR not in sys.path:
    sys.path.insert(0, _AGENT_DIR)

from memory import venue_reference


def _fmt(steps: dict[str, "Step"]) -> str:
    return "\n".join(
        f"{sid}: {s.statement} (parents: {', '.join(s.parents) or 'none'})"
        for sid, s in steps.items()
    )


# ---------------------------------------------------------------------------
# Structured-output models
# ---------------------------------------------------------------------------

class HypothesisBatch(BaseModel):
    hypotheses: list[str]


class RankedBatch(BaseModel):
    ranked: list[str]


class Step(BaseModel):
    id: str
    statement: str
    parents: list[str]


class ReasoningSteps(BaseModel):
    steps: list[Step]


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class HypothesisAgent:
    """Proposes hypotheses and their reasoning graphs; repairs on supervisor feedback."""

    def __init__(self, llm):
        self.llm = llm

    def generate_batch(self, question_log: list[dict], memory_overview: str) -> list[str]:
        structured_llm = self.llm.with_structured_output(HypothesisBatch).with_retry(stop_after_attempt=3)
        prompt = _prompt("generate_batch").format(
            venue_reference=venue_reference(),
            memory_overview=memory_overview,
            question_log=question_log,
        )
        result: HypothesisBatch = structured_llm.invoke(prompt)
        return result.hypotheses

    def rank(self, hypotheses: list[str]) -> list[str]:
        structured_llm = self.llm.with_structured_output(RankedBatch).with_retry(stop_after_attempt=3)
        prompt = _prompt("rank").format(
            venue_reference=venue_reference(),
            hypotheses="\n".join(hypotheses),
        )
        result: RankedBatch = structured_llm.invoke(prompt)
        return result.ranked

    def reasoning_steps(self, hypothesis: str) -> dict[str, Step]:
        structured_llm = self.llm.with_structured_output(ReasoningSteps).with_retry(stop_after_attempt=3)
        prompt = _prompt("reasoning_steps").format(hypothesis=hypothesis)
        result: ReasoningSteps = structured_llm.invoke(prompt)
        return {s.id: s for s in result.steps}

    def revise(
        self,
        hypothesis: str,
        steps: dict[str, Step],
        broken_id: str,
        feedback: str,
    ) -> dict[str, Step]:
        structured_llm = self.llm.with_structured_output(ReasoningSteps).with_retry(stop_after_attempt=3)
        prompt = _prompt("revise").format(
            hypothesis=hypothesis,
            steps=_fmt(steps),
            broken_id=broken_id,
            broken_statement=steps[broken_id].statement,
            feedback=feedback,
        )
        result: ReasoningSteps = structured_llm.invoke(prompt)
        return {s.id: s for s in result.steps}
