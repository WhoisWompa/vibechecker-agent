"""The after-close bridge: question bank -> analysis pass -> memory -> bank and checklist.

No model, no network: the models are fakes and the research pass is a stub that files one
verdict with Lionel's memory API. The last test drives the real cafe for two heuristic days
with the analysis switched on, to show the loop closes inside one run.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

NAV = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NAV))
import _paths  # noqa: E402,F401

from after_close import AfterClose, open_questions  # noqa: E402
from memory import ExperienceMemory, ExperimentRecord  # noqa: E402

BANK = [
    {"question": "what should be done about a big group booked in this cafe?", "tag": "new gap", "matched_node": None,
     "asked_by": "daily", "date": "2024-04-05", "t": 0, "situation": "morning", "status": "open"},
    {"question": "What should be done about a big group booked in this cafe?", "tag": "new gap", "matched_node": None,
     "asked_by": "intraday", "date": "2024-04-05", "t": 420, "situation": "note", "status": "open"},  # a repeat
    {"question": "what do people want from this room when it rains?", "tag": "urgency bump", "matched_node": "r2",
     "asked_by": "daily", "date": "2024-04-05", "t": 0, "situation": "morning", "status": "open"},
    {"question": "what works on a very hot day?", "tag": "budget", "matched_node": None,
     "asked_by": "intraday", "date": "2024-04-05", "t": 600, "situation": "", "status": "open"},  # out of budget, not a gap
    {"question": "how fast does the fan clear CO2 in this room?", "tag": "new gap", "matched_node": None,
     "error": "ThrottlingException", "asked_by": "intraday", "date": "2024-04-05", "t": 700, "status": "open"},
    {"question": "what settings suit a Thursday in this cafe?", "tag": "new gap", "matched_node": None,
     "asked_by": "daily", "date": "2024-04-04", "t": 0, "situation": "morning", "status": "answered"},
]


def test_open_questions_are_distinct_and_skip_budget_and_error_rows():
    qs = open_questions(BANK)
    assert qs == ["what should be done about a big group booked in this cafe?",
                  "what do people want from this room when it rains?"]
    assert open_questions(BANK, limit=1) == qs[:1]


class _Structured:
    """Fake structured-output model: answers by schema name, in Lionel's shapes."""

    def __init__(self, schema, memory_path):
        self.schema = schema
        self.memory_path = memory_path

    def with_retry(self, **kwargs):
        return self

    def invoke(self, prompt):
        name = self.schema.__name__
        if name == "FlagQuestion":
            return self.schema(question="Why do Wednesdays run busier than the other days?")
        if name == "Level1Judgment":
            # memory answers when a node's question is quoted in the prompt and it has a conclusion
            mem = ExperienceMemory.load(self.memory_path) if Path(self.memory_path).exists() else ExperienceMemory()
            for node in mem.reasoning.values():
                if getattr(node, "conclusion", None) is not None and node.question in prompt:
                    return self.schema(sufficient=True, answer=f"{node.prediction} (tested {node.conclusion})")
            return self.schema(sufficient=False, answer="")
        if name == "Level2Judgment":
            return self.schema(outcome="nothing", answer="")
        if name == "ChecklistAction":
            return self.schema(action="add", line="Wednesdays run busier than the other days: plan lunch as its own block.", target_line="")
        raise AssertionError(name)


class FakeLLM:
    def __init__(self, memory_path):
        self.memory_path = memory_path

    def with_structured_output(self, schema):
        return _Structured(schema, self.memory_path)


def _exact_matches(q, cands, k=5):
    """Stand-in for the embedding search: only a word-for-word match is a candidate."""
    want = " ".join(q.lower().split()).rstrip("?")
    return [c for c in cands if " ".join(c["question"].lower().split()).rstrip("?") == want][:k]


def _stub_research(*answer_for: str):
    """A research pass that settles the questions starting with ``answer_for`` True and leaves the rest."""

    def research(questions, db_path, memory_path):
        mem = ExperienceMemory.load(memory_path) if Path(memory_path).exists() else ExperienceMemory()
        hyps = {}
        for q in questions:
            if any(q.lower().startswith(a.lower()) for a in answer_for):
                r = mem.add_empirical(q, "music down 5 dB and the air 1 C cooler from 20 minutes before keeps the group longer")
                h = mem.add_hypothesis("a booked big group stays longer with quieter music and cooler air")
                mem.connect_to_hypothesis(r, h)
                mem.record_result(r, True, ExperimentRecord(method="before-after", results="stay 31 vs 26 min",
                                                            statistics={"p_value": 0.03, "n_per_side": {"group": 4, "none": 4}}))
                hyps[h] = mem.check(h)
        mem.save(memory_path)
        return {"hypotheses": hyps, "nodes_tested": list(mem.reasoning), "discards": 0, "cycles": [], "skipped": [],
                "questions_seen": list(questions)}

    return research


def _state(tmp_path, with_db=True):
    state = tmp_path / "state"
    state.mkdir()
    (state / "question_bank.json").write_text(json.dumps(BANK, indent=2), encoding="utf-8")
    (state / "checklist.md").write_text("# Checklist of habits\n- Read the owner's note for today.\n", encoding="utf-8")
    if with_db:
        import scanner
        scanner._build_test_db(str(state / "history.db"))  # 30 days with a planted outlier and a busy Wednesday
    return state


def test_after_close_files_verdicts_into_the_bank_and_the_checklist(tmp_path, monkeypatch):
    import memory_agent
    monkeypatch.setattr(memory_agent, "top_matches", _exact_matches)  # no embeddings in tests
    state = _state(tmp_path)
    mem_path = state / "memory.json"
    research = _stub_research("what should be done about a big group", "why do wednesdays")
    seen = {}

    def research_spy(questions, db_path, memory_path):
        seen["questions"] = list(questions)
        return research(questions, db_path, memory_path)

    pas = AfterClose(str(state), llm_factory=lambda name: FakeLLM(str(mem_path)), research=research_spy, log=lambda m: None)
    report = pas.run("2024-04-05")

    # the bank's distinct open questions went in, plus the unexplained flag question; not the budget/error rows
    assert seen["questions"][:2] == ["what should be done about a big group booked in this cafe?",
                                     "what do people want from this room when it rains?"]
    assert "Why do Wednesdays run busier than the other days?" in seen["questions"]
    assert not any("CO2" in q or "hot day" in q for q in seen["questions"])
    assert report["flags"], "the scanner should flag the planted anomalies"

    # the verdict came back through memory: both rows of the answered question flipped, the rest stay open
    bank = json.loads((state / "question_bank.json").read_text(encoding="utf-8"))
    big = [e for e in bank if e["question"].lower().startswith("what should be done about a big group")]
    assert all(e["status"] == "answered" and e["answered_on"] == "2024-04-05" and "music" in e["answer"] for e in big)
    rain = next(e for e in bank if e["question"].startswith("what do people want"))
    assert rain["status"] == "open" and rain["passes"] == 1
    assert next(e for e in bank if e["tag"] == "budget")["status"] == "open"
    assert [a["question"] for a in report["answered"]] == ["what should be done about a big group booked in this cafe?"]
    assert report["still_open"] == ["what do people want from this room when it rains?"]

    # the memory file holds the verdict and the checklist gained the line the reflection agent wrote
    mem = ExperienceMemory.load(str(mem_path))
    assert any(getattr(n, "conclusion", None) is True for n in mem.reasoning.values())
    lines = (state / "checklist.md").read_text(encoding="utf-8")
    assert "# Checklist of habits" in lines and "Wednesdays run busier" in lines
    assert report["checklist_after"] != report["checklist_before"]

    # the report and the pass log are on disk
    assert (state / "analysis" / "2024-04-05.json").exists()
    assert (state / "analysis" / "run_2024-04-05" / "events.jsonl").exists()
    passes = (state / "analysis" / "passes.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(passes) == 1 and json.loads(passes[0])["answered"] == 1


def test_a_crash_in_the_research_pass_is_reported_not_raised(tmp_path, monkeypatch):
    import memory_agent
    monkeypatch.setattr(memory_agent, "top_matches", _exact_matches)
    state = _state(tmp_path, with_db=False)

    def boom(questions, db_path, memory_path):
        raise RuntimeError("bedrock throttled")

    pas = AfterClose(str(state), llm_factory=lambda name: FakeLLM(str(state / "memory.json")), research=boom, log=lambda m: None)
    report = pas.run("2024-04-05")
    assert "bedrock throttled" in report["research_error"]
    assert report["flags"] == [] and report["answered"] == []
    bank = json.loads((state / "question_bank.json").read_text(encoding="utf-8"))
    assert all(e["status"] == "open" for e in bank if e["question"].startswith("what should"))


def test_no_open_questions_means_no_research(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "question_bank.json").write_text("[]", encoding="utf-8")
    calls = []
    pas = AfterClose(str(state), llm_factory=lambda name: FakeLLM(str(state / "memory.json")),
                     research=lambda *a: calls.append(a), log=lambda m: None)
    report = pas.run("2024-04-05")
    assert calls == [] and report["research"] is None and report["questions"] == []


@pytest.mark.slow
def test_two_heuristic_days_with_the_analysis_on_close_the_loop(tmp_path, monkeypatch):
    """Day 1 banks questions; the pass after close answers one; day 2's door reads the new memory."""
    import memory_agent
    monkeypatch.setattr(memory_agent, "top_matches", _exact_matches)
    from runner import NavRun

    state = tmp_path / "run"
    run = NavRun(days=2, seed=1, start_date="2024-04-05", state_dir=str(state), mode="heuristic",
                 notes=["big group of 12 at 3pm-5pm"], verbose=False, analysis="nightly")
    run.after_close_kwargs = {"llm_factory": lambda name: FakeLLM(str(state / "memory.json")),
                              "research": _stub_research("what should be done about a big group")}
    summaries = run.run()

    assert len(run.analysis_reports) == 2
    assert summaries[0]["analysis"]["answered"] >= 1
    assert (state / "memory.json").exists() and (state / "analysis" / "2024-04-05.json").exists()
    day1 = json.loads((state / "days" / "2024-04-05.json").read_text(encoding="utf-8"))
    assert day1["analysis"]["date"] == "2024-04-05" and any("after close" in l for l in day1["log"])
    # day 2's door saw a non-empty memory: in heuristic mode a non-exact question is banked with the note
    bank = json.loads((state / "question_bank.json").read_text(encoding="utf-8"))
    day2 = [e for e in bank if e.get("date") == "2024-04-06"]
    assert day2 and any(e.get("note") == "no model to judge similar memories" for e in day2)
    summary = json.loads((state / "summary.json").read_text(encoding="utf-8"))
    assert summary["analysis"] == "nightly" and len(summary["analysis_passes"]) == 2
