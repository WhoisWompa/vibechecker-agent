## Open questions
1. The exact cafe outcome metric.
2. ~~Which sensors in, which actuators out.~~ **Settled and built** — see `simulation.md` §3.
3. How the simulation models occupant response — the crux; the agent is only as credible as what it learns against. **Partly built** — the room physics and the arrival/dwell process run; the hidden functions `f` and `g` behind them are still open (`simulation.md` §8).
4. What "adapt" means concretely: LLM reasoning over a history log, or a real optimisation loop.