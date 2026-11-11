"""Supervisor agent: classifies steps, routes research questions, gates on
data, dedups against existing nodes, and judges inferences. Self-contained —
talks to no other agent class; memory objects arrive as parameters."""

import sys
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

_DIR = Path(__file__).parent


def _prompt(name: str) -> str:
    return (_DIR / f"{name}.md").read_text(encoding="utf-8")


# Make sure agent/Agent_architecture (where memory.py lives) is importable,
# whether this module is run from there directly or imported with that
# directory already on sys.path.
_AGENT_ARCH_DIR = str(Path(__file__).resolve().parents[2])
if _AGENT_ARCH_DIR not in sys.path:
    sys.path.insert(0, _AGENT_ARCH_DIR)

try:
    from memory import EmpiricalNode, ExperimentRecord, venue_reference
except ImportError:
    from .memory import EmpiricalNode, ExperimentRecord, venue_reference


# ---------------------------------------------------------------------------
# Structured-output models
# ---------------------------------------------------------------------------

class StepClassification(BaseModel):
    step: str
    kind: str  # "fact" | "assumption" | "empirical" | "inference" | "unclear"


class StepClassificationBatch(BaseModel):
    classifications: list[StepClassification]


class ResearchQuestion(BaseModel):
    question: str
    prediction: str


class FactQuestion(BaseModel):
    question: str


class DedupVerdict(BaseModel):
    node_id: str
    verdict: str  # "same" | "opposite" | "different"


class DedupBatch(BaseModel):
    verdicts: list[DedupVerdict]


class InferenceTargetVerdict(BaseModel):
    target_id: str
    verdict: Optional[bool] = None


class InferenceJudgment(BaseModel):
    verdicts: list[InferenceTargetVerdict]


class OppositePrediction(BaseModel):
    opposite: str


class GateFeedback(BaseModel):
    feedback: str


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class SupervisorAgent:
    """Classifies steps, routes research questions, gates on data, dedups, and judges inferences."""

    def __init__(self, llm):
        self.llm = llm

    def classify_steps(self, steps: list[dict]) -> list[dict]:
        """Classifies reasoning steps shown WITH their parents. Each entry in
        steps is {"id", "statement", "parents"} (parents: list of step ids)."""
        lines = "\n".join(
            f"{s['id']}: {s['statement']} (parents: {', '.join(s['parents']) or 'none'})"
            for s in steps
        )
        structured_llm = self.llm.with_structured_output(StepClassificationBatch).with_retry(stop_after_attempt=3)
        prompt = _prompt("classify_steps").format(venue_reference=venue_reference(), steps=lines)
        result: StepClassificationBatch = structured_llm.invoke(prompt)
        return [{"step": c.step, "kind": c.kind} for c in result.classifications]

    def research_question(self, step: str) -> dict:
        """For an empirical step only (a venue claim); fact steps are settled by a
        lookup, not a research question. Routing between the two now happens at
        classification (classify_steps), not here."""
        structured_llm = self.llm.with_structured_output(ResearchQuestion).with_retry(stop_after_attempt=3)
        prompt = _prompt("research_question").format(step=step)
        result: ResearchQuestion = structured_llm.invoke(prompt)
        return {
            "question": result.question,
            "prediction": result.prediction,
        }

    def fact_question(self, step: str) -> str:
        """Rewrite a fact step (an assertion) into the lookup question that
        settles it; the websearch agent answers that question."""
        structured_llm = self.llm.with_structured_output(FactQuestion).with_retry(stop_after_attempt=3)
        prompt = _prompt("fact_question").format(step=step)
        result: FactQuestion = structured_llm.invoke(prompt)
        return result.question

    def gate_feedback(self, step: str, reason: str) -> str:
        structured_llm = self.llm.with_structured_output(GateFeedback).with_retry(stop_after_attempt=3)
        prompt = _prompt("gate_feedback").format(step=step, reason=reason)
        result: GateFeedback = structured_llm.invoke(prompt)
        return result.feedback

    def dedup(self, question: str, candidates: list[dict]) -> list[dict]:
        structured_llm = self.llm.with_structured_output(DedupBatch).with_retry(stop_after_attempt=3)
        prompt = _prompt("dedup").format(question=question, candidates=candidates)
        result: DedupBatch = structured_llm.invoke(prompt)
        return [{"node_id": v.node_id, "verdict": v.verdict} for v in result.verdicts]

    def judge_inference(self, statement: str, upstream: list[dict], targets: list[dict]) -> dict:
        structured_llm = self.llm.with_structured_output(InferenceJudgment).with_retry(stop_after_attempt=3)
        prompt = _prompt("judge_inference").format(statement=statement, upstream=upstream, targets=targets)
        result: InferenceJudgment = structured_llm.invoke(prompt)
        return {v.target_id: v.verdict for v in result.verdicts}

    def opposite_prediction(self, question: str, prediction: str) -> str:
        structured_llm = self.llm.with_structured_output(OppositePrediction).with_retry(stop_after_attempt=3)
        prompt = _prompt("opposite_prediction").format(question=question, prediction=prediction)
        result: OppositePrediction = structured_llm.invoke(prompt)
        return result.opposite

    def stamp_assumptions(self, parents: list[EmpiricalNode]) -> dict:
        # Deterministic, no LLM: the assumptions dict is stamped at dispatch time
        # from the node's parents and their live verdicts.
        return {parent.id: parent.conclusion for parent in parents}
