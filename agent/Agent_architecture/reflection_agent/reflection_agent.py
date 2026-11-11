"""Reflection agent: a stateless courier (decisions rule 56) that runs twice
around each research run over the scanner's flag list.

before_run turns each scanner flag into a plain question, asks the memory
agent, and hands back the question/answer pairs. after_run re-asks each
question for a fresh answer and, if there is now something to say, applies
one add/retire/merge/nothing action to the plain-markdown checklist file.

The reflection agent keeps no state of its own between before_run and
after_run — the weekly-pass caller holds the returned items and passes them
back in.
"""

from pathlib import Path

from pydantic import BaseModel

_DIR = Path(__file__).parent


def _prompt(name: str) -> str:
    return (_DIR / f"{name}.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Structured-output models
# ---------------------------------------------------------------------------

class FlagQuestion(BaseModel):
    question: str


class ChecklistAction(BaseModel):
    action: str
    line: str
    target_line: str


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class ReflectionAgent:
    """Stateless courier between the scanner's flags and the memory agent,
    and between the memory agent's answers and the checklist file."""

    def __init__(self, llm, memory_agent, checklist_path):
        self.llm = llm
        self.memory_agent = memory_agent
        self.checklist_path = checklist_path

    # -- public --

    def before_run(self, flags: list[dict]) -> list[dict]:
        items = []
        for flag in flags:
            structured_llm = self.llm.with_structured_output(FlagQuestion).with_retry(stop_after_attempt=3)
            prompt = _prompt("flag_to_question").format(text=flag["text"])
            result: FlagQuestion = structured_llm.invoke(prompt)
            question = result.question
            answer = self.memory_agent.ask(question)
            items.append({"flag": flag, "question": question, "answer": answer})
        return items

    def after_run(self, items: list[dict]) -> None:
        for item in items:
            fresh_answer = self.memory_agent.ask(item["question"])
            if fresh_answer == "no evidence yet":
                continue

            raw_lines = self._load_checklist()
            checklist_lines = [raw[2:] for raw in raw_lines if raw.startswith("- ")]
            structured_llm = self.llm.with_structured_output(ChecklistAction).with_retry(stop_after_attempt=3)
            prompt = _prompt("update_checklist").format(
                question=item["question"],
                answer=fresh_answer,
                checklist_lines=checklist_lines,
            )
            result: ChecklistAction = structured_llm.invoke(prompt)
            self._apply_action(result, raw_lines)

    # -- checklist helpers --

    def _load_checklist(self) -> list[str]:
        path = Path(self.checklist_path)
        if not path.exists():
            return []
        return path.read_text(encoding="utf-8").splitlines()

    def _write_checklist(self, lines: list[str]) -> None:
        path = Path(self.checklist_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        content = "\n".join(lines) + "\n"
        path.write_text(content, encoding="utf-8")

    def _apply_action(self, result: ChecklistAction, lines: list[str]) -> None:
        if result.action == "add":
            lines.append(f"- {result.line}")
            self._write_checklist(lines)
        elif result.action == "retire":
            target = f"- {result.target_line}"
            if target in lines:
                lines.remove(target)
                self._write_checklist(lines)
        elif result.action == "merge":
            target = f"- {result.target_line}"
            if target in lines:
                idx = lines.index(target)
                lines[idx] = f"- {result.line}"
                self._write_checklist(lines)
        # unknown action or unmatched target_line -> do nothing


if __name__ == "__main__":
    import tempfile

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

    # -- stub memory agent --

    class _StubMemoryAgent:
        def __init__(self, answers):
            self._answers = list(answers)

        def ask(self, question):
            return self._answers.pop(0)

    checklist_dir = tempfile.mkdtemp()
    checklist_path = str(Path(checklist_dir) / "checklist.md")

    # (a) before_run turns 2 flags into 2 items with questions and answers
    flags = [
        {"check": "walkins", "label": "Monday walk-ins", "value": 12, "baseline": 30,
         "sd_units": -1.2, "n": 10, "text": "Monday walk-ins average 18 below the plan"},
        {"check": "revenue", "label": "Tuesday revenue", "value": 500, "baseline": 700,
         "sd_units": -0.9, "n": 10, "text": "Tuesday revenue runs 200 below the plan"},
    ]
    llm_a = _StubLLM()
    llm_a.queue(FlagQuestion, [
        FlagQuestion(question="Why do Mondays run quieter than planned?"),
        FlagQuestion(question="Why does Tuesday revenue run below plan?"),
    ])
    mem_a = _StubMemoryAgent(["Mondays are quiet because of weather.", "no evidence yet"])
    agent_a = ReflectionAgent(llm_a, mem_a, checklist_path)
    items_a = agent_a.before_run(flags)
    assert len(items_a) == 2
    assert items_a[0]["question"] == "Why do Mondays run quieter than planned?"
    assert items_a[0]["answer"] == "Mondays are quiet because of weather."
    assert items_a[1]["answer"] == "no evidence yet"

    # (b) after_run with a "no evidence yet" fresh answer writes nothing
    mem_b = _StubMemoryAgent(["no evidence yet"])
    agent_b = ReflectionAgent(_StubLLM(), mem_b, checklist_path)
    agent_b.after_run([{"flag": flags[0], "question": "q", "answer": "prior"}])
    assert not Path(checklist_path).exists()

    # (c) after_run "add" appends the line
    llm_c = _StubLLM()
    llm_c.queue(ChecklistAction, [ChecklistAction(action="add", line="Mondays are quiet due to weather.", target_line="")])
    mem_c = _StubMemoryAgent(["Mondays are quiet due to weather."])
    agent_c = ReflectionAgent(llm_c, mem_c, checklist_path)
    agent_c.after_run([{"flag": flags[0], "question": "q", "answer": "prior"}])
    lines_c = agent_c._load_checklist()
    assert lines_c == ["- Mondays are quiet due to weather."]

    # (d) after_run "merge" replaces the existing line
    llm_d = _StubLLM()
    llm_d.queue(ChecklistAction, [ChecklistAction(
        action="merge",
        line="Mondays are quiet due to weather and low foot traffic.",
        target_line="Mondays are quiet due to weather.",
    )])
    mem_d = _StubMemoryAgent(["Mondays are quiet due to weather and low foot traffic."])
    agent_d = ReflectionAgent(llm_d, mem_d, checklist_path)
    agent_d.after_run([{"flag": flags[0], "question": "q", "answer": "prior"}])
    lines_d = agent_d._load_checklist()
    assert lines_d == ["- Mondays are quiet due to weather and low foot traffic."]

    # (e) after_run "retire" removes it
    llm_e = _StubLLM()
    llm_e.queue(ChecklistAction, [ChecklistAction(
        action="retire", line="", target_line="Mondays are quiet due to weather and low foot traffic.",
    )])
    mem_e = _StubMemoryAgent(["actually Mondays are busy"])
    agent_e = ReflectionAgent(llm_e, mem_e, checklist_path)
    agent_e.after_run([{"flag": flags[0], "question": "q", "answer": "prior"}])
    lines_e = agent_e._load_checklist()
    assert lines_e == [""]

    # (f) a "retire" whose target_line matches nothing leaves the file unchanged
    agent_f = ReflectionAgent(llm_e, mem_e, checklist_path)  # unused llm ok, reuse setup below
    llm_f = _StubLLM()
    llm_f.queue(ChecklistAction, [ChecklistAction(action="add", line="Weekend brunch draws steady crowds.", target_line="")])
    mem_f0 = _StubMemoryAgent(["Weekend brunch draws steady crowds."])
    agent_f0 = ReflectionAgent(llm_f, mem_f0, checklist_path)
    agent_f0.after_run([{"flag": flags[0], "question": "q", "answer": "prior"}])
    before_f = agent_f0._load_checklist()

    llm_f2 = _StubLLM()
    llm_f2.queue(ChecklistAction, [ChecklistAction(action="retire", line="", target_line="Nonexistent line")])
    mem_f2 = _StubMemoryAgent(["some new fresh answer"])
    agent_f2 = ReflectionAgent(llm_f2, mem_f2, checklist_path)
    agent_f2.after_run([{"flag": flags[0], "question": "q", "answer": "prior"}])
    after_f = agent_f2._load_checklist()
    assert after_f == before_f

    # (g) a hand-added header line survives an "add" rewrite
    header_dir = tempfile.mkdtemp()
    header_path = str(Path(header_dir) / "checklist.md")
    Path(header_path).write_text("# Checklist\n- Existing bullet.\n", encoding="utf-8")
    llm_g = _StubLLM()
    llm_g.queue(ChecklistAction, [ChecklistAction(action="add", line="New bullet.", target_line="")])
    mem_g = _StubMemoryAgent(["New bullet."])
    agent_g = ReflectionAgent(llm_g, mem_g, header_path)
    agent_g.after_run([{"flag": flags[0], "question": "q", "answer": "prior"}])
    lines_g = agent_g._load_checklist()
    assert "# Checklist" in lines_g
    assert "- New bullet." in lines_g

    print("ok")
