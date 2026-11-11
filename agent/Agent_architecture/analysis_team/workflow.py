"""Orchestration for one research pass.

Per-node dispatch (data gate -> assumptions -> design -> run -> interpret ->
record) is a LangGraph state machine (decisions rule 47). The supervisor loop
that builds each hypothesis's graph, and the dependency scheduler that decides
which node runs next, are plain Python (decisions rule 23).

Read agent/experience_memory_decisions.md rules 15-23, 29, 44-47 before
touching this file.
"""

import json
import sys
import traceback
from pathlib import Path
from typing import Optional, TypedDict

# agent/ (where memory.py and llm.py live)
_ANALYSIS_TEAM_DIR = Path(__file__).resolve().parent
_AGENT_DIR = str(_ANALYSIS_TEAM_DIR.parent)
if _AGENT_DIR not in sys.path:
    sys.path.insert(0, _AGENT_DIR)

from memory import (  # noqa: E402
    AssumptionNode,
    EmpiricalNode,
    ExperienceMemory,
    ExperimentRecord,
    FactNode,
    InferenceNode,
)
from llm import get_llm  # noqa: E402
import run_log  # noqa: E402

# Each agent folder is not a package (no __init__.py) -- add every folder we
# need a module from, then import that module by filename, the same shim
# pattern the agent classes themselves use for memory.py/llm.py.
for _sub in (
    "hypothesis_agent",
    "supervisor_agent",
    "researcher_agent",
    "websearch_agent",
    "data_handler_agent",
):
    _p = str(_ANALYSIS_TEAM_DIR / _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

from hypothesis_agent import HypothesisAgent  # noqa: E402
from supervisor_agent import SupervisorAgent  # noqa: E402
from researcher_agent import (  # noqa: E402
    ResearcherAgent,
    record_prose,
    ExperimentForm,
    AssumptionSynthesis,
    Interpretation,
)
from websearch_agent import WebsearchAgent  # noqa: E402
from data_handler_agent import DataHandlerAgent, GroupPlan  # noqa: E402
import pipeline  # noqa: E402
import sim_search  # noqa: E402

from langgraph.graph import END, StateGraph  # noqa: E402


# ---------------------------------------------------------------------------
# Agent construction
# ---------------------------------------------------------------------------

def build_agents(db_path: str) -> dict:
    """Construct all agents from llm.py factories and return them in a dict."""
    return {
        "hypothesis": HypothesisAgent(llm=get_llm("hypothesis")),
        "supervisor": SupervisorAgent(llm=get_llm("supervisor")),
        "researcher": ResearcherAgent(llm=get_llm("researcher")),
        "websearch": WebsearchAgent(llm=get_llm()),
        "data_handler": DataHandlerAgent(llm=get_llm("researcher"), db_path=db_path),
    }


# ---------------------------------------------------------------------------
# Shared gate logic (used both inside the dispatch graph and by the
# supervisor loop's design-time check, so the two never drift apart).
# ---------------------------------------------------------------------------

def _gate(agents: dict, question: str) -> dict:
    """Returns the data handler's gate verdict: {"plan", "testable", "reason"}."""
    return agents["data_handler"].check_data(question)


def _parent_dict(node) -> dict:
    """Plain-dict view of one parent node for the researcher's assumption
    synthesis prompt. Empirical parents carry their verdict and measured
    range (rule 3); fact parents carry their answer; inference parents carry
    only their statement (rule 31)."""
    if isinstance(node, EmpiricalNode):
        range_text = ""
        if node.experiment and node.experiment.statistics:
            stats = node.experiment.statistics
            if "ci_low" in stats and "ci_high" in stats:
                range_text = f"{stats['ci_low']:.3g} to {stats['ci_high']:.3g}"
        return {
            "parent_id": node.id,
            "kind": "empirical",
            "question": node.question,
            "prediction": node.prediction,
            "verdict": node.conclusion,
            "range": range_text,
        }
    if isinstance(node, FactNode):
        return {"parent_id": node.id, "kind": "fact", "question": node.question, "answer": node.answer}
    if isinstance(node, AssumptionNode):
        return {"parent_id": node.id, "kind": "assumption", "statement": node.statement}
    return {"parent_id": node.id, "kind": "inference", "statement": node.statement}


def _collect_parents(memory: ExperienceMemory, node_id: str) -> list:
    """Every parent node wired to node_id, across every hypothesis that wires
    it (a node's parents are hypothesis-local, but the set of parent node
    objects itself is shared), de-duplicated, in stable order."""
    parents = []
    seen = set()
    for hyp in memory.hypotheses.values():
        for parent_id in hyp.upstream.get(node_id, []):
            if parent_id not in seen and parent_id in memory.reasoning:
                seen.add(parent_id)
                parents.append(memory.reasoning[parent_id])
    return parents


# ---------------------------------------------------------------------------
# Per-node dispatch graph (LangGraph)
# ---------------------------------------------------------------------------

class NodeState(TypedDict):
    node_id: str
    question: str
    prediction: str
    parents: list
    plan: Optional[GroupPlan]
    data_available: Optional[bool]
    feedback: Optional[str]
    assumptions_dict: Optional[dict]
    synthesis: Optional[AssumptionSynthesis]
    form: Optional[ExperimentForm]
    statistics: Optional[dict]
    verdict: Optional[bool]
    values: Optional[dict]
    interpretation: Optional[Interpretation]


def build_dispatch_graph(agents: dict, memory: ExperienceMemory):
    data_handler = agents["data_handler"]
    supervisor = agents["supervisor"]
    researcher = agents["researcher"]

    def gate(state: NodeState) -> dict:
        result = _gate(agents, state["question"])
        if result["testable"]:
            run_log.event("note", "gate: pass")
        else:
            run_log.event("note", f"gate: fail — {result['reason']}")
        update = {"plan": result["plan"], "data_available": result["testable"]}
        if not result["testable"]:
            update["feedback"] = result["reason"]
        return update

    def gate_router(state: NodeState) -> str:
        return "synthesize" if state["data_available"] else END

    def synthesize(state: NodeState) -> dict:
        parents = state.get("parents", [])
        empirical_parents = [p for p in parents if isinstance(p, EmpiricalNode)]
        assumptions_dict = supervisor.stamp_assumptions(empirical_parents)
        synthesis = researcher.synthesize_assumptions([_parent_dict(p) for p in parents])
        return {"assumptions_dict": assumptions_dict, "synthesis": synthesis}

    def design(state: NodeState) -> dict:
        plan, synthesis = state["plan"], state["synthesis"]
        assert plan is not None and synthesis is not None
        form = researcher.design_experiment(
            state["question"], state["prediction"], plan, synthesis
        )
        run_log.event("note", "design: experiment form ready")
        return {"form": form}

    def run(state: NodeState) -> dict:
        plan, form = state["plan"], state["form"]
        assert plan is not None and form is not None
        result = pipeline.run_experiment(data_handler, plan, form)
        run_log.event("note", f"run: verdict={result['verdict']}")
        return {
            "statistics": result["statistics"],
            "verdict": result["verdict"],
            "values": result["values"],
        }

    def interpret(state: NodeState) -> dict:
        plan, form, statistics = state["plan"], state["form"], state["statistics"]
        assert plan is not None and form is not None and statistics is not None
        interpretation = researcher.interpret(
            state["question"],
            state["prediction"],
            plan,
            form,
            statistics,
            state["verdict"],
        )
        run_log.event("note", "interpret: done")
        return {"interpretation": interpretation}

    def record(state: NodeState) -> dict:
        plan, form, interpretation = state["plan"], state["form"], state["interpretation"]
        assumptions_dict, synthesis = state["assumptions_dict"], state["synthesis"]
        assert plan is not None and form is not None and interpretation is not None
        assert assumptions_dict is not None and synthesis is not None
        prose = record_prose(plan, form)
        experiment = ExperimentRecord(
            assumptions=assumptions_dict,
            assumptions_notes=str(synthesis),
            method=prose["method"],
            setup=prose["setup"],
            procedure=prose["procedure"],
            metrics=prose["metrics"],
            success_criteria=prose["success_criteria"],
            results=str(state["values"]),
            statistics=state["statistics"],
            conclusion=state["verdict"],
            limitations=interpretation.limitations,
        )
        memory.record_result(state["node_id"], state["verdict"], experiment)
        run_log.event("note", f"record: verdict={state['verdict']}")
        run_log.event("result", json.dumps({
            "verdict": state["verdict"],
            "conclusion": (interpretation.reason or "")[:300],
        }))
        run_log.event("memupdate", json.dumps({
            "node": state["node_id"], "kind": "empirical",
            "outcome": {True: "supported", False: "not supported"}.get(state["verdict"], "inconclusive"),
            "detail": (interpretation.reason or "")[:200],
        }))
        return {}

    graph = StateGraph(NodeState)
    graph.add_node("gate", gate)
    graph.add_node("synthesize", synthesize)
    graph.add_node("design", design)
    graph.add_node("run", run)
    graph.add_node("interpret", interpret)
    graph.add_node("record", record)

    graph.set_entry_point("gate")
    graph.add_conditional_edges("gate", gate_router, {"synthesize": "synthesize", END: END})
    graph.add_edge("synthesize", "design")
    graph.add_edge("design", "run")
    graph.add_edge("run", "interpret")
    graph.add_edge("interpret", "record")
    graph.add_edge("record", END)

    return graph.compile()


# ---------------------------------------------------------------------------
# Supervisor loop: builds and wires one hypothesis's graph
# ---------------------------------------------------------------------------

MAX_BOUNCE = 5
MAX_STRUCT_BOUNCE = 3
HYPOTHESIS_CAP = 5  # code owns the cutoff; the LLM only orders the batch


def validate_structure(steps: dict, kinds: dict) -> list[dict]:
    """LLM-free structural check of a classified reasoning graph.

    steps: step_id -> Step (statement/parents); kinds: step_id -> kind.
    Returns a list of {"step_id", "feedback"} violations, empty when sound."""
    violations = []
    children: dict = {sid: [] for sid in steps}
    for sid, step in steps.items():
        for p in step.parents:
            if p not in steps:
                violations.append({
                    "step_id": sid,
                    "feedback": f"parent {p} does not exist in the graph",
                })
            else:
                children[p].append(sid)

    hyp_ids = [sid for sid in steps if kinds.get(sid) == "hypothesis"]
    if not hyp_ids:
        anchor = next((sid for sid in steps if not children[sid]), next(iter(steps), None))
        if anchor is not None:
            violations.append({
                "step_id": anchor,
                "feedback": "no step is the hypothesis itself; the graph must end "
                            "in the hypothesis as its single terminal step",
            })
    else:
        for extra in hyp_ids[1:]:
            violations.append({
                "step_id": extra,
                "feedback": "more than one step is classified as the hypothesis; exactly one is allowed",
            })

    for sid, step in steps.items():
        kind = kinds.get(sid)
        if kind == "fact" and step.parents:
            violations.append({"step_id": sid, "feedback": "a fact step must have no parents"})
        if kind == "assumption" and step.parents:
            violations.append({"step_id": sid, "feedback": "an assumption step must have no parents"})
        if kind == "inference" and not step.parents:
            violations.append({"step_id": sid, "feedback": "an inference step must have at least one parent"})
        if kind == "hypothesis":
            if not step.parents:
                violations.append({"step_id": sid, "feedback": "the hypothesis step must have at least one parent"})
            if children[sid]:
                violations.append({
                    "step_id": sid,
                    "feedback": "the hypothesis step must have no children; nothing may build on it",
                })
        elif not children[sid]:
            violations.append({
                "step_id": sid,
                "feedback": "this step is childless but is not the hypothesis; "
                            "every other step must feed into some later step",
            })
    return violations


def process_hypothesis(hyp_statement: str, agents: dict, memory: ExperienceMemory, plans: dict):
    """Runs the hypothesis agent <-> supervisor loop for one hypothesis
    statement. On survival, wires it into memory and returns
    {"hypothesis_id": ..., "cycles": [...]}. On a spent bounce cap, returns
    the string "discarded" and leaves memory untouched (rule 20: discards
    never enter).

    Discard-vs-dedup: dedup must be able to match a step against a node
    created earlier in this same hypothesis, but rule 20 says nothing from a
    discarded hypothesis may enter memory. Both hold by staging every new
    node locally (a "staged:<step_id>" placeholder id) so dedup can see it,
    and only calling memory.add_fact/add_empirical/add_inference once the
    whole hypothesis has survived the loop. A free settlement on an
    "opposite" dedup match (rule 6) is likewise recorded as a deferred
    instruction and applied right after the real id is minted.
    """
    hypothesis = agents["hypothesis"]
    supervisor = agents["supervisor"]

    hyp_label = hyp_statement[:80]

    bounce = 0
    struct_bounce = 0
    run_log.set_context(hyp_label, "hypothesis agent ↔ supervisor")
    steps = hypothesis.reasoning_steps(hyp_statement)

    # Revision cache, keyed by a step's exact statement text: a revision
    # rewrites only the steps that broke, so a statement that comes back
    # character-for-character identical reuses its previous staging/dedup
    # outcome and gate result without re-rolling the LLM (the db does not
    # change during a pass, so the old outcome still holds).
    # Entry fields, filled as a step travels: "existing_id" (a dedup match)
    # or "staged" (the staged node dict); then "gate"
    # ({"plan", "testable", "reason"}) for staged empirical steps.
    # Kinds are cached separately, keyed by (statement, parents), because a
    # step's classification also depends on its place in the graph.
    cache: dict = {}
    kind_cache: dict = {}

    def _kind_key(step):
        return (step.statement, tuple(step.parents))

    while True:
        ordered_ids = list(steps.keys())

        # -- stamp the hypothesis step deterministically: the unique terminal
        # step (no other step lists it as a parent) IS the hypothesis, so the
        # LLM never classifies it. Zero or multiple terminals: no stamp;
        # validate_structure reports the shape problem as before. --
        parented = {p for sid in ordered_ids for p in steps[sid].parents}
        terminals = [sid for sid in ordered_ids if sid not in parented]
        stamped = {terminals[0]: "hypothesis"} if len(terminals) == 1 else {}

        # -- classify; repair any step that cannot be classified --
        # (batch call, only the steps not seen in a previous iteration)
        run_log.set_context(hyp_label, "hypothesis agent ↔ supervisor")
        uncached_ids = [
            sid for sid in ordered_ids
            if sid not in stamped and _kind_key(steps[sid]) not in kind_cache
        ]
        if uncached_ids:
            classifications = supervisor.classify_steps([
                {
                    "id": sid,
                    "statement": steps[sid].statement,
                    "parents": list(steps[sid].parents),
                }
                for sid in uncached_ids
            ])
            for sid, c in zip(uncached_ids, classifications):
                kind_cache[_kind_key(steps[sid])] = c["kind"]
        kinds = {
            sid: stamped.get(sid, kind_cache.get(_kind_key(steps[sid])))
            for sid in ordered_ids
        }
        run_log.event("dag", json.dumps({
            "nodes": [
                {"id": sid, "statement": steps[sid].statement, "kind": kinds[sid]}
                for sid in ordered_ids
            ],
            "edges": [[p, sid] for sid in ordered_ids for p in steps[sid].parents],
        }))
        unclear = next((sid for sid in ordered_ids if kinds[sid] == "unclear"), None)
        if unclear is not None:
            if bounce >= MAX_BOUNCE:
                run_log.event("note", "discarded")
                return "discarded"
            run_log.event("note", f"revise bounce {bounce + 1}: step {unclear} unclear")
            steps = hypothesis.revise(
                hyp_statement, steps, unclear,
                "step is unclear: cannot tell fact, empirical, or inference apart, "
                "or it blends a real-world fact with a venue claim; break it down further",
            )
            bounce += 1
            continue

        # -- structural check (plain Python, no LLM): nothing downstream runs
        # until the graph's shape agrees with the kinds --
        violations = validate_structure(steps, kinds)
        if violations:
            if struct_bounce >= MAX_STRUCT_BOUNCE:
                run_log.event("note", "discarded")
                return "discarded"
            # all violations go back in one bounce, so one revision can fix
            # every known problem instead of burning a bounce per step
            feedback = "\n".join(f"{v['step_id']}: {v['feedback']}" for v in violations)
            run_log.event(
                "note",
                f"revise bounce (structure) {struct_bounce + 1}:\n{feedback}",
            )
            steps = hypothesis.revise(hyp_statement, steps, violations[0]["step_id"], feedback)
            struct_bounce += 1
            continue

        # -- dedup / stage every step, deferred until this hypothesis survives --
        staged: dict = {}     # local_id -> {"kind", "question"/"statement", "prediction", "derived"}
        resolved: dict = {}   # step_id -> resolved id ("rN" existing, or "staged:<local_id>")

        def fact_pool():
            pool = [
                {"node_id": nid, "question": n.question}
                for nid, n in memory.reasoning.items() if isinstance(n, FactNode)
            ]
            pool += [
                {"node_id": f"staged:{lid}", "question": s["question"]}
                for lid, s in staged.items() if s["kind"] == "fact"
            ]
            return pool

        def empirical_pool():
            pool = [
                {"node_id": nid, "question": n.question, "text": f"{n.question} — {n.prediction}"}
                for nid, n in memory.reasoning.items() if isinstance(n, EmpiricalNode)
            ]
            pool += [
                {"node_id": f"staged:{lid}", "question": s["question"], "text": f"{s['question']} — {s['prediction']}"}
                for lid, s in staged.items() if s["kind"] == "empirical"
            ]
            return pool

        def resolve(rid):
            if rid.startswith("staged:"):
                return staged[rid[len("staged:"):]]
            return memory.reasoning[rid]

        for sid in ordered_ids:
            kind = kinds[sid]
            if kind == "hypothesis":
                # the hypothesis step is never dispatched: no lookup, no
                # testing, no inference judging — it is wired at commit only
                continue
            step = steps[sid]
            entry = cache.setdefault(step.statement, {})

            # unchanged statement from a previous iteration: reuse its outcome
            if "existing_id" in entry:
                run_log.event("note", f"step cached — skipped ({sid})")
                resolved[sid] = entry["existing_id"]
                continue
            if "staged" in entry:
                run_log.event("note", f"step cached — skipped ({sid})")
                staged[sid] = dict(entry["staged"])
                resolved[sid] = f"staged:{sid}"
                continue

            if kind == "fact":
                # a fact step arrives as an assertion; rewrite it into the
                # lookup question the websearch agent will answer
                question = supervisor.fact_question(step.statement)
                candidates = fact_pool()
                top5 = sim_search.top_candidates(question, candidates, k=5)
                verdicts = supervisor.dedup(question, top5) if top5 else []
                same = next((v["node_id"] for v in verdicts if v["verdict"] == "same"), None)
                if same is not None:
                    entry["existing_id"] = same
                    resolved[sid] = same
                else:
                    staged[sid] = {"kind": "fact", "question": question}
                    entry["staged"] = staged[sid]
                    resolved[sid] = f"staged:{sid}"

            elif kind == "assumption":
                staged[sid] = {"kind": "assumption", "statement": step.statement}
                entry["staged"] = staged[sid]
                resolved[sid] = f"staged:{sid}"

            elif kind == "empirical":
                qp = supervisor.research_question(step.statement)
                question, prediction = qp["question"], qp["prediction"]
                entry["question"], entry["prediction"] = question, prediction  # log-only
                opposite = supervisor.opposite_prediction(question, prediction)
                candidates = empirical_pool()
                matches = sim_search.top_candidates_multi(
                    [f"{question} — {prediction}", f"{question} — {opposite}"], candidates, k=5
                )
                verdicts = supervisor.dedup(question, matches) if matches else []
                same = next((v for v in verdicts if v["verdict"] == "same"), None)
                opp = next((v for v in verdicts if v["verdict"] == "opposite"), None)

                if same is not None:
                    entry["existing_id"] = same["node_id"]
                    resolved[sid] = same["node_id"]
                elif opp is not None:
                    matched = resolve(opp["node_id"])
                    matched_conclusion = matched.conclusion if isinstance(matched, EmpiricalNode) else None
                    derived = None
                    if matched_conclusion is True:
                        # a matched False leaves the new node open (rule 6)
                        derived = (False, f"derived: opposite of {opp['node_id']}, which is concluded True")
                    staged[sid] = {"kind": "empirical", "question": question, "prediction": prediction, "derived": derived, "opp": opp["node_id"]}
                    entry["staged"] = staged[sid]
                    resolved[sid] = f"staged:{sid}"
                else:
                    staged[sid] = {"kind": "empirical", "question": question, "prediction": prediction, "derived": None}
                    entry["staged"] = staged[sid]
                    resolved[sid] = f"staged:{sid}"

            else:  # "inference"
                staged[sid] = {"kind": "inference", "statement": step.statement}
                entry["staged"] = staged[sid]
                resolved[sid] = f"staged:{sid}"

        # -- log-only: staging summary (no behavior change) --
        stage_rows = []
        for sid in ordered_ids:
            e = cache.get(steps[sid].statement, {})
            s = staged.get(sid)
            q = p = ""
            if kinds[sid] == "empirical":
                src = s if s is not None else e
                q, p = src.get("question", ""), src.get("prediction", "")
            rid = resolved.get(sid)
            if rid is None:
                dedup = ""
            elif not rid.startswith("staged:"):
                dedup = f"same as {rid}"
            elif s is not None and s.get("opp"):
                dedup = f"opposite of {s['opp']}"
            else:
                dedup = "new"
            stage_rows.append({
                "id": sid, "statement": steps[sid].statement, "kind": kinds[sid],
                "question": q, "prediction": p, "dedup": dedup,
            })
        run_log.event("stage", json.dumps({"rows": stage_rows}))

        # -- data gate on every newly staged empirical node --
        run_log.set_context(hyp_label, "supervisor ↔ data handler (data gate)")
        local_plans: dict = {}
        gate_retry_id = None
        gate_feedback = None
        for lid, s in staged.items():
            if s["kind"] != "empirical":
                continue
            entry = cache[steps[lid].statement]
            gate_result = entry.get("gate")
            if gate_result is None:
                gate_result = _gate(agents, s["question"])
                entry["gate"] = gate_result
            else:
                run_log.event("note", f"step cached — skipped (gate for {lid})")
            if not gate_result["testable"]:
                run_log.event("note", f"gate fail — {lid}: {gate_result['reason']}")
                gate_retry_id = lid
                gate_feedback = supervisor.gate_feedback(
                    steps[lid].statement, gate_result["reason"]
                )
                break
            run_log.event("note", f"gate pass — {lid}")
            local_plans[lid] = gate_result["plan"]

        if gate_retry_id is not None:
            if bounce >= MAX_BOUNCE:
                run_log.event("note", "discarded")
                return "discarded"
            run_log.set_context(hyp_label, "hypothesis agent ↔ supervisor")
            run_log.event("note", f"revise bounce {bounce + 1}: gate failed on {gate_retry_id}")
            steps = hypothesis.revise(hyp_statement, steps, gate_retry_id, gate_feedback)
            bounce += 1
            continue

        # everything staged and gated -- commit
        break

    # -- commit: mint real ids, apply free settlements, wire, add the hypothesis --
    real_id: dict = {}
    for lid, s in staged.items():
        if s["kind"] == "fact":
            real_id[lid] = memory.add_fact(s["question"])
        elif s["kind"] == "assumption":
            real_id[lid] = memory.add_assumption(s["statement"])
        elif s["kind"] == "empirical":
            real_id[lid] = memory.add_empirical(s["question"], s["prediction"])
        else:
            real_id[lid] = memory.add_inference(s["statement"])

    for lid, s in staged.items():
        if s.get("derived") is not None:
            verdict, note = s["derived"]
            memory.record_result(real_id[lid], verdict, ExperimentRecord(method="derived, not tested", results=note))

    for lid, plan in local_plans.items():
        plans[real_id[lid]] = plan

    def final_id(rid):
        return real_id[rid[len("staged:"):]] if rid.startswith("staged:") else rid

    # The hypothesis step itself never becomes a node: its parents wire
    # straight to the hypothesis record instead.
    hyp_id = memory.add_hypothesis(hyp_statement)
    child_ids = set()
    for sid, step in steps.items():
        if kinds[sid] == "hypothesis":
            continue
        to_id = final_id(resolved[sid])
        for parent_sid in step.parents:
            from_id = final_id(resolved[parent_sid])
            memory.connect(hyp_id, from_id, to_id)
            child_ids.add(from_id)
    for sid, step in steps.items():
        if kinds[sid] == "hypothesis":
            continue
        this_id = final_id(resolved[sid])
        if this_id not in child_ids:
            memory.connect_to_hypothesis(this_id, hyp_id)

    cycles = memory.find_cycles()
    run_log.event("note", f"committed as {hyp_id}")
    commit_nodes = []
    for sid in steps:
        if kinds[sid] == "hypothesis":
            continue
        rid = resolved[sid]
        was_staged = rid.startswith("staged:")
        s = staged.get(rid[len("staged:"):]) if was_staged else None
        d = s.get("derived") if s else None
        commit_nodes.append({
            "id": final_id(rid), "kind": kinds[sid], "text": steps[sid].statement,
            "new": was_staged, "derived": d[1] if d else "",
        })
    run_log.event("commit", json.dumps({"hypothesis_id": hyp_id, "nodes": commit_nodes}))
    return {"hypothesis_id": hyp_id, "cycles": cycles}


# ---------------------------------------------------------------------------
# Dependency scheduler
# ---------------------------------------------------------------------------

def ready_empirical(memory: ExperienceMemory, plans: dict) -> list:
    """Pure, LLM-free readiness scan: fact nodes with no answer yet are
    always ready; an untested empirical node (that passed its design-time
    gate, i.e. has an entry in plans) is ready once every fact/empirical
    parent wired to it (across every hypothesis using it) is filled --
    fact: answer non-empty; empirical: verified True. Inference parents are
    never a readiness gate (rule 12: an inconclusive parent never blocks)."""
    ready = []
    for node_id, node in memory.reasoning.items():
        if isinstance(node, FactNode):
            if not node.answer:
                ready.append(node_id)
            continue
        if not isinstance(node, EmpiricalNode):
            continue
        if node.verified:
            continue
        if node_id not in plans:
            continue
        parent_ids = set()
        for hyp in memory.hypotheses.values():
            parent_ids.update(hyp.upstream.get(node_id, []))
        all_filled = True
        for parent_id in parent_ids:
            parent = memory.reasoning.get(parent_id)
            if isinstance(parent, FactNode) and not parent.answer:
                all_filled = False
                break
            if isinstance(parent, EmpiricalNode) and not parent.verified:
                all_filled = False
                break
        if all_filled:
            ready.append(node_id)
    return ready


def run_research_pass(questions: list, db_path: str, memory_path: str) -> dict:
    memory = ExperienceMemory.load(memory_path) if Path(memory_path).exists() else ExperienceMemory()
    agents = build_agents(db_path)
    dispatch = build_dispatch_graph(agents, memory)

    overview_lines = [
        f"{hid}: {h.statement} (conclusion={h.conclusion})" for hid, h in memory.hypotheses.items()
    ]
    overview = "\n".join(overview_lines) if overview_lines else "no existing hypotheses yet"

    plans: dict = {}
    discards = 0
    cycles: set = set()

    # untested assumptions from earlier passes join the question feed, so
    # common knowledge the reasoning leaned on gets proposed for real testing
    assumption_questions = [
        f"earlier research leaned on this untested assumption — can a venue-testable version of it be checked? \"{n.statement}\""
        for n in memory.reasoning.values() if isinstance(n, AssumptionNode)
    ]
    all_questions = list(questions) + assumption_questions

    run_log.set_context(None, "hypothesis agent — generate batch")
    run_log.event("note", "questions fed in:\n" + "\n".join(all_questions))
    batch = agents["hypothesis"].generate_batch(all_questions, overview)
    run_log.event("batch", json.dumps(batch))
    if len(batch) > HYPOTHESIS_CAP:
        ranked = agents["hypothesis"].rank(batch)
        ranked = [h for h in ranked if h in batch]
        ranked += [h for h in batch if h not in ranked]
        batch = ranked[:HYPOTHESIS_CAP]
        run_log.event("rank_cap", json.dumps(batch))
    for hyp_statement in batch:
        try:
            result = process_hypothesis(hyp_statement, agents, memory, plans)
        except Exception:
            run_log.set_context(hyp_statement[:80], "hypothesis agent ↔ supervisor")
            run_log.event("note", "hypothesis crashed:\n" + traceback.format_exc())
            run_log.event("note", "discarded")
            discards += 1
            continue
        if result == "discarded":
            discards += 1
        else:
            cycles.update(result["cycles"])

    skipped = []
    tested_node_ids = []
    retest_counts: dict = {}

    def first_hypothesis_of(node_id):
        """First hypothesis id whose wiring uses this node (label only)."""
        for hid, hyp in memory.hypotheses.items():
            if (
                node_id in hyp.nodes
                or node_id in hyp.upstream
                or any(node_id in parents for parents in hyp.upstream.values())
            ):
                return hid
        return None

    def dispatch_node(node_id, question, prediction):
        run_log.set_context(first_hypothesis_of(node_id), f"researcher — node {node_id}")
        state: NodeState = {
            "node_id": node_id,
            "question": question,
            "prediction": prediction,
            "parents": _collect_parents(memory, node_id),
            "plan": None,
            "data_available": None,
            "feedback": None,
            "assumptions_dict": None,
            "synthesis": None,
            "form": None,
            "statistics": None,
            "verdict": None,
            "values": None,
            "interpretation": None,
        }
        try:
            final_state = dispatch.invoke(state)
        except Exception:
            run_log.event("note", "node crashed:\n" + traceback.format_exc())
            plans.pop(node_id, None)
            skipped.append(node_id)
            return
        if not final_state.get("data_available", True):
            # a gate-failed node must leave the plans map or ready_empirical re-offers it forever
            plans.pop(node_id, None)
            skipped.append(node_id)
            return
        tested_node_ids.append(node_id)

    while True:
        ready = ready_empirical(memory, plans)
        if not ready:
            break
        for node_id in ready:
            node = memory.reasoning[node_id]
            if isinstance(node, FactNode):
                run_log.set_context(first_hypothesis_of(node_id), f"websearch — node {node_id}")
                try:
                    answer = agents["websearch"].answer(node.question)
                    memory.record_fact(node_id, answer.answer or "no answer found", answer.sources or "none")
                    run_log.event("memupdate", json.dumps({
                        "node": node_id, "kind": "fact",
                        "outcome": (answer.answer or "no answer found")[:200],
                    }))
                except Exception:
                    run_log.event("note", f"node {node_id} crashed:\n" + traceback.format_exc())
                    memory.record_fact(node_id, "no answer found", "none")
                    run_log.event("memupdate", json.dumps({
                        "node": node_id, "kind": "fact",
                        "outcome": "no answer found",
                    }))
            else:
                assert isinstance(node, EmpiricalNode)
                dispatch_node(node_id, node.question, node.prediction)

            for candidate_id in memory.retest_candidates(node_id):
                count = retest_counts.get(candidate_id, 0)
                if count >= 2:
                    continue
                retest_counts[candidate_id] = count + 1
                candidate = memory.reasoning[candidate_id]
                assert isinstance(candidate, EmpiricalNode)  # retest_candidates only names empirical nodes
                dispatch_node(candidate_id, candidate.question, candidate.prediction)

    # -- judge inference nodes whose parents are all filled --
    for node_id, node in memory.reasoning.items():
        if not isinstance(node, InferenceNode):
            continue
        parent_ids = set()
        for hyp in memory.hypotheses.values():
            parent_ids.update(hyp.upstream.get(node_id, []))
        upstream_summaries = []
        all_filled = True
        for parent_id in parent_ids:
            parent = memory.reasoning[parent_id]
            if isinstance(parent, FactNode):
                if not parent.answer:
                    all_filled = False
                    break
                upstream_summaries.append({"node_id": parent_id, "kind": "fact", "question": parent.question, "answer": parent.answer})
            elif isinstance(parent, EmpiricalNode):
                if not parent.verified:
                    all_filled = False
                    break
                upstream_summaries.append({
                    "node_id": parent_id, "kind": "empirical", "question": parent.question,
                    "prediction": parent.prediction, "conclusion": parent.conclusion,
                })
            elif isinstance(parent, AssumptionNode):
                upstream_summaries.append({"node_id": parent_id, "kind": "assumption", "statement": parent.statement})
            else:
                upstream_summaries.append({"node_id": parent_id, "kind": "inference", "statement": parent.statement})
        if not all_filled:
            continue

        targets = []
        for hyp_id, hyp in memory.hypotheses.items():
            for child_id, child_parents in hyp.upstream.items():
                if node_id in child_parents:
                    targets.append({"target_id": child_id})
            if node_id in hyp.nodes:
                targets.append({"target_id": hyp_id})
        if not targets:
            continue

        run_log.set_context(first_hypothesis_of(node_id), f"supervisor — inference {node_id}")
        try:
            verdicts = agents["supervisor"].judge_inference(node.statement, upstream_summaries, targets)
            memory.record_inference(node_id, verdicts)
            run_log.event("memupdate", json.dumps({
                "node": node_id, "kind": "inference",
                "outcome": "; ".join(
                    f"{tid}: " + {True: "supported", False: "not supported"}.get(v, "inconclusive")
                    for tid, v in verdicts.items()
                ),
                "detail": "",
            }))
        except Exception:
            run_log.event("note", f"node {node_id} crashed:\n" + traceback.format_exc())
            continue

    for hyp_id in memory.hypotheses:
        memory.check(hyp_id)

    memory.save(memory_path)

    run_log.set_context(None, None)

    # per hypothesis, the untested assumptions its wiring leans on (derived
    # from memory, not stored) — so a conclusion shows what it rests on
    leaned_on: dict = {}
    for hid, hyp in memory.hypotheses.items():
        used = set(hyp.nodes)
        for node_id, parents in hyp.upstream.items():
            used.add(node_id)
            used.update(parents)
        statements = [
            memory.reasoning[nid].statement
            for nid in sorted(used)
            if nid in memory.reasoning and isinstance(memory.reasoning[nid], AssumptionNode)
        ]
        if statements:
            leaned_on[hid] = statements

    return {
        "hypotheses": {hid: h.conclusion for hid, h in memory.hypotheses.items()},
        "leaned_on_assumptions": leaned_on,
        "nodes_tested": tested_node_ids,
        "discards": discards,
        "cycles": sorted(cycles),
        "skipped": skipped,
    }


if __name__ == "__main__":
    mem = ExperienceMemory()

    h1 = mem.add_hypothesis("test hypothesis")
    fact_id = mem.add_fact("was there an event nearby?")
    emp_id = mem.add_empirical("does the event raise walk-ins?", "yes")
    mem.connect(h1, fact_id, emp_id)
    mem.connect_to_hypothesis(emp_id, h1)

    plans = {emp_id: object()}  # design-time gate already passed for emp_id

    # fact has no answer yet -> always ready; empirical's fact parent is
    # unfilled -> not ready
    ready = ready_empirical(mem, plans)
    assert fact_id in ready, ready
    assert emp_id not in ready, ready

    # fill the fact -> empirical becomes ready
    mem.record_fact(fact_id, "yes, a street fair", "local paper")
    ready = ready_empirical(mem, plans)
    assert emp_id in ready, ready
    assert fact_id not in ready, ready  # answer non-empty now

    # empirical without a plan (never passed the design-time gate) never ready
    emp_id2 = mem.add_empirical("does rain raise walk-ins?", "yes")
    mem.connect_to_hypothesis(emp_id2, h1)
    ready = ready_empirical(mem, plans)
    assert emp_id2 not in ready, ready

    # once tested, an empirical node drops out of the ready set
    mem.record_result(emp_id, True, ExperimentRecord(results="confirmed"))
    ready = ready_empirical(mem, plans)
    assert emp_id not in ready, ready

    # -- revision cache: a bounce must not re-roll untouched steps --
    from hypothesis_agent import Step

    class _StubHypothesis:
        def reasoning_steps(self, hypothesis):
            return {
                "s1": Step(id="s1", statement="emp v1", parents=[]),
                "s2": Step(id="s2", statement="an inference step", parents=["s1"]),
                "s3": Step(id="s3", statement="stub hypothesis", parents=["s2"]),
            }

        def revise(self, hypothesis, steps, broken_id, feedback):
            assert broken_id == "s1", broken_id
            assert feedback == "narrow it", feedback
            return {
                "s1": Step(id="s1", statement="emp v2", parents=[]),
                "s2": steps["s2"],
                "s3": steps["s3"],
            }

    class _StubSupervisor:
        def __init__(self):
            self.classify_calls = []
            self.rq_calls = []
            self.gate_feedback_calls = []

        def classify_steps(self, steps):
            self.classify_calls.append([s["statement"] for s in steps])

            def kind(text):
                # the hypothesis step is stamped in code and never sent here
                return "empirical" if text.startswith("emp") else "inference"

            return [{"step": s["statement"], "kind": kind(s["statement"])} for s in steps]

        def research_question(self, step):
            self.rq_calls.append(step)
            return {"question": f"{step}?", "prediction": "up"}

        def opposite_prediction(self, question, prediction):
            return "down"

        def dedup(self, question, candidates):
            raise AssertionError("dedup must not run: candidate pools are empty")

        def gate_feedback(self, step, reason):
            self.gate_feedback_calls.append((step, reason))
            return "narrow it"

    class _StubDataHandler:
        def __init__(self):
            self.check_calls = []

        def check_data(self, question):
            self.check_calls.append(question)
            testable = question != "emp v1?"
            reason = "" if testable else "side B has too few days"
            return {"plan": object(), "testable": testable, "reason": reason}

    stub_agents = {
        "hypothesis": _StubHypothesis(),
        "supervisor": _StubSupervisor(),
        "data_handler": _StubDataHandler(),
    }
    mem2 = ExperienceMemory()
    plans2: dict = {}
    result = process_hypothesis("stub hypothesis", stub_agents, mem2, plans2)
    assert result != "discarded", result
    sup = stub_agents["supervisor"]
    dh = stub_agents["data_handler"]
    # first pass classifies both non-hypothesis steps (the terminal step is
    # stamped in code, never classified); after the bounce only the rewritten one
    assert sup.classify_calls == [
        ["emp v1", "an inference step"],
        ["emp v2"],
    ], sup.classify_calls
    # research_question and the gate run once per statement text, never re-rolled
    assert sup.rq_calls == ["emp v1", "emp v2"], sup.rq_calls
    assert dh.check_calls == ["emp v1?", "emp v2?"], dh.check_calls
    assert sup.gate_feedback_calls == [("emp v1", "side B has too few days")], sup.gate_feedback_calls
    assert len(plans2) == 1, plans2

    print("ok")
