# Vibechecker workflow

## How the work is split

The workflow is split into teams. Each team owns one question, not one topic and not one stage of the process.

## Teams

**Team A — Goal team**
Runs once, at the start.
Turns the owner's stated goal into a clear objective.
Also comes up with a set of concrete metrics that can be used to check whether things are improving toward that objective.

**Team B — Analysis team**
Owns the question "what have we learned about this venue?". Runs the weekly pass around each research run, and answers questions from the other teams all week through the memory agent.

Members:

- **Memory agent** — the only door to the experience memory. Every other agent asks it in plain words. Three answer modes:
  - The question matches something proven → it gives the verdict.
  - The question touches a tested-but-unsettled hypothesis → it walks the chain behind it and says which steps are proven true, proven false, and still open.
  - The question matches nothing → "no evidence yet".
  - Every question it cannot fully answer goes into the unanswered-question log, tagged "new gap" (nothing matches) or "urgency bump" (matches an inconclusive node).

- **Scanner** — plain code, no LLM. Recomputed over the full history every week on plan-vs-actual gaps: bias by weekday, bias by time-of-day, slow drift, and any single day far outside normal. Prints a short list of flags with the numbers attached. A flag is material for a question, never a belief.

- **Reflection agent** — stateless. Runs twice around each research run:
  - Before the run: takes each scanner flag to the memory agent. Explained flags need nothing; unexplained ones enter the question log.
  - After the run: asks about its flags again. Each newly concluded answer becomes a one-line entry in the checklist. A proven-false answer can retire an entry. Near-duplicate entries merge. Inconclusive results produce no entry.

- **Research team** — hypothesis agent, supervisor, researchers (the loop in `Agent_architecture/experience_memory_decisions.md`). Reads the question log, generates the run's full batch of hypotheses upfront, processes them one at a time through the supervisor loop, then runs the retest cascade until a wave changes nothing.

**The checklist**
One file of one-line habits about what to check and ask before planning ("ask if anything special is happening tomorrow"). Read by the daily and intraday agents at planning time. Holds habits only — facts about the venue live in the experience memory. Starts with a few hand-written lines on day one.

**What happens to questions that stay unanswered**
- The hypothesis never became testable (bounce cap spent) → discarded without trace, question cleared from the log. If the pattern is real, the scanner flags it again in a later week with more data behind it.
- The hypothesis was tested but came back inconclusive → its whole chain stays in memory. A repeat flag lands as an urgency bump, and the data gate decides whether a retest is worth running.

Flukes never repeat, so they stay closed. Real patterns keep flagging until answered.

**Team C — Intraday team**
Event-driven. One agent, asleep between events.
Woken by exactly two events: a new 2-hour weather forecast, or an owner message about later today.
On waking, it reads the event and the daily notes, rewrites the rest of the day's settings list, pushes the update, and goes back to sleep.
Sensor readings are recorded all day and reviewed by the scanner at the weekly pass.

**Team D — Daily planning team**
Runs once, before each day. One agent.
Reads four things: the baseline, the checklist, the memory agent, and today's notes.
Those four things are everything it reads — it never looks at past days' plans or data directly; the baseline and the memory agent carry the past.
For every note, its first step is asking the memory agent what is known about that kind of event; questions the memory agent cannot answer go into the question log.
Produces the day's settings list, pushed into the mainframe, and the notes beside it — `{from, to, settings, reasons}` blocks explaining each stretch of the day.

**The baseline**
Plain code, no LLM. Computed from full history each morning: the typical settings for this weekday, stretch by stretch, handed to the daily agent as its starting table. The exact statistic gets specced together with the scanner statistics.

**Notes**
Owner-written, each covering a time window of the day. Two kinds:
- Fact note: the window and what is happening ("crowd from 10am to 12pm"). The daily agent decides the settings itself.
- Override note: the window, the exact settings, and the reason. Copied into the plan as-is; the daily agent plans around it.

Settings numbers enter a note from the owner's hand only. An owner message about a future day joins the note pile for the next morning's planning; a message about later today wakes the intraday agent.

**Memory between teams**
The daily, intraday, and reflection agents keep no private memories. All state lives in shared places: the experience memory (through the memory agent), the question log, the checklist, and the saved plans and notes.

## How a week flows

**Once, at setup**
Team A turns the owner's stated goal into an objective and a set of metrics.

**Every day**
The daily planning team builds the day's settings list and notes — reading the baseline, the checklist, the memory agent, and today's notes — and pushes the settings list into the mainframe. The day runs on that list. The intraday agent wakes only for a new 2-hour forecast or an owner message about later today, rewrites the rest of the day's list, and sleeps again. Unanswerable questions get logged the moment they happen. The settings list, the notes, and the day's data all get saved.

**Once a week — the weekly pass, in order**
1. The scanner recomputes over full history and prints its flags.
2. The reflection agent takes each flag to the memory agent; unexplained flags enter the question log.
3. The research run fires: the hypothesis agent reads the log, generates the batch, the supervisor loop processes each hypothesis, experiments run, the retest cascade finishes.
4. The reflection agent asks about its flags again and writes the fresh conclusions into the checklist.
5. The daily and intraday teams plan the following week with the updated checklist and the updated memory.
