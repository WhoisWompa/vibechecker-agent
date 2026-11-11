"""The navigation team's door to memory, and the question bank behind it.

Wraps Lionel's ``MemoryAgent`` (the only door to the experience memory) so
that every question the daily or intraday agent asks is answered the same
way, and every question memory cannot answer lands in the question bank with
who asked it, on which day, and in what situation. The bank is what the
reflection agent reads after close.

Without a memory file (the barebone run) the memory is empty: every question
gets "no evidence yet" and is banked. No embedding model is loaded on that
path, so the barebone needs no extra packages.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_ARCH = Path(__file__).resolve().parents[1]
for p in (str(_ARCH), str(_ARCH / "memory_agent"), str(_ARCH / "analysis_team" / "supervisor_agent")):
    if p not in sys.path:
        sys.path.insert(0, p)

from pydantic import BaseModel  # noqa: E402

from memory import EmpiricalNode, ExperienceMemory, FactNode  # noqa: E402
from memory_agent import MemoryAgent  # noqa: E402
from sim_search import top_candidates  # noqa: E402

NO_EVIDENCE = "no evidence yet"
_DIR = Path(__file__).parent


class _Verdict(BaseModel):
    question_index: int
    node_id: str
    verdict: str  # same | opposite | different


class _MatchAll(BaseModel):
    verdicts: List[_Verdict]


class _Answer(BaseModel):
    question_index: int
    answer: str


class _AnswerAll(BaseModel):
    answers: List[_Answer]


class QuestionBank:
    """One JSON list, shared with the analysis team."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def append(self, entry: Dict[str, Any]) -> None:
        bank = self.load()
        bank.append(entry)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(bank, f, indent=2)

    def open_questions(self) -> List[Dict[str, Any]]:
        return [q for q in self.load() if q.get("status", "open") == "open"]


class MemoryDoor:
    """ask(question) -> plain answer; unknowns go to the bank with context."""

    def __init__(self, llm, bank_path: str, memory_path: Optional[str] = None, budget_ok=None, tracker=None) -> None:
        self.budget_ok = budget_ok or (lambda: True)
        self.tracker = tracker  # labels each model call with its purpose and the questions behind it
        self.bank = QuestionBank(bank_path)
        self.llm = llm
        self.memory_path = str(memory_path) if memory_path else None
        self.memory: ExperienceMemory = ExperienceMemory()
        self.agent: MemoryAgent = MemoryAgent(llm, self.memory, str(self.bank.path))
        self.reload()
        self.context: Dict[str, Any] = {}
        self.asked: List[Dict[str, Any]] = []
        self.cache: Dict[str, Dict[str, Any]] = {}  # today's answers by question: a repeat costs nothing

    def knows(self, question: str) -> bool:
        """True if this question would be answered without a model: already answered today, or held word for word."""
        return self._norm(question) in self.cache or self._exact(question) is not None

    def reload(self) -> None:
        """Read the memory file again: the analysis team rewrites it after close."""
        mp = Path(self.memory_path) if self.memory_path else None
        self.memory = ExperienceMemory.load(str(mp)) if mp and mp.exists() else ExperienceMemory()
        self.agent = MemoryAgent(self.llm, self.memory, str(self.bank.path))
        self.agent._append_log = self._bank_entry  # type: ignore[method-assign]

    def new_day(self) -> None:
        """Forget today's answers and re-read memory: the analysis team may have changed it overnight."""
        self.cache.clear()
        self.reload()

    def _cached(self, question: str, asked_by: str, t: Optional[int]) -> Optional[Dict[str, Any]]:
        hit = self.cache.get(self._norm(question))
        if hit is None:
            return None
        rec = {**hit, "asked_by": asked_by, "t": t, "how": "cached"}
        self.asked.append(rec)
        if self.tracker is not None:
            self.tracker.free(question, "cached")
        return rec

    def _remember(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        self.cache[self._norm(rec["question"])] = {k: v for k, v in rec.items() if k not in ("asked_by", "t", "how")}
        return rec

    def _bank_entry(self, entry: Dict[str, Any]) -> None:
        self.bank.append({**entry, **self.context, "status": "open"})

    # -- a word-for-word repeat of a memory question is answered by the door itself: no embeddings, no model --

    @staticmethod
    def _norm(q: str) -> str:
        return " ".join(str(q or "").lower().split()).rstrip("?").strip()

    def _exact(self, question: str):
        want = self._norm(question)
        for nid, node in self.memory.reasoning.items():
            if isinstance(node, (EmpiricalNode, FactNode)) and self._norm(node.question) == want:
                return nid, node
        return None

    @staticmethod
    def _evidence(node: EmpiricalNode) -> str:
        """One clause on how strong the test was, from the experiment's statistics when Lionel's
        pipeline computed them (p-value and days per side), else from the write-up's method."""
        exp = getattr(node, "experiment", None)
        stats = getattr(exp, "statistics", None) or {}
        if stats.get("p_value") is not None:
            sides = stats.get("n_per_side") or {}
            n = ", ".join(f"{k} {v}" for k, v in sides.items()) if isinstance(sides, dict) else ""
            return f"p = {stats['p_value']:.2f}" + (f"; {n}" if n else "")
        method = getattr(exp, "method", "") or ""
        return method if method else "tested in this cafe"

    def _answer_from_node(self, question: str, nid: str, node, asked_by: str, t: Optional[int]) -> Dict[str, Any]:
        if isinstance(node, FactNode):
            if node.answer:
                answer, known = f"{node.answer} (looked up: {node.sources or 'no source given'})", True
            else:
                answer, known = NO_EVIDENCE, False
                self._bank_entry({"question": question, "tag": "urgency bump", "matched_node": nid})
        elif node.conclusion is True:
            answer, known = f"{node.prediction} (tested true in this cafe, {self._evidence(node)})", True
        elif node.conclusion is False:
            answer, known = f"tested and found false in this cafe: {node.prediction}", True
        else:  # asked before, tested, not settled: Lionel's rule bumps its urgency for the analysis team
            answer, known = f"not yet confirmed in this cafe; memory's current guess: {node.prediction}", False
            self._bank_entry({"question": question, "tag": "urgency bump", "matched_node": nid})
        rec = {"question": question, "answer": answer, "known": known, "asked_by": asked_by, "t": t,
               "matched_node": nid, "how": "exact"}
        self.asked.append(rec)
        if self.tracker is not None:
            self.tracker.free(question, "exact")
        return self._remember(rec)

    def _label(self, purpose: str, questions: List[str]) -> None:
        if self.tracker is not None:
            self.tracker.context(purpose=purpose, questions=questions)

    def ask(self, question: str, asked_by: str, date: str, t: Optional[int] = None,
            situation: str = "") -> Dict[str, Any]:
        self.context = {"asked_by": asked_by, "date": date, "t": t, "situation": situation}
        cached = self._cached(question, asked_by, t)
        if cached:
            return cached
        hit = self._exact(question)
        if hit:
            return self._answer_from_node(question, hit[0], hit[1], asked_by, t)
        if self.agent.llm is None and self.memory.reasoning:
            # no model to judge similar memories (heuristic mode): only word-for-word repeats can be answered,
            # so the question is banked as a gap without the embedding search
            self._bank_entry({"question": question, "tag": "new gap", "matched_node": None, "note": "no model to judge similar memories"})
            rec = {"question": question, "answer": NO_EVIDENCE, "known": False, "asked_by": asked_by, "t": t}
            self.asked.append(rec)
            return rec
        self._label("memory ask", [question])
        try:
            if not self.memory.reasoning:      # empty memory: nothing to match, no model needed
                answer = self.agent.ask(question)
            elif not self.budget_ok():
                answer = NO_EVIDENCE
                self._bank_entry({"question": question, "tag": "budget", "matched_node": None})
            else:
                answer = self.agent.ask(question)
        except Exception as exc:  # the door never takes the planner down
            answer = NO_EVIDENCE
            self._bank_entry({"question": question, "tag": "new gap", "matched_node": None,
                              "error": repr(exc)[:200]})
        known = answer.strip().lower() != NO_EVIDENCE
        rec = {"question": question, "answer": answer, "known": known, "asked_by": asked_by, "t": t}
        self.asked.append(rec)
        return self._remember(rec)

    def ask_all(self, questions: List[str], asked_by: str, date: str, t: Optional[int] = None,
                situation: str = "") -> List[Dict[str, Any]]:
        """Several questions at once: one similarity search each (local), then ONE model call to
        judge all candidates and ONE to write all the answers, instead of two calls per question."""
        questions = [q for q in questions if q]
        self.context = {"asked_by": asked_by, "date": date, "t": t, "situation": situation}
        # word-for-word repeats are answered here; only the rest travel to the model
        results: Dict[int, Dict[str, Any]] = {}
        rest: List[tuple] = []
        for i, q in enumerate(questions):
            cached = self._cached(q, asked_by, t)
            hit = None if cached else self._exact(q)
            if cached:
                results[i] = cached
            elif hit:
                results[i] = self._answer_from_node(q, hit[0], hit[1], asked_by, t)
            else:
                rest.append((i, q))
        rest_qs = [q for _, q in rest]
        if len(rest_qs) <= 1 or not self.memory.reasoning or not self.budget_ok() or self.agent.llm is None:
            answered = [self.ask(q, asked_by, date, t, situation) for q in rest_qs]
        else:
            try:
                answered = self._ask_batch(rest_qs, asked_by, t)
            except Exception as exc:  # never take the planner down; fall back to one at a time
                self.asked.append({"batch_error": repr(exc)[:200]})
                answered = [self.ask(q, asked_by, date, t, situation) for q in rest_qs]
        for (i, _), rec in zip(rest, answered):
            results[i] = self._remember(rec)
        return [results[i] for i in range(len(questions))]

    def _ask_batch(self, questions: List[str], asked_by: str, t: Optional[int]) -> List[Dict[str, Any]]:
        nodes = {nid: n for nid, n in self.memory.reasoning.items() if isinstance(n, EmpiricalNode)}
        candidates = [{"node_id": nid, "question": n.question} for nid, n in nodes.items()]
        per_q = [top_candidates(q, candidates, k=5) for q in questions]
        items = []
        for i, (q, cands) in enumerate(zip(questions, per_q)):
            items.append(f"Question {i}: \"{q}\"")
            for c in cands:
                items.append(f"  - node {c['node_id']}: \"{c['question']}\"")
        prompt = (_DIR / "memory_door_match_batch.md").read_text(encoding="utf-8").format(items="\n".join(items))
        self._label("memory match", questions)
        matched: _MatchAll = self.agent.llm.with_structured_output(_MatchAll).invoke(prompt)
        verdicts: Dict[int, List[_Verdict]] = {}
        for v in matched.verdicts:
            verdicts.setdefault(v.question_index, []).append(v)

        results: Dict[int, Dict[str, Any]] = {}
        to_answer: List[tuple] = []
        for i, q in enumerate(questions):
            vs = verdicts.get(i, [])
            same_conclusive = next((v for v in vs if v.verdict == "same" and v.node_id in nodes and nodes[v.node_id].conclusion is not None), None)
            opposite_true = next((v for v in vs if v.verdict == "opposite" and v.node_id in nodes and nodes[v.node_id].conclusion is True), None)
            same_open = next((v for v in vs if v.verdict == "same" and v.node_id in nodes and nodes[v.node_id].conclusion is None), None)
            if same_conclusive:
                n = nodes[same_conclusive.node_id]
                to_answer.append((i, q, {"question": n.question, "prediction": n.prediction, "conclusion": n.conclusion,
                                         "evidence": self._evidence(n)}))
            elif opposite_true:
                n = nodes[opposite_true.node_id]
                to_answer.append((i, q, {"question": n.question, "prediction": n.prediction, "conclusion": False,
                                         "evidence": self._evidence(n),
                                         "note": "the asked question is the logical opposite of a claim tested True, so it is False"}))
            elif same_open:
                # matched a claim that is tested but not settled: Lionel's memory agent walks the
                # hypothesis subgraph (its level 2) and logs the urgency bump itself; rare
                self._label("memory chain walk", [q])
                answer = self.agent.ask(q)
                results[i] = {"question": q, "answer": answer, "known": answer.strip().lower() != NO_EVIDENCE}
            else:
                self._bank_entry({"question": q, "tag": "new gap", "matched_node": None})
                results[i] = {"question": q, "answer": NO_EVIDENCE, "known": False}

        if to_answer:
            items = [f"Question {i}: \"{q}\"\n  memory holds: {json.dumps(info)}" for i, q, info in to_answer]
            prompt = (_DIR / "memory_door_answer_batch.md").read_text(encoding="utf-8").format(items="\n".join(items))
            self._label("memory answer", [q for _, q, _ in to_answer])
            answered: _AnswerAll = self.agent.llm.with_structured_output(_AnswerAll).invoke(prompt)
            got = {a.question_index: a.answer for a in answered.answers}
            for i, q, _ in to_answer:
                results[i] = {"question": q, "answer": got.get(i, NO_EVIDENCE), "known": i in got}
        out = []
        for i, q in enumerate(questions):
            rec = {**results[i], "asked_by": asked_by, "t": t}
            self.asked.append(rec)
            out.append(rec)
        return out  # ask_all remembers these
