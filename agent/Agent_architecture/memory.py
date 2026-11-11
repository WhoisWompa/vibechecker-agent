"""Data structures for reasoning and hypothesis memory."""

import json
import os
from dataclasses import dataclass, field, asdict


def venue_reference() -> str:
    """The shared venue reference text, injected into prompts."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "venue_reference.md")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


@dataclass
class ExperimentRecord:
    """One test run on an empirical node; a retest overwrites it."""
    assumptions: dict[str, bool | None] = field(default_factory=dict)  # node_id -> verdict assumed at test time
    assumptions_notes: str = ""     # optional prose about the assumptions
    method: str = ""                # test approach (simulation / A-B / before-after / ablation / human-in-the-loop / pilot) and why
    setup: str = ""                 # environment, tools, parameters, initial conditions
    procedure: str = ""             # step-by-step actions taken
    metrics: str = ""               # what's measured, how, how often
    success_criteria: str = ""      # what result counts as confirming vs rejecting
    results: str = ""                # raw observations / data
    statistics: dict | None = None  # computed by the experiment query: effect, se, ci_low, ci_high, p_value, n_per_side (side label -> occurrence count)
    conclusion: bool | None = None  # was the prediction supported
    limitations: str = ""           # caveats that could weaken the result


@dataclass
class FactNode:
    """An outside-world fact, settled by internet lookup, never by experiment."""
    id: str          # "r1", "r2", ... (shares the id space with the other node kinds)
    question: str    # the lookup question
    answer: str = "" # empty until looked up
    sources: str = "" # where the answer came from; empty until looked up


@dataclass
class AssumptionNode:
    """Standalone common knowledge the reasoning leans on; neither looked up
    nor tested. Fed back into later research passes as an open question."""
    id: str          # "r1", "r2", ... (shares the id space with the other node kinds)
    statement: str   # the common-knowledge claim, in words


@dataclass
class EmpiricalNode:
    """An experiment-based question, answered by testing against data."""
    id: str                  # "r1", "r2", ...
    question: str            # what we're trying to find out
    prediction: str          # the predicted answer
    verified: bool = False   # False = never tested yet
    conclusion: bool | None = None  # live verdict: True / False / None (tested but inconclusive)
    experiment: "ExperimentRecord | None" = None  # the current test write-up; a retest overwrites it


@dataclass
class InferenceNode:
    """A logic link between steps; its truth is judged by the supervisor's LLM, never tested."""
    id: str                  # "r1", "r2", ... (shares the id space with EmpiricalNode)
    statement: str           # the logic link, in words
    verdicts: dict[str, bool | None] = field(default_factory=dict)
    # what this argument implies, per downstream target (child node id, or a hypothesis id
    # for a terminal); written by the supervisor's LLM, wiped stale when upstream changes


@dataclass
class HypothesisNode:
    id: str                        # "h1", "h2", ...
    statement: str                 # the claim, short and condensed
    nodes: list[str] = field(default_factory=list)
    # terminal reasoning nodes plugged directly into this hypothesis;
    # each is one independent route of argument
    upstream: dict[str, list[str]] = field(default_factory=dict)
    # this hypothesis's own wiring: node_id -> parent node ids, valid only here.
    # empirical nodes are shared facts; how they are wired is local to each hypothesis.
    conclusion: bool | None = None # all terminals True -> True; any False -> False;
                                   # else None (False wins over None)


class ExperienceMemory:
    """A shared pool of reasoning nodes; each hypothesis wires its own DAG over them."""

    def __init__(self):
        self.reasoning: dict[str, FactNode | EmpiricalNode | InferenceNode] = {}
        self.hypotheses: dict[str, HypothesisNode] = {}
        self._r_count = 0
        self._h_count = 0

    # -- creation --

    def add_empirical(self, question, prediction):
        self._r_count += 1
        node_id = f"r{self._r_count}"
        self.reasoning[node_id] = EmpiricalNode(
            id=node_id,
            question=question,
            prediction=prediction,
        )
        return node_id

    def add_fact(self, question):
        self._r_count += 1
        node_id = f"r{self._r_count}"
        self.reasoning[node_id] = FactNode(id=node_id, question=question)
        return node_id

    def add_assumption(self, statement):
        self._r_count += 1
        node_id = f"r{self._r_count}"
        self.reasoning[node_id] = AssumptionNode(id=node_id, statement=statement)
        return node_id

    def add_inference(self, statement):
        self._r_count += 1
        node_id = f"r{self._r_count}"
        self.reasoning[node_id] = InferenceNode(id=node_id, statement=statement)
        return node_id

    def add_hypothesis(self, statement):
        self._h_count += 1
        hypothesis_id = f"h{self._h_count}"
        self.hypotheses[hypothesis_id] = HypothesisNode(id=hypothesis_id, statement=statement)
        return hypothesis_id

    # -- wiring (always inside one hypothesis's subgraph) --

    def connect(self, hypothesis_id, from_id, to_id):
        """Add the edge from_id -> to_id inside one hypothesis's subgraph."""
        hyp = self.hypotheses[hypothesis_id]
        if from_id not in self.reasoning:
            raise KeyError(from_id)
        if to_id not in self.reasoning:
            raise KeyError(to_id)
        if from_id == to_id:
            raise ValueError("a node cannot feed itself")
        if isinstance(self.reasoning[to_id], (FactNode, AssumptionNode)):
            raise ValueError("fact and assumption nodes take no parents")
        parents = hyp.upstream.setdefault(to_id, [])
        if from_id in parents:
            return  # edge already there
        if to_id in self._ancestors(hyp, from_id):
            raise ValueError(f"edge {from_id}->{to_id} would create a cycle in {hypothesis_id}")
        parents.append(from_id)
        target = self.reasoning[to_id]
        if isinstance(target, InferenceNode):
            # a new premise makes the old judgment stale
            target.verdicts = {}
            self._invalidate(hyp, to_id)

    def connect_to_hypothesis(self, node_id, hypothesis_id):
        if node_id not in self.reasoning:
            raise KeyError(node_id)
        hyp = self.hypotheses[hypothesis_id]
        if node_id not in hyp.nodes:
            hyp.nodes.append(node_id)

    def _ancestors(self, hyp, node_id):
        """All nodes reachable walking upstream from node_id inside hyp's subgraph."""
        seen = set()
        stack = list(hyp.upstream.get(node_id, []))
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(hyp.upstream.get(current, []))
        return seen

    # -- stale-wiping (push-based) --

    def _invalidate(self, hyp, node_id):
        """Wipe verdicts on downstream inference nodes inside hyp's subgraph — they must
        be re-judged by the supervisor's LLM. Stops at empirical children; their truth
        is their own test only."""
        children = [n for n, parents in hyp.upstream.items() if node_id in parents]
        for child_id in children:
            child = self.reasoning[child_id]
            if isinstance(child, InferenceNode):
                child.verdicts = {}
                self._invalidate(hyp, child_id)

    # -- testing --

    def record_result(self, node_id, conclusion, experiment):
        """The only way test outcomes get written onto an empirical node.
        The new result ripples through every hypothesis whose wiring uses this node."""
        node = self.reasoning[node_id]
        if not isinstance(node, EmpiricalNode):
            raise ValueError("only empirical nodes are tested")
        if experiment.conclusion is None:
            experiment.conclusion = conclusion
        elif experiment.conclusion != conclusion:
            raise ValueError("write-up conclusion contradicts the recorded verdict")
        node.experiment = experiment
        node.verified = True
        node.conclusion = conclusion
        for hyp in self.hypotheses.values():
            if any(node_id in parents for parents in hyp.upstream.values()):
                self._invalidate(hyp, node_id)

    def record_inference(self, node_id, verdicts):
        """The only way inference judgments get written. verdicts maps each downstream
        target (child node id, or a hypothesis id for a terminal) to True/False/None."""
        node = self.reasoning[node_id]
        if not isinstance(node, InferenceNode):
            raise ValueError("only inference nodes are judged")
        node.verdicts = dict(verdicts)
        for hyp in self.hypotheses.values():
            if any(node_id in parents for parents in hyp.upstream.values()):
                self._invalidate(hyp, node_id)

    def record_fact(self, node_id, answer, sources):
        """The only way lookup answers get written onto a fact node."""
        node = self.reasoning[node_id]
        if not isinstance(node, FactNode):
            raise ValueError("only fact nodes take lookup answers")
        node.answer = answer
        node.sources = sources
        for hyp in self.hypotheses.values():
            if any(node_id in parents for parents in hyp.upstream.values()):
                self._invalidate(hyp, node_id)

    def retest_candidates(self, changed_id):
        """Empirical nodes needing a retest after changed_id's verdict changed (one wave only).

        Two triggers, both read purely from write-ups (assumption dicts are pre-filled by
        the supervisor from the node's parents at dispatch time; a None value means
        "ran under do-not-assume-changed_id"):
        - broken premise: the node's write-up assumed a verdict for changed_id that
          no longer matches changed_id's current conclusion;
        - unlock: the node's write-up ran under do-not-assume-changed_id, changed_id is
          now conclusive, and the node itself is still inconclusive.
        Running the retests is the supervisor's job; this only names the wave.
        """
        changed = self.reasoning[changed_id]
        candidates = []
        for node_id, node in self.reasoning.items():
            if node_id == changed_id:
                continue
            if not isinstance(node, EmpiricalNode) or node.experiment is None:
                continue
            assumptions = node.experiment.assumptions
            broken_premise = (
                changed_id in assumptions
                and assumptions[changed_id] is not None
                and assumptions[changed_id] != changed.conclusion
            )
            unlock = (
                changed_id in assumptions
                and assumptions[changed_id] is None
                and changed.conclusion is not None
                and node.conclusion is None
            )
            if broken_premise or unlock:
                candidates.append(node_id)
        return candidates

    # -- verdict --

    def check(self, hypothesis_id):
        """Read the hypothesis verdict off its terminal nodes.
        An empirical terminal contributes its conclusion; an inference terminal
        contributes verdicts.get(hypothesis_id) (absent = None, not yet judged for
        this hypothesis).
        All True -> True; any False -> False (False beats inconclusive); else None."""
        hyp = self.hypotheses[hypothesis_id]
        verdicts = []
        for n in hyp.nodes:
            node = self.reasoning[n]
            if isinstance(node, EmpiricalNode):
                verdicts.append(node.conclusion)
            elif isinstance(node, (FactNode, AssumptionNode)):
                verdicts.append(None)  # a bare fact or assumption argues nothing by itself
            else:
                verdicts.append(node.verdicts.get(hypothesis_id))
        if any(v is False for v in verdicts):
            hyp.conclusion = False
        elif verdicts and all(v is True for v in verdicts):
            hyp.conclusion = True
        else:
            hyp.conclusion = None
        return hyp.conclusion

    # -- cycle check across the union of all hypotheses --

    def find_cycles(self):
        """Nodes caught in a cross-hypothesis wiring loop. Each hypothesis alone is
        acyclic; the union may not be. The supervisor runs this after wiring, before
        testing, and debugs any loop returned."""
        union_parents: dict[str, set[str]] = {}
        for hyp in self.hypotheses.values():
            for node_id, parents in hyp.upstream.items():
                union_parents.setdefault(node_id, set()).update(parents)
                for p in parents:
                    union_parents.setdefault(p, set())

        remaining = dict(union_parents)
        changed = True
        while changed:
            changed = False
            resolved = {n for n, parents in remaining.items() if not parents}
            if resolved:
                changed = True
                for n in resolved:
                    del remaining[n]
                for parents in remaining.values():
                    parents.difference_update(resolved)
        return sorted(remaining.keys())

    # -- persistence --

    def save(self, path):
        data = {
            "reasoning": {},
            "hypotheses": {k: asdict(v) for k, v in self.hypotheses.items()},
            "r_count": self._r_count,
            "h_count": self._h_count,
        }
        for node_id, node in self.reasoning.items():
            record = asdict(node)
            if isinstance(node, FactNode):
                record["kind"] = "fact"
            elif isinstance(node, AssumptionNode):
                record["kind"] = "assumption"
            elif isinstance(node, EmpiricalNode):
                record["kind"] = "empirical"
            else:
                record["kind"] = "inference"
            data["reasoning"][node_id] = record
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp_path, path)

    @classmethod
    def load(cls, path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        mem = cls()
        for node_id, record in data.get("reasoning", {}).items():
            kind = record.pop("kind")
            if kind == "fact":
                mem.reasoning[node_id] = FactNode(**record)
            elif kind == "assumption":
                mem.reasoning[node_id] = AssumptionNode(**record)
            elif kind == "empirical":
                record["experiment"] = ExperimentRecord(**record["experiment"]) if record.get("experiment") else None
                mem.reasoning[node_id] = EmpiricalNode(**record)
            else:
                mem.reasoning[node_id] = InferenceNode(**record)
        mem.hypotheses = {
            k: HypothesisNode(**v) for k, v in data.get("hypotheses", {}).items()
        }
        mem._r_count = data.get("r_count", 0)
        mem._h_count = data.get("h_count", 0)
        return mem


if __name__ == "__main__":
    mem = ExperienceMemory()

    # 1. record_result on empirical nodes still works; connecting parents into an
    # inference leaves its verdicts {} (stale) -- no auto-derivation anymore
    r1 = mem.add_empirical("does the lunch hour raise walk-ins?", "yes, it raises them")
    r2 = mem.add_empirical("does the weekday office crowd raise walk-ins?", "yes")
    mem.record_result(r1, True, ExperimentRecord(method="before-after", results="walk-ins +40% during lunch"))
    mem.record_result(r2, True, ExperimentRecord(method="A-B", results="weekday walk-ins higher than weekend"))
    h1 = mem.add_hypothesis("weekday lunch is the peak")
    r3 = mem.add_inference("both effects together should pack the lunch service")
    mem.connect(h1, r1, r3)
    mem.connect(h1, r2, r3)
    assert mem.reasoning[r3].verdicts == {}

    # 2. record_inference fills verdicts explicitly
    r6 = mem.add_empirical("do bad-weather lunches still peak?", "yes")
    mem.record_inference(r3, {r6: True, h1: True})
    assert mem.reasoning[r3].verdicts == {r6: True, h1: True}

    # 3. check(): inference terminal read via verdicts.get(hyp_id)
    mem.connect_to_hypothesis(r3, h1)
    assert mem.check(h1) is True   # judged True for h1

    h2 = mem.add_hypothesis("promos work best at lunch")
    r4 = mem.add_empirical("do promos raise walk-ins?", "yes")
    r5 = mem.add_inference("promo on top of the lunch rush should compound")
    mem.connect(h2, r1, r5)
    mem.connect(h2, r4, r5)
    mem.connect_to_hypothesis(r5, h2)
    assert mem.check(h2) is None   # r5 unjudged for h2 -> None

    # False beats None via an empirical False terminal
    h3 = mem.add_hypothesis("weekend matches weekday")
    r8 = mem.add_empirical("does weekend lunch match weekday lunch?", "yes, should match")
    mem.connect_to_hypothesis(r8, h3)
    assert mem.check(h3) is None
    mem.record_result(
        r8, False,
        ExperimentRecord(method="A-B", assumptions={r1: True}, results="weekend lunch much quieter"),
    )
    mem.connect_to_hypothesis(r5, h3)  # r5 unjudged for h3 -> None, but r8 is False
    assert mem.check(h3) is False
    # hypothesis with no terminals is inconclusive
    h_empty = mem.add_hypothesis("empty for now")
    assert mem.check(h_empty) is None

    # 4. stale-wipe: upstream empirical retest wipes a downstream judged inference
    assert mem.reasoning[r3].verdicts == {r6: True, h1: True}
    mem.record_result(r1, False, ExperimentRecord(method="ablation", results="effect weaker on re-test"))
    assert mem.reasoning[r3].verdicts == {}   # wiped stale
    assert mem.check(h1) is None              # r3 unjudged again -> None

    # inference->inference chain: the wipe recurses
    mem.record_inference(r3, {r6: True, h1: True})
    r7 = mem.add_inference("so the peak claim should hold")
    mem.connect(h1, r3, r7)
    mem.record_inference(r7, {h1: True})
    mem.record_result(r2, True, ExperimentRecord(method="A-B", results="reconfirmed"))
    assert mem.reasoning[r3].verdicts == {}
    assert mem.reasoning[r7].verdicts == {}   # recursive wipe from r3 -> r7

    # re-judge for the next scenarios
    mem.record_inference(r3, {r6: True, h1: True})
    mem.record_inference(r7, {h1: True})

    # 5. connect() into an already-judged inference wipes it
    r_extra = mem.add_empirical("does an extra factor matter?", "yes")
    mem.record_result(r_extra, True, ExperimentRecord(results="extra factor confirmed"))
    mem.connect(h1, r_extra, r7)
    assert mem.reasoning[r7].verdicts == {}

    # 6. record_result mismatch guard
    try:
        mem.record_result(r1, True, ExperimentRecord(conclusion=False, results="contradiction"))
        assert False, "expected ValueError"
    except ValueError:
        pass

    # 7. record_inference on empirical raises; record_result on inference raises
    try:
        mem.record_inference(r1, {h1: True})
        assert False, "expected ValueError"
    except ValueError:
        pass
    try:
        mem.record_result(r3, True, ExperimentRecord())
        assert False, "expected ValueError"
    except ValueError:
        pass

    # cycle rejected within a hypothesis (still enforced by connect)
    try:
        mem.connect(h1, r7, r1)
        assert False, "expected ValueError"
    except ValueError:
        pass

    # 8. retest_candidates
    h10 = mem.add_hypothesis("retest wave scenario")
    rA = mem.add_empirical("does A hold?", "yes")
    rB = mem.add_empirical("does B hold given A?", "yes")
    mem.connect(h10, rA, rB)
    mem.record_result(rA, True, ExperimentRecord(results="A confirmed"))
    mem.record_result(rB, True, ExperimentRecord(assumptions={rA: True}, results="B confirmed assuming A"))
    # 8a. broken premise: rA flips False -> flagged
    mem.record_result(rA, False, ExperimentRecord(results="A retested, no longer holds"))
    assert mem.retest_candidates(rA) == [rB]

    # 8b. unrelated node with empty assumptions -> not flagged
    rC = mem.add_empirical("does C hold, unrelated to A?", "yes")
    mem.record_result(rC, True, ExperimentRecord(results="C confirmed, unrelated"))
    assert rC not in mem.retest_candidates(rA)

    # 8c. don't-assume unlock: write-up {rA: None}, node inconclusive, rA now conclusive
    rD = mem.add_empirical("does D hold given A?", "unclear")
    mem.connect(h10, rA, rD)
    mem.record_result(rD, None, ExperimentRecord(assumptions={rA: None}, results="ran under do-not-assume-A, inconclusive"))
    mem.record_result(rA, True, ExperimentRecord(results="A reconfirmed"))
    assert rD in mem.retest_candidates(rA)

    # 8d. conclusive under don't-assume -> NOT flagged when rA resolves
    rG = mem.add_empirical("does G hold given A?", "yes, regardless")
    mem.connect(h10, rA, rG)
    mem.record_result(rG, True, ExperimentRecord(assumptions={rA: None}, results="ran under do-not-assume-A, concluded True regardless"))
    assert rG not in mem.retest_candidates(rA)

    # 8e. one wave only: rE assumes about rB, untouched by rA's wave
    rE = mem.add_empirical("does E hold given B?", "yes")
    mem.connect(h10, rB, rE)
    mem.record_result(rE, True, ExperimentRecord(assumptions={rB: True}, results="E confirmed assuming B"))
    assert rE not in mem.retest_candidates(rA)

    # 9. find_cycles: [] on the existing acyclic setup
    assert mem.find_cycles() == []

    # wire A->B inside one hypothesis and B->A inside another -> both ids returned
    h_cyc1 = mem.add_hypothesis("cycle side 1")
    h_cyc2 = mem.add_hypothesis("cycle side 2")
    cA = mem.add_empirical("cycle node A?", "n/a")
    cB = mem.add_empirical("cycle node B?", "n/a")
    mem.connect(h_cyc1, cA, cB)
    mem.connect(h_cyc2, cB, cA)
    cycles = mem.find_cycles()
    assert cA in cycles and cB in cycles

    # per-hypothesis cycle rejection in connect still works
    try:
        mem.connect(h_cyc1, cB, cA)
        assert False, "expected ValueError"
    except ValueError:
        pass

    # 10. save/load round-trip
    import tempfile
    mem_path = os.path.join(tempfile.mkdtemp(), "memory.json")
    r_stats = mem.add_empirical("does rain change turnout?", "yes, rain lowers turnout")
    mem.record_result(
        r_stats, True,
        ExperimentRecord(
            method="A-B",
            results="turnout lower on rainy days",
            statistics={"effect": 35.0, "se": 10.0, "ci_low": 15.0, "ci_high": 55.0, "p_value": 0.01, "n_per_side": {"rainy": 6, "dry": 22}},
        ),
    )
    mem.save(mem_path)
    reloaded = ExperienceMemory.load(mem_path)
    assert reloaded.reasoning[r_stats].experiment.statistics == mem.reasoning[r_stats].experiment.statistics
    assert set(reloaded.reasoning.keys()) == set(mem.reasoning.keys())
    assert isinstance(reloaded.reasoning[r1], EmpiricalNode)
    assert isinstance(reloaded.reasoning[r3], InferenceNode)
    assert reloaded.reasoning[r3].statement == mem.reasoning[r3].statement
    assert reloaded.reasoning[r3].verdicts == mem.reasoning[r3].verdicts
    assert reloaded.reasoning[r1].conclusion is False
    assert isinstance(reloaded.reasoning[r1].experiment, ExperimentRecord)
    assert reloaded.reasoning[r1].experiment.method == "ablation"
    assert reloaded.hypotheses[h1].upstream == mem.hypotheses[h1].upstream
    r_new = reloaded.add_empirical("does x raise y?", "yes")
    assert r_new not in mem.reasoning  # id counter survived the round-trip
    h_new = reloaded.add_hypothesis("new hyp")
    assert h_new not in mem.hypotheses  # h counter survived the round-trip

    # 11. FactNode: an outside-world fact settled by internet lookup, never by experiment
    r_before_fact = mem._r_count
    rF = mem.add_fact("what is the venue's posted lunch opening time?")
    assert isinstance(mem.reasoning[rF], FactNode)
    assert mem._r_count == r_before_fact + 1  # shares the r-id counter

    # record_fact fills answer/sources and stale-wipes a downstream judged inference
    h_fact = mem.add_hypothesis("posted hours explain the lunch rush")
    r_fact_inf = mem.add_inference("if opening is at 11am, the lunch rush follows naturally")
    mem.connect(h_fact, rF, r_fact_inf)
    mem.record_inference(r_fact_inf, {h_fact: True})
    assert mem.reasoning[r_fact_inf].verdicts == {h_fact: True}
    mem.record_fact(rF, "11:00 AM", "venue website")
    assert mem.reasoning[rF].answer == "11:00 AM"
    assert mem.reasoning[rF].sources == "venue website"
    assert mem.reasoning[r_fact_inf].verdicts == {}  # stale-wiped

    # record_result on a fact raises
    try:
        mem.record_result(rF, True, ExperimentRecord())
        assert False, "expected ValueError"
    except ValueError:
        pass

    # record_inference on a fact raises
    try:
        mem.record_inference(rF, {h_fact: True})
        assert False, "expected ValueError"
    except ValueError:
        pass

    # connect() with a fact as the target raises
    try:
        mem.connect(h_fact, r_fact_inf, rF)
        assert False, "expected ValueError"
    except ValueError:
        pass

    # a fact wired directly as a hypothesis terminal makes check() read None
    h_fact2 = mem.add_hypothesis("bare fact terminal")
    rF2 = mem.add_fact("does the venue open on public holidays?")
    mem.connect_to_hypothesis(rF2, h_fact2)
    assert mem.check(h_fact2) is None

    # save/load round-trip: FactNode survives with question/answer/sources intact
    mem.save(mem_path)
    reloaded = ExperienceMemory.load(mem_path)
    assert isinstance(reloaded.reasoning[rF], FactNode)
    assert reloaded.reasoning[rF].question == mem.reasoning[rF].question
    assert reloaded.reasoning[rF].answer == mem.reasoning[rF].answer
    assert reloaded.reasoning[rF].sources == mem.reasoning[rF].sources

    # 12. AssumptionNode: standalone common knowledge, no parents, never settled
    rAs = mem.add_assumption("people generally have more free time on weekends")
    assert isinstance(mem.reasoning[rAs], AssumptionNode)
    h_as = mem.add_hypothesis("assumption wiring")
    r_as_inf = mem.add_inference("so weekends should be busier")
    mem.connect(h_as, rAs, r_as_inf)
    # an assumption can never be a target
    try:
        mem.connect(h_as, r_as_inf, rAs)
        assert False, "expected ValueError"
    except ValueError:
        pass
    # record_* on an assumption raises
    try:
        mem.record_result(rAs, True, ExperimentRecord())
        assert False, "expected ValueError"
    except ValueError:
        pass
    try:
        mem.record_fact(rAs, "answer", "source")
        assert False, "expected ValueError"
    except ValueError:
        pass
    # a bare assumption terminal reads None
    mem.connect_to_hypothesis(rAs, h_as)
    assert mem.check(h_as) is None
    # save/load round-trip
    mem.save(mem_path)
    reloaded = ExperienceMemory.load(mem_path)
    assert isinstance(reloaded.reasoning[rAs], AssumptionNode)
    assert reloaded.reasoning[rAs].statement == mem.reasoning[rAs].statement

    print("ok")
