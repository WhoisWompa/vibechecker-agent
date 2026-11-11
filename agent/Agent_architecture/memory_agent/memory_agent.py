"""Memory agent: the only door to the experience memory for the daily and
intraday agents (decisions rule 32). The supervisor works on the
ExperienceMemory directly and runs its own dedup, so this class never talks
to the supervisor — self-contained, memory and log arrive as constructor
parameters.

Two-level retrieval (decisions rule 32):
- Level 1 (flat): sim search over every fact/empirical question in memory,
  top-5, then one LLM call judges whether any candidate's stored answer is
  sufficient to answer the asked question (including free settlement of a
  proven-True claim's logical opposite, rule 6).
- Level 2 (subgraphs): only when level 1 is insufficient. Every hypothesis
  reachable from a level-1 candidate is expanded into its full subgraph
  (facts, assumptions, empiricals, inferences and their state), and one LLM
  call judges whether the fuller picture answers the question, is only
  partially settled, or is irrelevant.

Implements decisions rules 6 (free settlement of an opposite), 25 (outside
agents see prose only, never the raw graph), and 32 (three answer modes, log
what cannot be fully answered).
"""

import json
import sys
from pathlib import Path

from pydantic import BaseModel

_DIR = Path(__file__).parent


def _prompt(name: str) -> str:
    return (_DIR / f"{name}.md").read_text(encoding="utf-8")


# Make sure agent/ (where memory.py lives) is importable, whether this
# module is run directly or imported with that directory already on sys.path.
_AGENT_ARCH_DIR = str(Path(__file__).resolve().parents[1])  # memory_agent sits one level below the root here
for _p in (_AGENT_ARCH_DIR, str(_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from memory import FactNode, EmpiricalNode, InferenceNode, AssumptionNode
from mem_search import top_matches


# ---------------------------------------------------------------------------
# Structured-output models
# ---------------------------------------------------------------------------

class Level1Judgment(BaseModel):
    sufficient: bool
    answer: str


class Level2Judgment(BaseModel):
    outcome: str  # "answered" | "inconclusive" | "nothing"
    answer: str


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class MemoryAgent:
    """The only door to the experience memory for the daily and intraday agents."""

    def __init__(self, llm, memory, log_path):
        self.llm = llm
        self.memory = memory
        self.log_path = log_path

    # -- public --

    def ask(self, question: str) -> str:
        candidates = [
            {"node_id": node_id, "question": node.question}
            for node_id, node in self.memory.reasoning.items()
            if isinstance(node, (FactNode, EmpiricalNode))
        ]
        if not candidates:
            self._append_log({"question": question, "tag": "new gap", "matched_node": None})
            return "no evidence yet"

        matches = top_matches(question, candidates, k=5)

        # -- level 1: flat --

        candidate_info = [self._level1_info(m["node_id"]) for m in matches]
        structured_llm = self.llm.with_structured_output(Level1Judgment).with_retry(stop_after_attempt=3)
        prompt = _prompt("level1_sufficient").format(question=question, candidates=candidate_info)
        level1: Level1Judgment = structured_llm.invoke(prompt)
        if level1.sufficient and level1.answer.strip():
            return level1.answer

        # -- level 2: subgraphs --

        matched_ids = [m["node_id"] for m in matches]
        hyp_ids = self._hypotheses_touching(matched_ids)
        if not hyp_ids:
            self._append_log({"question": question, "tag": "new gap", "matched_node": None})
            return "no evidence yet"

        subgraphs = [self._subgraph(hyp_id) for hyp_id in hyp_ids]
        structured_llm = self.llm.with_structured_output(Level2Judgment).with_retry(stop_after_attempt=3)
        prompt = _prompt("level2_subgraphs").format(question=question, subgraphs=subgraphs)
        level2: Level2Judgment = structured_llm.invoke(prompt)

        if level2.outcome == "answered":
            return level2.answer
        elif level2.outcome == "inconclusive":
            top_node_id = matches[0]["node_id"] if matches else None
            self._append_log({"question": question, "tag": "urgency bump", "matched_node": top_node_id})
            return level2.answer
        else:
            self._append_log({"question": question, "tag": "new gap", "matched_node": None})
            return "no evidence yet"

    # -- level 1 helpers --

    def _level1_info(self, node_id: str) -> dict:
        node = self.memory.reasoning[node_id]
        if isinstance(node, FactNode):
            if not node.answer:
                return {"node_id": node_id, "kind": "fact", "question": node.question, "note": "not yet looked up"}
            return {
                "node_id": node_id,
                "kind": "fact",
                "question": node.question,
                "answer": node.answer,
                "sources": node.sources,
            }
        # EmpiricalNode
        info = {
            "node_id": node_id,
            "kind": "empirical",
            "question": node.question,
            "prediction": node.prediction,
            "verified": node.verified,
            "conclusion": node.conclusion,
        }
        if node.experiment is not None and node.experiment.statistics:
            info["statistics"] = node.experiment.statistics
        return info

    # -- level 2 helpers --

    def _hypotheses_touching(self, node_ids: list[str]) -> list[str]:
        """Distinct hypothesis ids where any given node_id is a terminal, an
        upstream key, or an upstream parent, in that hypothesis's subgraph."""
        touched = []
        for hyp_id, hyp in self.memory.hypotheses.items():
            for r in node_ids:
                referenced = (
                    r in hyp.nodes
                    or r in hyp.upstream
                    or any(r in parents for parents in hyp.upstream.values())
                )
                if referenced:
                    touched.append(hyp_id)
                    break
        return touched

    def _subgraph(self, hyp_id: str) -> dict:
        hyp = self.memory.hypotheses[hyp_id]
        step_ids = set(hyp.nodes) | set(hyp.upstream.keys())
        for parents in hyp.upstream.values():
            step_ids.update(parents)
        steps = [self._step_info(hyp_id, step_id) for step_id in sorted(step_ids)]
        return {
            "hypothesis_id": hyp_id,
            "statement": hyp.statement,
            "conclusion": hyp.conclusion,
            "steps": steps,
        }

    def _step_info(self, hyp_id: str, node_id: str) -> dict:
        node = self.memory.reasoning.get(node_id)
        if node is None:
            return {"node_id": node_id, "kind": "unknown"}
        if isinstance(node, FactNode):
            return {
                "node_id": node_id,
                "kind": "fact",
                "text": node.question,
                "answer": node.answer,
                "sources": node.sources,
            }
        if isinstance(node, AssumptionNode):
            return {"node_id": node_id, "kind": "assumption", "text": node.statement}
        if isinstance(node, EmpiricalNode):
            info = {
                "node_id": node_id,
                "kind": "empirical",
                "text": node.question,
                "verified": node.verified,
                "conclusion": node.conclusion,
            }
            if node.experiment is not None and node.experiment.statistics:
                info["statistics"] = node.experiment.statistics
            return info
        # InferenceNode
        return {
            "node_id": node_id,
            "kind": "inference",
            "text": node.statement,
            "verdicts": node.verdicts,
        }

    # -- unanswered-question log --

    def _load_log(self) -> list[dict]:
        path = Path(self.log_path)
        if not path.exists():
            return []
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _append_log(self, entry: dict):
        log = self._load_log()
        log.append(entry)
        path = Path(self.log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(log, f, indent=2)


if __name__ == "__main__":
    import tempfile

    try:
        from memory import ExperienceMemory, ExperimentRecord
    except ImportError:
        from .memory import ExperienceMemory, ExperimentRecord

    # -- stub LLM --

    class _StubStructured:
        def __init__(self, outputs):
            self._outputs = list(outputs)

        def with_retry(self, **kwargs):
            return self

        def invoke(self, prompt):
            return self._outputs.pop(0)

    class _StubLLM:
        def __init__(self):
            self._queue = {}  # model_name -> list of outputs

        def queue(self, model_cls, outputs):
            self._queue.setdefault(model_cls.__name__, []).extend(outputs)

        def with_structured_output(self, model_cls):
            outputs = self._queue.get(model_cls.__name__, [])
            return _StubStructured(outputs)

    log_dir = tempfile.mkdtemp()

    # (a) empty memory -> "no evidence yet" + new-gap log entry
    mem_a = ExperienceMemory()
    log_a = str(Path(log_dir) / "log_a.json")
    agent_a = MemoryAgent(_StubLLM(), mem_a, log_a)
    result_a = agent_a.ask("does lunch rush spike on weekdays?")
    assert result_a == "no evidence yet"
    log_entries_a = agent_a._load_log()
    assert len(log_entries_a) == 1
    assert log_entries_a[0]["tag"] == "new gap"
    assert log_entries_a[0]["matched_node"] is None

    # (b) level-1 sufficient stub -> returns the stubbed answer, no log entry
    mem_b = ExperienceMemory()
    rb = mem_b.add_empirical("does the lunch hour raise walk-ins?", "yes")
    mem_b.record_result(rb, True, ExperimentRecord(method="A-B", results="walk-ins +40% during lunch"))
    log_b = str(Path(log_dir) / "log_b.json")
    llm_b = _StubLLM()
    llm_b.queue(Level1Judgment, [Level1Judgment(sufficient=True, answer="Yes, lunch hour raises walk-ins by about 40%.")])
    agent_b = MemoryAgent(llm_b, mem_b, log_b)
    result_b = agent_b.ask("does the lunch hour raise walk-ins?")
    assert result_b == "Yes, lunch hour raises walk-ins by about 40%."
    assert agent_b._load_log() == []

    # (c) level-1 insufficient + level-2 "inconclusive" on a memory where the
    # matched node wires into a hypothesis -> urgency-bump log entry
    mem_c = ExperienceMemory()
    rc = mem_c.add_empirical("does the lunch hour raise walk-ins?", "yes")
    mem_c.record_result(rc, None, ExperimentRecord(method="A-B", results="mixed signal so far"))
    hc = mem_c.add_hypothesis("weekday lunch is the peak")
    mem_c.connect_to_hypothesis(rc, hc)
    log_c = str(Path(log_dir) / "log_c.json")
    llm_c = _StubLLM()
    llm_c.queue(Level1Judgment, [Level1Judgment(sufficient=False, answer="")])
    llm_c.queue(Level2Judgment, [Level2Judgment(outcome="inconclusive", answer="Lunch hour's effect on walk-ins is still unsettled.")])
    agent_c = MemoryAgent(llm_c, mem_c, log_c)
    result_c = agent_c.ask("does the lunch hour raise walk-ins?")
    assert result_c == "Lunch hour's effect on walk-ins is still unsettled."
    log_entries_c = agent_c._load_log()
    assert len(log_entries_c) == 1
    assert log_entries_c[0]["tag"] == "urgency bump"
    assert log_entries_c[0]["matched_node"] == rc

    # (d) level-1 insufficient + level-2 "nothing" -> new-gap entry + "no evidence yet"
    mem_d = ExperienceMemory()
    rd = mem_d.add_empirical("does the lunch hour raise walk-ins?", "yes")
    mem_d.record_result(rd, None, ExperimentRecord(method="A-B", results="mixed signal so far"))
    hd = mem_d.add_hypothesis("weekday lunch is the peak")
    mem_d.connect_to_hypothesis(rd, hd)
    log_d = str(Path(log_dir) / "log_d.json")
    llm_d = _StubLLM()
    llm_d.queue(Level1Judgment, [Level1Judgment(sufficient=False, answer="")])
    llm_d.queue(Level2Judgment, [Level2Judgment(outcome="nothing", answer="")])
    agent_d = MemoryAgent(llm_d, mem_d, log_d)
    result_d = agent_d.ask("does the lunch hour raise walk-ins?")
    assert result_d == "no evidence yet"
    log_entries_d = agent_d._load_log()
    assert len(log_entries_d) == 1
    assert log_entries_d[0]["tag"] == "new gap"
    assert log_entries_d[0]["matched_node"] is None

    print("ok")
