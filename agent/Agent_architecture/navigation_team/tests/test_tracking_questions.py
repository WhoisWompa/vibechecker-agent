"""Every model call says what it was for and which questions it served; free answers are counted."""

import sys
from pathlib import Path

NAV = Path(__file__).resolve().parents[1]
if str(NAV) not in sys.path:
    sys.path.insert(0, str(NAV))

from tracking import Tracker  # noqa: E402


def _call(tr, run_id, tokens_in, tokens_out):
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, LLMResult

    tr.on_chat_model_start({}, [[AIMessage(content="q")]], run_id=run_id, metadata={"agent": "daily"})
    msg = AIMessage(content="ok", usage_metadata={"input_tokens": tokens_in, "output_tokens": tokens_out,
                                                  "total_tokens": tokens_in + tokens_out})
    tr.on_llm_end(LLMResult(generations=[[ChatGeneration(message=msg)]]), run_id=run_id)


def test_calls_carry_purpose_and_questions_and_costs_split_per_question():
    tr = Tracker()
    tr.context(day=1, date="2024-04-05", loop="morning")
    q1, q2 = "what settings suit a Friday in this cafe?", "what do people want from this room when it rains?"
    tr.context(purpose="memory match", questions=[q1, q2])
    _call(tr, "m1", 100, 50)                      # one batched call for two questions
    tr.context(purpose="memory answer", questions=[q2])
    _call(tr, "a1", 60, 40)                       # only q2 had something to answer
    tr.context(purpose="morning plan", questions=[])
    _call(tr, "p1", 1000, 200)                    # the plan call serves no question
    tr.free("what should be done about a big group booked in this cafe?", "exact")

    assert [r["purpose"] for r in tr.records] == ["memory match", "memory answer", "morning plan"]
    assert tr.records[0]["questions"] == [q1, q2] and tr.records[2]["questions"] == []
    by_purpose = {r["purpose"]: r for r in tr.summary("purpose")}
    assert by_purpose["morning plan"]["input_tokens"] == 1000 and by_purpose["memory match"]["calls"] == 1

    rows = {r["question"]: r for r in tr.by_question()}
    assert rows[q1]["total_tokens"] == 75 and rows[q1]["calls_share"] == 0.5          # half of the match call
    assert rows[q2]["total_tokens"] == 175 and rows[q2]["calls_share"] == 1.5         # half the match + the answer
    assert rows[q2]["purposes"] == ["memory answer", "memory match"]
    free = rows["what should be done about a big group booked in this cafe?"]
    assert free["free_answers"] == 1 and free["total_tokens"] == 0 and free["usd"] == 0
    assert "memory match" in tr.table("purpose")


def test_memory_door_labels_its_calls(tmp_path, monkeypatch):
    import memory_door
    from seed_memory import seed

    seed(str(tmp_path / "memory.json"))
    labels = []

    class Recorder:
        def context(self, **kw):
            labels.append(("context", kw.get("purpose"), list(kw.get("questions") or [])))

        def free(self, question, how="exact"):
            labels.append(("free", how, [question]))

    class FakeStructured:
        def __init__(self, schema):
            self.schema = schema

        def invoke(self, prompt):
            if self.schema is memory_door._MatchAll:
                return memory_door._MatchAll(verdicts=[memory_door._Verdict(question_index=0, node_id="r2", verdict="same"),
                                                       memory_door._Verdict(question_index=1, node_id="r1", verdict="different")])
            return memory_door._AnswerAll(answers=[memory_door._Answer(question_index=0, answer="warmer and brighter")])

    class FakeLLM:
        def with_structured_output(self, schema):
            return FakeStructured(schema)

    monkeypatch.setattr(memory_door, "top_candidates", lambda q, cands, k=5: cands[:3])
    door = memory_door.MemoryDoor(FakeLLM(), str(tmp_path / "bank.json"), str(tmp_path / "memory.json"), tracker=Recorder())
    out = door.ask_all(["what should be done about a big group booked in this cafe?",   # exact hit: free
                        "what do people want when it pours?",                            # model match -> r2
                        "what settings suit a Monday in this cafe?"],                    # nothing
                       "daily", "2024-04-05", 0, "test")
    assert [o["known"] for o in out] == [True, True, False]
    assert labels[0] == ("free", "exact", ["what should be done about a big group booked in this cafe?"])
    assert labels[1] == ("context", "memory match", ["what do people want when it pours?", "what settings suit a Monday in this cafe?"])
    assert labels[2] == ("context", "memory answer", ["what do people want when it pours?"])
