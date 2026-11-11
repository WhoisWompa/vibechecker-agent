# Vibechecker — how everything works

A detailed walkthrough of the whole system: the venue, the two teams, the memory, and
how a piece of knowledge travels from a question to a habit.

---

## 1 · What the system is

Vibechecker tunes the physical environment of a shared space — a cafe. It reads the room
through sensors, decides what the settings should be, applies them, watches what happens,
and gets better at it over weeks.

```
Sense  ->  read the live sensor stream
Plan   ->  reason about the room against the owner's goal, decide the settings
Act    ->  apply the settings to the room's controls
Adapt  ->  observe the effect, carry it forward, converge on what works for this room
```

The objective is an **input**, not a built-in score. At run time the owner says in their
own words what they want the space to do — keep people around longer, keep tables turning,
make it a nice place to sit. Vibechecker works out what that implies for the room and
drives the settings toward it. Nothing is hard-coded about which outcomes matter.

### The venue

- A cafe. 30 seats. Open 8am to 10pm.
- One timestep is one minute. `t = 0` is 8am, so a day is 840 timesteps.
- The environment is simulated for now; no real hardware.

### What the system controls

| Setting | Meaning | Range |
|---|---|---|
| `light` | light level | lux |
| `temperature` | thermostat setpoint | °C |
| `volume` | music volume | dB |
| `fan` | ventilation fan speed | 0 (off) – 5 (max) |

### What comes back

Sensors: room temperature, humidity, CO₂, noise, measured light (outside light shines in,
so this differs from the set light), outside temperature.
Crowd: people who entered that minute, people who left, current occupancy.

A setting takes one minute to act on the room — minute `t`'s readings show what minute
`t−1`'s settings produced.

Every recorded row also carries **intent**, so "22°C because planned" can be told apart
from "22°C because rain fired": which plan block was in force, that block's reason in
words, whether it was set by the morning plan or an intraday adjustment, and which 2-hour
stretch of the day it falls in (0 = 08–10 through 6 = 20–22).

---

## 2 · The shape of the architecture

Three parts, and the middle one is the only thing the other two share.

```
  NAVIGATION TEAM                 MEMORY                  ANALYSIS TEAM
  (day clock)                                             (week clock)

  daily agent  ──── asks ────>  memory agent  <──── asks ──── reflection agent
  intraday agent                       │                              │
        ▲                              │ logs unanswerable            │ questions
        │                              ▼                              ▼
        │                        question bank ─────────────>  research run
        │                                                             │
        └────────── checklist  <──── one-line habits ─────────────────┘
```

Two teams, two clocks, one memory. The navigation team runs on the day clock; the analysis
team runs on the week clock. **They never call each other.** Navigation puts questions into
the memory; analysis puts answers back. That's the entire coupling.

### Runs once, at setup: the goal team

The owner's stated goal goes in. Out comes a clear objective plus a set of concrete metrics
that can be used to check whether things are improving toward it. It never runs again —
the objective is a fixed input, and all learning happens underneath it.

---

## 3 · The navigation team (day clock)

Runs the cafe day to day. Two agents, one door to memory, one master record.

### 3.1 The daily agent

A LangGraph, four nodes: `gather → ask_memory → plan → write`.

**gather (code, no model)** collects exactly four things:

1. **The baseline** — plain code. Medians from history for this weekday, stretch by
   stretch; or, with no history yet, the seed room adjusted by the forecast. This is the
   starting table the planner works from.
2. **The checklist** — standing one-line habits plus today's items. Habits only, never
   facts ("ask whether anything special is happening tomorrow").
3. **The owner's notes** for today, one file per date.
4. Plus the weekday, the calendar, and the 24-hour forecast.

Crucially, it never reads past days' plans or raw data. The past reaches it **only**
through the baseline and the memory. One odd day therefore cannot move the plan, and
plan-vs-actual residuals stay clean for the scanner.

**ask_memory (code)** takes every checklist item that carries a question through the memory
door. Anything memory can't answer lands in the question bank with who asked, when, and the
situation.

**plan (model — one call, plus one repair attempt on validation failure)** produces the
day's blocks: `{from, to, settings, reasons, assumes}`. The `assumes` field is the block's
premise in two words — weather fair/cloudy/rain, crowd quiet/moderate/busy — so the intraday
agent can test whether a block's premise still holds without spending a model call.

**write (code)** saves the plan as JSON and markdown. That file is what the intraday agent
reads all day.

### 3.2 Owner's notes

Owner-written, each covering a window of the day. Two kinds:

- **Fact note** — the window and what's happening ("crowd from 10am to 12pm"). The agent
  decides the settings itself.
- **Override note** — the window, the exact settings, and the reason. Copied into the plan
  verbatim; the agent plans around it.

Settings numbers enter a note from the owner's hand only. A message about a future day joins
the note pile for that morning; a message about later today wakes the intraday agent.

Notes come from the owner's **Google Calendar**, read each morning before the daily graph
runs, through the calendar MCP server — no model call. The event title is the note, start
and end are the window, an all-day event is the whole day, and a title starting with
`override` carrying `lever=value` pairs is an override note. Notes are filed once per event,
so a rerun adds nothing twice. If the calendar can't be reached, the day runs on the notes
already on file.

There's also an **owner's desk**: the owner says a sentence ("book a group of 12 next Friday
3 to 5pm"), one model call reads it into a title, date, window and — only if the owner gave
numbers — exact settings, then plain code creates that calendar event. The title is written
the way a note is written, so the next morning's read turns the event back into the same
note. It shows the event and waits for a yes before writing; it only ever creates, never
edits or deletes.

### 3.3 How questions to memory are phrased

Every question the team asks memory is about the **kind of situation**, in memory's own
words: no counts, no clock times, no dates, one relationship per question, starts with a
question word, ends with "?".

An owner's note is mapped to the question memory would hold about that kind of note — a
booking becomes *"what should be done about a big group booked in this cafe?"* An
unrecognised note keeps its words with the particulars stripped. The note itself travels
in the question bank entry's situation field, so the detail isn't lost, it's just not part
of the question.

### 3.4 The intraday agent

A LangGraph per check: `observe → detect → (hold | ask_memory → replan → push)`.

The runner pushes the plan minute by minute, ramped: 6 lux, 0.1°C, 0.5 dB per minute.
Every 15 minutes, `detect` — **plain code, no model** — compares the plan against the room.
What can fire, with its cooldown:

| Trigger | Cooldown |
|---|---|
| Forecast category changed | 60 min |
| Fullness category differs, two checks in a row, after the first half hour | 60 min |
| CO₂ over the 800 ppm target or the 1000 limit | 60 min |
| Room more than 2°C from setpoint, after the first hour | 120 min |
| Guests leaving 50% faster than the hour before, with ≥5 people around | 120 min |
| Empty room after 9pm → energy mode **by rule, no model** | once |
| An owner message | always |
| A late owner's note for today whose window is still ahead | — |

Only when something fires does the model run (`replan`, one call). A late **override** note
puts its settings into its window by rule. A late **fact** note is asked of memory, recorded
as the premise of the blocks it covers, and marks them busy if it speaks of a group or a
booking.

**The model gets only the remaining blocks and returns only the windows it changes** — a
patch, not a rewrite. Untouched stretches survive exactly as they were.

**Guardrails that hold in code whatever the model says:**

- Hard bounds: 21–26°C, 40–70 dB.
- A change cap: 100 lux, 1°C, 4 dB, 1 fan step — every lever a replan moves is held within
  that distance of what the plan already said for that time. So a replan adjusts and never
  lurches.
- The room then ramps toward the new value at 6 lux / 0.1°C / 0.5 dB per minute, so every
  change is gradual.
- Blocks the replan left alone, owner overrides, and energy mode are never touched.
- Whatever was held back is logged and written into the push note.

### 3.5 Context at every replan

Every block carries the window it was born with, its 2-hour stretch (the grid the baseline
learns on), and who made it — the morning plan, or a push at a given minute with its note.
A push that cuts a block keeps the block's birth window.

At each check the snapshot carries:

- **where** — e.g. 13:10 is 70 minutes into the 12:00–14:00 stretch with 50 minutes left,
  which block is in force, and how that block came to exist.
- **previous_stretch** — what the last completed stretch planned, what it actually ran on
  average, what the room did, who came and left, what fired, and what was pushed.

Both go into the replan prompt in plain words.

### 3.6 Caps, so nothing runs away

- Every graph is a straight line. The only retry is one repair attempt when a plan fails
  validation.
- A per-day cap on intraday model attempts (default 12), then the heuristic path.
- A whole-run budget on calls (default 80) and dollars (default 1.00), counted on attempts
  including failed ones. Once tripped, remaining calls are skipped and the run finishes on
  the heuristic path.

Three modes exist: `model` (real model calls), `heuristic` (same loop, no model), and
`fixed` (timetable only) — so the loop can be tested without spending anything.

### 3.7 What the navigation team writes

- **The master record** — one row per minute: date, weekday, month, time, settings, sensors,
  and the intent columns (which block, its reason, set by daily or intraday, which stretch).
- **The question bank** — open questions for the analysis team.
- **Token and message logs** — every model call: which agent, day, loop, purpose, the
  questions or events behind it, tokens, latency, cost, and the full prompt and reply.
- **Per-day summaries and roll-ups** — by day, agent, loop, purpose, and question.

---

## 4 · The memory

One shared store of reasoning nodes, persisted to disk so it endures across runs. It is
**deterministic data only** — no model calls and no embeddings inside the structure itself.
All judgment happens in the agents, which write plain values into the nodes. That's what
makes every cascade reproducible and debuggable.

### 4.1 The four node kinds

**Fact node** — something about the world, settled by an internet lookup. Holds a question,
an answer, and sources. **No prediction**, because a fact has a right answer, not a guess.
Takes no parents; it's ground truth.

**Empirical node** — a claim about *our* venue, tested on *our* data. Holds a question, a
prediction, a confidence, whether it's been verified, a conclusion, and the current
experiment write-up. Never taken from the internet.

**Inference node** — a piece of reasoning. Judged, never tested. Holds a statement and a
**verdicts map**: what the argument implies for each downstream target *separately*, since
one argument can support one thing and refute another.

**Hypothesis node** — holds a short statement, its **terminals** (the steps its verdict is
read off), its own **private wiring**, and a conclusion.

### 4.2 Global versus local — the key structural idea

- **Global:** fact and empirical nodes, and their verdicts. One node per question, one
  verdict, shared by every hypothesis.
- **Local:** the wiring (which node feeds which), the inference judgments, and the
  hypothesis verdict. These live inside a single hypothesis.

So the same tested claim is a shared fact, but each hypothesis can argue with it in its own
way — the same node can be a bottom-level premise in one hypothesis and a direct terminal in
another.

### 4.3 Three-way verdicts

A step's outcome is genuinely three-way: **concluded true**, **concluded false**, or
**inconclusive**. Statistics forces this — failing to reject is not the same as false.

Concluded-false is symmetric with concluded-true: both are usable facts, and an argument can
legitimately build on a false one ("rain does *not* raise walk-ins, so promos matter more").
Inconclusive is the odd one out — it's the absence of information.

### 4.4 How a hypothesis gets its verdict

Read off its terminals only, never mid-chain steps:

- Any terminal reads **False** → hypothesis False.
- All terminals read **True** → hypothesis True.
- Otherwise → **None** (open).

False beats inconclusive, because a positive disproof is information and a gap is not.

A step's truth is its own; a chain's support is a different thing. A false step upstream
doesn't make downstream steps false — it only breaks *that argument*. Chains are proofs,
hypotheses are theorems: a wrong lemma invalidates the proof, never the theorem, and the
theorem can be re-proven by another route.

**A broken argument reads None, never False.** When something upstream changes, the
inference's verdicts are wiped to stale and the hypothesis drops to open until re-judged —
absence of proof is not proof of absence.

### 4.5 The three write doors

Every write goes through exactly one door, and each door refuses the wrong node type:

- `record_fact` — writes a fact's answer and sources.
- `record_result` — writes a test outcome and its write-up. Refuses a write-up whose
  conclusion contradicts the recorded verdict.
- `record_inference` — writes an inference's per-target verdicts.

All three stale-wipe the inference judgments below them, so nothing downstream silently
keeps a stale conclusion.

### 4.6 Experiment write-ups

Each experiment record holds: assumptions, method, setup, procedure, metrics, success
criteria, results, conclusion, limitations.

The **assumptions dict** (node → the verdict assumed at test time) is stamped by the
supervisor at dispatch, from the node's parents and their verdicts. The researcher works
*under* it, not on it. A `None` value is meaningful: it means "this test ran under
do-not-assume-X".

Each node holds one record — the newest test. A retest replaces it; if the old result
matters, the researcher mentions it inside the new record's results text.

### 4.7 Confidence only moves through an experiment

Supporting days and correct predictions never nudge a number on their own. At most they
become a **seed** for a proper retest. This is what stops the belief store drifting on
anecdote.

### 4.8 Invalidate, don't delete

Broken chains and false steps stay in the memory. They stop the same dead argument being
rebuilt, and a false step's negation is a usable fact. (Experiment write-ups are the one
exception — a retest overwrites the old one.)

### 4.9 The retest cascade

The memory is the live belief store other agents query, so a broken premise can't wait for a
future run. When a verdict changes, affected tests are retested automatically, in the same
run.

Dependence follows **the write-ups, not the chain arrows** — a test depends only on the
verdicts its experiment record actually assumed. Two triggers, both read straight off the
assumptions dict with no graph walking:

- **Broken premise** — the write-up assumed True or False for the changed node, and its
  current verdict no longer matches. Retest.
- **Unlock** — the write-up says None for the changed node (it ran under do-not-assume), the
  changed node is now conclusive, and the node itself came out inconclusive. Retest, because
  the missing knowledge might be exactly what its test lacked.

A conclusive result earned under do-not-assume stays valid if that premise resolves later.

`retest_candidates(changed_id)` returns **one wave only**. The supervisor runs those
retests; only nodes whose verdicts actually changed trigger the next wave. The cascade stops
when a wave changes nothing. Retests are real experiments — the trigger is automatic, the
scoring still isn't.

### 4.10 Loop safety

Hypotheses never connect to each other, but they share fact nodes — so each hypothesis can
be loop-free while the *union* of all wiring holds a circle (A→B in one, B→A in another),
which would be two claims propping each other up with no experiment on the ground. After
wiring and dedup, before testing, `find_cycles` returns every node sitting on a loop in the
union graph. Any loop goes back to the supervisor to fix its own construction.

### 4.11 Duplicate detection — the dedup funnel

Questions are free text, so there's nothing to exact-match on. The funnel: similarity-search
the new question against all existing node questions, take the top 5 candidates, then one
model call judges each as **same**, **opposite**, or **different**.

- **Same** → reuse the node and its live verdict. No retest.
- **Opposite** → settle for free where logic allows. A True result makes its opposite False.
  Contraries ("increases" vs "decreases") only settle one way: the tested one being True
  makes the other False, but the tested one being False leaves the other open. A free
  settlement is written as a record noting it was derived, not tested.
- **Different** → new node.

Only the *questions* are compared — a node's experiment and verdict play no part in deciding
sameness. A question conditioned differently ("does E hold *when J happens*?") is a
different question with its own node and its own test. Undecidable counts as different. Two
believed nodes that can't both be true raise a contradiction flag, which seeds retesting
both.

### 4.12 Who can see what

Two views:

- **Outside the research team** — statement, conclusion, confidence. Nothing else.
- **The research team** — everything: chains, per-step conclusions, evidence.

If an outside agent asks *why*, a research-team agent translates the chain into a sentence.

### 4.13 No temporal rot

Conclusions are anchored to evidence, and evidence doesn't expire. There is no decay
mechanism. If the venue genuinely drifts, it surfaces as fresh anomalies — the top seed
priority — which lead to retests with new evidence.

---

## 5 · Retrieval — the memory agent

The memory agent is the **only door** to the memory for every agent outside the research
team. It answers in plain prose — never node ids, never talk of graphs. Retrieval has two
levels.

**Level 1 — flat.** Similarity search over the pool, each fact and empirical node as a
standalone entry. The question: is any single entry enough on its own?

- A settled empirical entry (True or False) answers directly; if a measured range is
  available, that range is quoted as how sure we are.
- A fact with its answer filled in answers, and cites where the answer came from.
- If the asked question is the logical **opposite** of something already proven True, the
  opposite is False — and that counts as sufficient.
- Not sufficient: a fact not yet looked up, an empirical entry never verified, or an entry
  that only loosely resembles the question.

**Level 2 — the DAG.** If no single entry sufficed, pull every hypothesis subgraph touching
the question — full chains, facts, assumptions, tested claims and the logic links — and read
them together. Three outcomes:

- **answered** — a short plain-prose answer.
- **inconclusive** — the partial picture: which steps are proven true, which proven false,
  which still open.
- **nothing** — none of it is actually relevant.

**Anything still unanswered goes into the question log**, tagged either **new gap** (nothing
matched) or **urgency bump** (matched an inconclusive node). That log is the analysis team's
only seed source.

---

## 6 · The analysis team (week clock)

Owns the question *"what have we learned about this venue?"* It runs the weekly pass, and
answers questions from the other teams all week through the memory agent.

### 6.1 The scanner — plain code, no model

Recomputed over the full history every week, on plan-vs-actual gaps:

- bias by weekday
- bias by time of day
- slow drift
- any single day far outside normal

It prints a short list of flags with the numbers attached. **A flag is material for a
question, never a belief** — the scanner is not allowed to conclude anything.

### 6.2 The reflection agent — stateless, runs twice

**Before the run:** takes each scanner flag to the memory agent. Explained flags need
nothing. Unexplained ones enter the question log.

**After the run:** asks about its flags again. Each newly concluded answer becomes a
one-line entry in the checklist. A proven-false answer can retire an entry. Near-duplicate
entries merge. **Inconclusive results produce no entry.**

### 6.3 The research run

**Seeds.** The unanswered-question log is the *only* seed source. Refinement isn't a seed —
that happens inside the supervisor loop. Retests aren't seeds — the cascade triggers those.
The hypothesis agent works from the log and the graph, never from raw data. Only researchers
touch raw data.

**Batching.** Per run, the hypothesis agent reads the whole question log and generates the
full batch of hypotheses upfront, in no particular order. The loop then processes them one
at a time. Runs are custom-triggered, roughly weekly; the memory is frozen between runs.

**The supervisor loop, per hypothesis:**

1. The hypothesis agent proposes a statement from a seed.
2. The supervisor asks for the step-by-step reasoning.
3. The supervisor classifies every step as **fact**, **empirical**, or **inference**. A step
   it can't classify — including one that blends a real-world fact with a venue claim —
   bounces back for further breakdown. (The hypothesis agent is told upfront to keep
   real-world knowledge as its own separate steps.)
4. **The data-availability gate:** for each claim-type step, is there enough data to test it?
   Facts run through dedup like everything else but skip this gate — the lookup *is* the data.
5. **Untestable steps trigger a repair, not a restart.** The supervisor names the broken step
   and the still-testable prefix ("A→B holds; reroute from B") and the hypothesis agent
   revises in the same conversation.
6. **One shared bounce cap of 5** across breakdown and reroute bounces. When it's spent, the
   hypothesis is discarded entirely, no trace.
7. Wiring: edges into an empirical node declare the assumptions offered to its test. Wiring
   A and D into E means E's researcher is told to take A's and D's verdicts as given.
8. `find_cycles` runs after wiring and dedup, before any testing.

Everything that survives the loop enters the memory, **including hypotheses that end
unresolved**, so downstream agents can see the candidate pool. Discards never enter.

Why the gate matters so much: it forces hypotheses to start simple automatically. Early on,
only claims the short history can distinguish get through — the system earns the right to ask
harder questions as data accumulates. It also makes inconclusive results rare, which means a
well-powered test that finds nothing is real evidence of absence.

**Researchers.** They design and run the experiments. Scoring is **two separate calls**: the
first designs and runs the experiment and reports raw numbers; the second turns those fixed
numbers into a confidence with a written reason. The experiment therefore can't be bent
toward a desired score.

**Testing continues downstream no matter what** — only the context changes. Upstream
concluded true → "assume X true". Concluded false → "assume X false", using the negation
explicitly. Inconclusive → "do not assume anything about X".

### 6.4 The checklist

One file of one-line **habits**, never facts. What to check or ask before planning ("ask
whether anything special is happening tomorrow"). Read by the morning and intraday agents at
planning time. Venue facts live only in the memory. It starts with a few hand-written lines
on day one.

This is the return path, and its shape matters: knowledge comes back to navigation as a
*habit*, not as a raw fact the planner has to interpret.

### 6.5 The data handler — the single door to stored history

One SQLite table, `readings`, one row per played minute, each finished day appended below
the last, enriched deterministically with date, weekday and clock time before insert. Three
jobs:

- **Ingest** — pure code, and the only write path.
- **Inventory** — pure code. Days so far, days per weekday, settings ranges. Handed to the
  data gate as its data description.
- **Serving queries** — the one model job: a plain-words request becomes one **read-only**
  SELECT, returned as a DataFrame.

Models translate intent; code touches data. The read-only guard means no agent can corrupt
history by phrasing a request badly.

### 6.6 Questions that stay unanswered

Two tracks:

- **Never became testable** (bounce cap spent) → discarded without trace, question cleared
  from the log. If the pattern is real, the scanner flags it again in a later week with more
  data behind it.
- **Tested but inconclusive** → the whole chain stays in memory. A repeat flag lands as an
  urgency bump, and the data gate decides whether a retest is worth spending.

Flukes never repeat, so they stay closed. Real patterns keep flagging until answered.

---

## 7 · How a week flows

**Once, at setup**
The goal team turns the owner's stated goal into an objective and a set of metrics.

**Every day**
The daily agent builds the day's settings list and notes from the baseline, the checklist,
the memory agent and today's notes, then pushes the settings into the mainframe. The day runs
on that list. The intraday agent wakes only for a new 2-hour forecast or an owner message
about later today, rewrites the rest of the day's list, and sleeps again. Unanswerable
questions get logged the moment they happen. The settings list, the notes, and the day's data
are all saved.

**Once a week — the weekly pass, in order**

1. The scanner recomputes over full history and prints its flags.
2. The reflection agent takes each flag to the memory agent; unexplained flags enter the
   question log.
3. The research run fires: batch generation, the supervisor loop per hypothesis, experiments,
   then the retest cascade until a wave changes nothing.
4. The reflection agent asks about its flags again and writes fresh conclusions into the
   checklist.
5. The navigation team plans the following week with the updated checklist and updated memory.

---

## 8 · Following one question all the way round

1. **Tuesday morning.** The owner books a big group. The daily agent asks memory what's
   known about big groups in this cafe. Level 1 finds nothing sufficient; level 2 finds no
   relevant subgraph. Memory answers *no evidence yet*, and the question drops into the bank
   with who asked, when, and the situation. The agent plans the day on judgement alone.
2. **The weekend.** The scanner recomputes and notices the room ran hot on days like that.
   Reflection checks memory, still nothing, so the flag joins the same log.
3. **The research run.** The hypothesis agent picks up both entries and proposes a
   hypothesis. The supervisor breaks it into steps and classifies each one. The data gate
   confirms there's enough history now. Cycles are checked. Researchers run the experiments;
   a second call turns the raw numbers into a confidence with a written reason. It comes back
   **true**.
4. **The cascade.** Any earlier test that assumed a now-changed verdict is retested in the
   same run, wave by wave, until a wave changes nothing.
5. **Reflection, after.** The newly settled answer becomes one line in the checklist.
6. **Next Monday.** The daily agent reads that line and plans big groups differently —
   without ever knowing an experiment happened.

Two teams, two clocks, one memory. Navigation asks the questions; analysis answers them. The
memory is the only thing either side touches, which is exactly why they can run on completely
different schedules without ever coordinating.

---

## 9 · Design rules worth stating plainly

- **Code where it must be deterministic, models where judgment is needed.** The baseline,
  the scanner, the intraday detection, the ingest, the inventory, the hard bounds and the
  change cap are all plain code. Models plan, phrase questions, classify steps, judge
  inferences, design experiments and translate intent.
- **One door per boundary.** One memory door for agents, one data handler for history, three
  write doors into the memory. Each door refuses malformed input rather than accommodating it.
- **The planner never reads the past directly.** Only the baseline and the memory carry it.
- **Numbers reaching a plan have exactly three origins:** computed from history (baseline),
  decided by the planner, or fixed by the owner. Never left behind by another agent untested.
- **Confidence moves only through an experiment.** Never through a good day or a lucky guess.
- **Nothing is deleted.** False steps and broken arguments are kept, because they're
  informative and they stop dead arguments being rebuilt.
- **Every loop has a cap.** Bounce cap of 5, one repair attempt, per-day model call cap,
  whole-run call and dollar budgets, and cascade waves that stop when nothing changes.
- **No private memories.** The morning, intraday and reflection agents keep none. All state
  lives in shared places: the memory, the question log, the checklist, and the saved plans,
  notes and record.
