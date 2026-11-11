# Experience memory — confirmed decisions

Running record of what has been discussed and confirmed, with the reason each choice was made, so settled points do not get reopened.

## The structure (built)

1. **Four reasoning-node kinds plus `HypothesisNode` in `memory.py` (built):** `FactNode`, `AssumptionNode` (a common-knowledge claim taken as given — the last resort for what neither a lookup nor our data can settle), `EmpiricalNode`, `InferenceNode`, and `HypothesisNode`. The memory is deterministic data only — no LLM calls, no embeddings inside the structure. All judgment (classifying, scoring, concluding) happens in the agents, which write plain values into the nodes. Reason: deterministic mechanics make every cascade reproducible and debuggable.

2. **EmpiricalNode fields (built):** id, question (what we're trying to find out), prediction (the predicted answer), verified (has a test run?), conclusion (True / False / None where None = tested but inconclusive), experiment (the current test write-up; a retest overwrites it). There is no confidence score; the experiment's statistics (effect, confidence interval, p-value) carry the uncertainty. The experiment is framed around the prediction, and the node's conclusion says whether the prediction held. Reason: question/prediction is what the agents naturally write and what the user can read; the earlier structured claim (subject/direction/object/conditions) was retired for being illegible.

3. **Each experiment write-up is an ExperimentRecord (built):** assumptions (a dict: node_id → the verdict assumed at test time — the machine-readable part the retest cascade checks — plus optional prose notes), method, setup, procedure, metrics, success criteria, results, conclusion, limitations. The assumptions dict is **stamped by the supervisor at dispatch time** from the node's parents and their verdicts; the researcher works under it, not on it. A `None` value is meaningful: "this test ran under do-not-assume-X" (a parent unresolved at dispatch). Where a parent is an inference node, the offered context is an LLM synthesis of the fact behind it plus the reasoning — but the recorded assumption keys point at the empirical facts, never at inference nodes. The write-up's conclusion must agree with the recorded verdict; `record_result` refuses a contradiction. Each node holds one record — the newest test; a retest replaces it. If the old result matters, the researcher mentions it inside the new record's results text.

4. **A step's outcome is genuinely three-way:** concluded true, concluded false, or inconclusive. Statistics forces this — a test can fail to decide (fail to reject ≠ false). Concluded-false is symmetric with concluded-true: both are usable facts. Inconclusive is the odd one out: it is the absence of information.

5. **HypothesisNode fields (built):** id, statement (short, condensed), nodes (its terminals — the steps its verdict is read from; each is one independent route of argument), upstream (its own private wiring: node → parents, valid only inside this hypothesis), and a conclusion (True/False/None). Empirical nodes are shared facts; how they are wired is local to each hypothesis, so the same fact can argue differently in different hypotheses.

6. **Duplicate detection is agent-side and compares questions only.** With free-text questions there is nothing to exact-match on, so spotting "this question already has a node" is an agent judgment call — the dedup funnel sketched in the still-open section is the mechanism. Rules of the comparison:
   - Only the questions are compared; a node's experiment and verdict play no part in deciding sameness. A matched node's answer is simply its live verdict.
   - A question conditioned differently ("does E hold *when J happens*?") is a different question — no collapse, own node, own test.
   - When a near-match turns out to be the *opposite* of an already-tested node, it can be settled for free where logic allows: a True result makes its opposite False. Contraries ("increases" vs "decreases") only settle one way — the tested one being True makes the other False, but the tested one being False leaves the other open. The free settlement is written as an experiment record noting it was derived, not tested.
   - Two believed nodes that cannot both be true are a contradiction flag: retesting both becomes a seed (rule 21).

## Truth and verdicts

7. **A step's truth is its own; a chain's support is a different thing.** Empirical steps are standalone claims about the world — a false step upstream does not make downstream steps false, it only breaks that argument. Chains are proofs; hypotheses are theorems; a wrong lemma invalidates the proof, never the theorem, and a theorem can be re-proven by another route (a new chain).

8. **Inference steps are judged, not tested, and judge per target (built).** An inference node holds a statement and a verdicts map — what the argument implies for each downstream node (or hypothesis) separately, since one argument can support one target and refute another. The supervisor's LLM writes the verdicts after reading the upstream nodes' questions, predictions and conclusions plus the statement (`record_inference`, the only door for judgments — mirror of `record_result`). Because a judge reasons, not gates, an argument can build on a *False* fact ("rain does not raise walk-ins, so promos matter more") — concluded-false is fully symmetric with concluded-true. When anything upstream changes, the memory wipes the inference's verdicts to stale and the supervisor re-judges; the memory itself never computes a judgment.

9. **A hypothesis is read off its terminals (built: `check`).** An empirical terminal contributes its own tested verdict; an inference terminal contributes what it was judged to imply *for this hypothesis* (its verdicts entry for the hypothesis id — unjudged or stale reads None). Any terminal False → False; all True → True; else None. False beats inconclusive because a positive disproof is information and a gap is not.

10. **A broken argument reads None, never False.** When upstream changes, an inference's verdicts are wiped to stale, so the hypothesis drops to None until re-judged — absence of proof is not proof of absence. Only a terminal that positively reads False (a tested-False fact, or an argument judged to refute the hypothesis) makes the hypothesis False.

11. **A verdict moves only through a researcher's experiment.** Supporting days and correct predictions never change a conclusion on their own; at most they become seeds for a proper retest.

12. **Testing continues downstream no matter what.** Every downstream empirical step still gets its experiment; only the context changes: upstream concluded true → "assume X true"; concluded false → "assume X false" (use the negation explicitly); inconclusive → "do not assume anything about X". A *conclusive* result earned under "do not assume X" stays valid if X resolves later; an inconclusive one gets retested (rule 29).

13. **Backtracking (downstream result proving an upstream step) is allowed only through a link explicitly marked biconditional** ("if and only if" — each side implies the other). One-way links never backtrack: B being true says nothing about A when the link is only A → B. Softer evidence can at most prompt a retest, never move confidence directly. Biconditional is a property of the edge between steps, not of a step.

14. **Invalidate, don't delete.** Broken chains and false steps stay in the memory. Reason: they stop the same dead argument being rebuilt, and a false step's negation is a usable fact. (Experiment write-ups are the one exception: a retest overwrites the old write-up — rule 3.)

## The hypothesis agent ↔ supervisor loop

15. **The loop:** the hypothesis agent proposes a statement from a seed; the supervisor asks for the step-by-step reasoning, then classifies every step as fact, assumption, empirical, or inference. A step it cannot classify — including one that blends a real-world fact with a venue claim — bounces back for further breakdown. The graph's structure is then checked by code (`validate_structure`): facts and assumptions take no parents, an inference needs at least one parent, exactly one step is the hypothesis itself — terminal, with nothing building on it — and every other step must feed a later one; violations bounce back in one batch. The hypothesis agent is told to keep real-world knowledge as its own separate steps when generating.

16. **Facts and empirical claims are separated at classification.** A fact step ("was there a public event nearby that day?") is settled by an internet lookup — our own counters can never capture such facts; its node holds question, answer, and sources, with no prediction, since a fact has a right answer, not a guess. An empirical step ("rain reduces foot traffic") is phrased as a question plus a predicted answer and is never taken from the internet — it must be tested on our own data. Facts run through the dedup funnel like everything else but skip the data-availability gate: the lookup is the data. Reason: a fact and a claim are two different things; the internet knows the world in general, not our venue.

17. **Data-availability gate.** For each claim-type step the supervisor checks whether enough data exists to test it. Reason: this forces hypotheses to start simple automatically — early on, only claims the short history can distinguish get through. The gate also makes inconclusive results rare: a well-powered test that finds nothing is real evidence of absence.

18. **Untestable steps trigger a repair, not a restart.** The supervisor names the broken step and the still-testable prefix (A→B holds; reroute from B); the hypothesis agent revises in the same conversation. Reason: backtracking from the last valid node is cheaper than regenerating and steers away from re-proposing the same dead chain.

19. **One shared bounce cap of 5** across breakdown and reroute bounces. When spent, the hypothesis is discarded entirely, no trace. Reason: many bounces of either kind signal the same thing — weak generation.

20. **Everything that survives the loop enters the memory,** including hypotheses that end unresolved, so downstream agents can see the candidate pool. Discards never enter.

21. **Where hypotheses come from: the memory agent's unanswered-question log is the only seed source** (rule 32). Refinement is not a seed — it happens inside the supervisor loop (rule 18). Retests are not seeds — the cascade triggers them (rule 29). The hypothesis agent works from the log and the graph, never raw data; only researchers touch raw data.

22. **The verdict is derived by code, never by the model.** The researcher designs the experiment form; the experiment runs as a deterministic query whose statistics (effect, confidence interval, p-value) decide the verdict via `derive_verdict` — the interval clear of zero in the predicted direction → True, clear against it → False, straddling → inconclusive. A separate model call then only writes the interpretation (reason and limitations). Reason: the result cannot be bent toward a desired answer.

## Operation

23. **Per run, the hypothesis agent reads the whole question log and generates the full batch of hypotheses upfront, in no particular order; the loop processes them one at a time.** The pass runs after close, on a flag (`--analysis off / nightly / weekly`; weekly = after every 7th day and the last). The memory is frozen between runs.

24. **This is the single memory.** The old temporal knowledge graph is retired; `memory.py` holds only this structure, persisted to disk so it endures across runs. Reason: one memory is easier to debug than two stores that can diverge.

## Retrieval by audience

25. **Two views.** Outside the research team, agents see only the abstract view: statement, conclusion, and the measured range where one exists. The research team sees everything — chains, per-step conclusions, evidence. If an outside agent asks "why", a research-team agent answers by translating the chain into a sentence.

## Late confirmations

26. **The hypothesis depends only on its terminals.** Mid-chain steps never set the hypothesis directly; they matter only by feeding the judgments and tests of the terminals (rules 8 and 9).

27. **No temporal rot mechanism.** Conclusions are anchored to evidence, and evidence does not expire. If the venue drifts, it surfaces as fresh anomalies — the top seed priority — which lead to retests with new evidence.

28. **The `ExperienceMemory` container (built):** a shared pool of reasoning nodes; each hypothesis wires its own graph over them (`upstream`: node → its parents, local to that hypothesis). `add_fact` / `add_empirical` / `add_inference` / `add_hypothesis` create nodes; `connect` wires an edge inside one hypothesis (rejects cycles; any node may have any number of parents; fact nodes take no parents — they are ground truth); `connect_to_hypothesis` marks a node as one of a hypothesis's terminals (a fact terminal reads None — a bare fact supports a hypothesis only through an inference step); `record_fact` (the only door for lookup answers; refuses non-fact nodes) writes a fact's answer and sources and stale-wipes downstream inference judgments; `record_result` (the only door for test outcomes; refuses fact and inference nodes, refuses a write-up that contradicts the verdict) writes the experiment and stale-wipes downstream inference judgments in every hypothesis using the node; `record_inference` (the only door for judgments; refuses empirical nodes) writes an inference's per-target verdicts and stale-wipes below it; `check` reads a hypothesis's verdict off its terminals; `retest_candidates` names the next retest wave (rule 29); `find_cycles` catches cross-hypothesis wiring loops (rule 31); atomic JSON save/load. Self-check at the bottom of the file; run `python memory.py` to verify the rules still hold.

## Retest cascade

29. **Automatic retest cascade (built: `retest_candidates`).** The memory is the live belief store other agents query, so a broken premise cannot wait for a future run. When a node's verdict changes, the affected empirical steps are found and retested automatically, in the same run, under the updated assumptions:
   - Dependence follows the **write-ups, not the chain arrows**: a test depends only on the verdicts its experiment record actually assumed. Chains are arguments; results are facts that carry their own dependencies.
   - Two triggers, both read purely off the write-up's assumptions dict (no graph walking at all). **Broken premise:** the write-up assumed True or False for the changed node and its current verdict no longer matches — retest. **Unlock:** the write-up says None for the changed node (ran under do-not-assume), the changed node is now conclusive, and the node itself came out inconclusive — retest, the missing knowledge might be what its test lacked. A *conclusive* result earned under do-not-assume stays valid (rule 12).
   - `retest_candidates(changed_id)` in `memory.py` returns **one wave only** — just the nodes triggered by this change. The supervisor runs those retests, and only nodes whose verdicts actually changed trigger the next wave. The cascade stops when a wave changes nothing.
   - Retests are real researcher experiments; the trigger is automatic, but the scoring still comes only from an experiment (rule 11 intact).
   - Until its retest lands, a node keeps its old verdict; the cascade finishes within the run.

## Wiring and assumptions

30. **Cross-hypothesis loops are caught before testing (built: `find_cycles`).** Hypotheses never connect to each other, but they share fact nodes, so each hypothesis can be loop-free while the union of all wiring holds a circle (A→B in one, B→A in another) — two facts propping each other up with no experiment standing on the ground. After wiring and dedup, before testing begins, the supervisor runs `find_cycles`, which returns every node sitting on a loop in the union graph; any loop goes back to the supervisor to debug its own hypothesis construction.

31. **Edges into an empirical node declare the assumptions offered to its test (built).** An empirical node can have any number of parents; wiring A and D into E means E's researcher is told to take A's and D's verdicts as given. Which assumptions a step needs is decided at wiring time, per node — a node only summarises its own upstream for a child if the child's edges say so. The offer and the record stay separate: the wiring is what the supervisor hands the researcher; the write-up's assumptions dict records what the test actually leaned on, and the retest cascade follows the write-up (rule 29).

## The analysis team (agent layer)

32. **The memory agent is the only door to the memory for agents outside the research team.** It answers in plain prose (it is rule 25's translator), in three modes: a proven match → the verdict; a match on a tested-but-unsettled hypothesis → a walk of the chain saying which steps are proven true, proven false, and still open; no match → "no evidence yet". Every question it cannot fully answer goes into the unanswered-question log, tagged "new gap" (no matching node) or "urgency bump" (matches an inconclusive node).

33. **The weekly pass runs in order:** scanner → reflection turns flags into questions → research run (batch generation, supervisor loop, retest cascade) → reflection turns fresh conclusions into checklist entries. The daily and intraday agents plan the following week with the updated checklist, consulting the memory agent as they go.

34. **The scanner is deterministic code, no LLM.** Recomputed over the full history every week on plan-vs-actual gaps: bias by weekday, bias by time-of-day, slow drift, and any single day far outside normal. Each flag carries its numbers. A flag is material for a question, never a belief.

35. **The reflection agent is stateless.** Before the run it takes each scanner flag to the memory agent and logs the unexplained ones. After the run it asks again and turns each newly concluded answer into a one-line checklist entry; a proven-false conclusion can retire an entry; near-duplicate entries merge. Inconclusive results produce no entry.

36. **The checklist holds habits, never facts.** One line per habit about what to check or ask before planning ("ask if anything special is happening tomorrow"). Venue facts live only in the experience memory. The checklist may start with a few hand-written lines on day one.

37. **Two-track closing for questions that stay unanswered.** (a) The hypothesis never became testable (bounce cap spent): discarded without trace, question cleared from the log; a real pattern re-flags in a later week with more data behind it. (b) Tested but inconclusive: the whole chain stays in memory; a repeat flag lands as an urgency bump, and the data gate (rule 17) decides whether a retest is worth spending.

38. **The daily, intraday, and reflection agents keep no private memories.** All state lives in shared places: the experience memory, the question log, the checklist, and the saved plans/notes.

## The daily and intraday layer

39. **The daily agent reads four things at planning time, and nothing else:** the baseline, the checklist, the memory agent, and today's notes. It never reads past days' raw plans or data; the past reaches it only through the baseline and the memory agent. The baseline is plain code, no LLM — recomputed from full history each morning into the typical settings for this weekday, handed over as a starting table (exact statistic to be specced with the scanner statistics). Reason: the agent starts from a computed summary of history, so a single odd day cannot spook it, and plan-vs-actual residuals stay clean for the scanner.

40. **The daily agent's first step for every note is asking the memory agent what is known about that kind of event.** Questions the memory agent cannot answer land in the question log. Reason: guidance comes from tested knowledge, not general LLM knowledge, and the first unexplained owner announcement automatically seeds the research that answers the next one.

41. **Notes are owner-written and time-windowed. Two kinds:** fact notes (window + what is happening; the agent decides the settings) and override notes (window + exact settings + reason; copied into the plan verbatim). Settings numbers enter a note from the owner's hand only. A message about a future day joins the note pile for the next morning; a message about later today wakes the intraday agent. Reason: numbers reaching a plan are either computed from history (baseline), decided by the planner, or fixed by the owner — never left behind by another agent untested.

42. **The daily agent's output is the settings list plus side notes** — `{from, to, settings, reasons}` blocks explaining each stretch. The settings list is pushed into the mainframe and the day runs on it. Reason: the reasons let the intraday agent judge when a block's premise has collapsed and a rewrite is worth waking for.

43. **The intraday agent is event-driven and asleep between events.** Woken by exactly two events: a new 2-hour weather forecast, or an owner message about later today. On waking it reads the event and the daily notes, rewrites the rest of the day's settings list, pushes it, and sleeps. Sensor readings are recorded all day and reviewed by the scanner at the weekly pass; an unannounced mid-day deviation surfaces as a single-day-outlier flag and gets investigated there. A room-drift wake trigger is a future extension, as is an ingestion agent routing live owner messages and reservations.

44. **The dedup funnel.** Sim search the new question against all existing node questions, take the top 5 candidates, then one LLM call judges each candidate: same, opposite, or different — a different condition means different, undecidable means different. Same → reuse the node and its live verdict, no retest. Opposite → settle for free where rule 6 allows. Different → new node. In the supervisor loop the supervisor is the judge; the memory agent runs the same funnel as its own judge to pick its answer mode and tag the question log.

45. **The data handler agent is the single door to stored history.** One SQLite table, `readings` — one row per played minute, every finished day appended below the last, enriched deterministically with date, weekday, and clock time before insert. Three jobs: ingest (pure code, the only write path), inventory (pure code — days so far, days per weekday, settings ranges — handed to the data gate as its data description), and serving queries (the one LLM job: a plain-words request is translated to one read-only SELECT and returned as a pandas DataFrame). Reason: LLMs translate intent, code touches data; the read-only guard means no agent can corrupt history by phrasing a request badly.
