# Vibechecker

An AI agent system that tunes a cafe's lighting, temperature, music volume and
ventilation through the day, and learns what works for that room over weeks.

Most shared spaces set the thermostat and lights once and leave them. The best
setting changes with the weather, the crowd and the hour, and nobody is
standing at the controls to adjust it. Vibechecker watches the room, adjusts
the settings, checks what happened afterwards, and remembers what it learned.

Built for the IGNITE Agentic AI Hackathon 2026 by Team welovechagee, using
LangChain, LangGraph and AWS Bedrock, against a simulated cafe.

## How it works

Two teams of agents work on different clocks and share one memory:

- **Navigation team (daily).** A daily agent plans the day's settings. An
  intraday agent watches the room and changes the plan only when something
  shifts, such as the weather, a crowd, or stuffy air.
- **Analysis team (weekly).** A scanner finds odd patterns in the day's
  records. Agents turn them into hypotheses, test them on the data, and write
  what they learned into the shared memory as short habits for the daily agent.

The full design is in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). The idea
behind the project is in [`problem_statement.md`](problem_statement.md).

## Repository layout

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
simulation/                 Lionel's cafe (the agents only use its public API, see agent/how_to_use_simulation.md)
data/weather/               NEA 2024 forecasts the simulation replays (download the large 2-hour CSV separately from data.gov.sg, NEA 2-hour weather forecast 2024, and place it here)
data/                       weather inputs and saved experiment results
docs/, scripts/             design notes, provisioning and dashboards
run_navigation.py           run the navigation team against the cafe (--analysis nightly runs the analysis team after close)
run_analysis.py             run the analysis team once over a state folder (--live streams every prompt to a page)
run_report.py, run_timeline.py   cost tables and the scrubbable day timeline of a run
run_sim.py                  run the cafe alone
state_nav/                  created when you run: plans, master record, question bank, memory, reports, tokens
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

Setup, once. Copy `agent/.env.example` to `agent/.env` and fill in your model and AWS settings.

```
uv sync --extra bedrock --extra calendar --extra memory --extra dev
uv run pytest -q                                   # navigation team + bridge, no model needed
uv run python agent/Agent_architecture/memory.py   # the analysis side's self-checks: memory.py, scanner.py,
                                                   # reflection_agent/reflection_agent.py, analysis_team/workflow.py
```
