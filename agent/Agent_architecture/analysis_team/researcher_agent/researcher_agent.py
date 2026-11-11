"""Researcher agent: settles assumptions, designs experiment forms, and
writes up interpretations. Self-contained — talks to no other agent class."""

import sys
from pathlib import Path
from typing import Literal

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
    from memory import ExperimentRecord, venue_reference
except ImportError:
    from .memory import ExperimentRecord, venue_reference


# ---------------------------------------------------------------------------
# Structured-output models
# ---------------------------------------------------------------------------

class AssumptionLine(BaseModel):
    parent_id: str
    verdict: str        # "true" | "false" | "unresolved"
    range_text: str     # measured range like "+15 to +55", or ""
    given: str          # one plain sentence of what is taken as given


class AssumptionSynthesis(BaseModel):
    lines: list[AssumptionLine]


class ExperimentForm(BaseModel):
    measure: Literal[
        "light_set", "temperature_set", "volume", "fan",
        "temperature_sensor", "humidity", "co2", "noise", "light_sensor",
        "entered", "left", "occupancy", "outside_temperature",
    ]                       # numeric columns of the readings table
    measure_kind: Literal["raw", "residual"]
    unit_summary: Literal["mean", "total", "peak"]
    predicted_direction: Literal["A higher", "A lower"]
    notes: str = ""


class Interpretation(BaseModel):
    reason: str
    limitations: str


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class ResearcherAgent:
    """Settles assumptions, designs experiment forms, and interprets results."""

    def __init__(self, llm):
        self.llm = llm

    def synthesize_assumptions(self, parents: list[dict]) -> AssumptionSynthesis:
        structured_llm = self.llm.with_structured_output(AssumptionSynthesis).with_retry(stop_after_attempt=3)
        prompt = _prompt("synthesize_assumptions").format(parents=parents)
        return structured_llm.invoke(prompt)

    def design_experiment(self, question, prediction, plan, assumptions) -> ExperimentForm:
        structured_llm = self.llm.with_structured_output(ExperimentForm).with_retry(stop_after_attempt=3)
        prompt = _prompt("design_experiment").format(
            venue_reference=venue_reference(),
            question=question,
            prediction=prediction,
            unit=plan.unit,
            episode_condition=plan.episode_condition,
            side_a=plan.side_a,
            side_b=plan.side_b,
            assumptions=assumptions.model_dump(),
        )
        return structured_llm.invoke(prompt)

    def interpret(self, question, prediction, plan, form, statistics: dict, verdict) -> Interpretation:
        structured_llm = self.llm.with_structured_output(Interpretation).with_retry(stop_after_attempt=3)
        prompt = _prompt("interpret").format(
            question=question,
            prediction=prediction,
            unit=plan.unit,
            side_a=plan.side_a,
            side_b=plan.side_b,
            measure=form.measure,
            measure_kind=form.measure_kind,
            unit_summary=form.unit_summary,
            statistics=statistics,
            verdict=verdict,
        )
        return structured_llm.invoke(prompt)


# ---------------------------------------------------------------------------
# Plain templating (no LLM)
# ---------------------------------------------------------------------------

def record_prose(plan, form) -> dict:
    """Template the ExperimentRecord prose fields from the settled plan and form."""
    return {
        "method": f"two-group comparison on {plan.unit}s: {plan.side_a} vs {plan.side_b}",
        "setup": f"unit={plan.unit}; episode_condition={plan.episode_condition or 'n/a'}; measure={form.measure} ({form.measure_kind}); summary per unit={form.unit_summary}",
        "procedure": "fixed pipeline: fetch sides, drop first minute of each day, build units, one number per unit, Welch CI + permutation p, verdict from the CI",
        "metrics": f"{form.unit_summary} {form.measure_kind} {form.measure} per {plan.unit}; effect, SE, 95% CI, permutation p-value, n per side",
        "success_criteria": f"95% CI entirely past zero matching '{form.predicted_direction}' -> supported; contradicting it -> refuted; straddling zero -> inconclusive",
    }
