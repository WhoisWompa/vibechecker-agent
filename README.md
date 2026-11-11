# Vibechecker

Team welovechagee, IGNITE Agentic AI Hackathon 2026. An agent that runs a cafe's light,
temperature, music volume and fan, and learns the room over days. `problem_statement.md`
has the idea; `agent/goal.md` the objective; `agent/architecture.md` the workflow.

```
agent/                      the agents (Lionel: analysis team, memory, data handler; Ashley: navigation team)
  Agent_architecture/
    navigation_team/        daily + intraday agents, memory door, tracking, runner  (README inside)
    analysis_team/          hypothesis, supervisor, researcher, websearch and data handler agents;
                            workflow.py is one research pass over the master record
    memory_agent/           the only door to the experience memory (both teams ask it)
    reflection_agent/       turns scanner flags into questions; writes verdicts into the checklist
    after_close.py          the bridge: question bank -> research pass -> memory -> bank + checklist
    scanner.py              statistical flags over the master record, no model
    memory.py, llm.py       the experience memory and the model factory (shared)
    venue_reference.md      the settings, sensors and intent columns every prompt sees
  .env                      model + AWS keys (never commit)
simulation/                 Lionel's cafe (the agents only use its public API, see agent/how_to_use_simulation.md)
data/weather/               NEA 2024 forecasts the simulation replays (download the large 2-hour CSV separately from data.gov.sg, NEA 2-hour weather forecast 2024, and place it here)
papers/, docs/, scripts/    reading, notes, provisioning
run_navigation.py           run the navigation team against the cafe (--analysis nightly runs the analysis team after close)
run_analysis.py             run the analysis team once over a state folder (--live streams every prompt to a page)
run_report.py, run_timeline.py   cost tables and the scrubbable day timeline of a run
run_sim.py                  run the cafe alone
state_nav/                  run outputs (plans, master record, question bank, memory, analysis reports, tokens)
```

How the two teams join (one state folder, `--state`):

```
morning   daily agent asks memory (memory_agent) -> plan; unknowns go to question_bank.json
day       intraday agent runs the plan, asks memory on surprises, writes every minute to history.db
close     after_close.py: open bank questions + scanner flags -> Lionel's research pass over
          history.db -> verdicts saved in memory.json -> bank entries marked answered,
          checklist.md updated -> report in analysis/<date>.json
morning   the memory door re-reads memory.json; yesterday's question is answered today
```

```
uv run python run_navigation.py --days 3 --seed 3 --start 2024-04-05 --preset demo \
    --note "big group of 12 at 3pm-5pm" --memory state_nav/demo/memory.json \
    --analysis nightly --state state_nav/joined
uv run python run_analysis.py --state state_nav/joined --live      # the same pass by hand, with the live view
```

Setup, once:

```
uv sync --extra bedrock --extra calendar --extra memory --extra dev
uv run pytest -q                                   # navigation team + bridge, no model needed
uv run python agent/Agent_architecture/memory.py   # the analysis side's self-checks: memory.py, scanner.py,
                                                   # reflection_agent/reflection_agent.py, analysis_team/workflow.py
```
