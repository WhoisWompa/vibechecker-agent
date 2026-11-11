# Navigation team

Runs the cafe each day. Two agents, one door to memory, one master record.

```
uv run python run_navigation.py --days 3 --seed 1 --start 2024-01-05 --mode model \
    --note "big group of 12 at 3pm-5pm" --message "t=390: a school group may come at 4pm"
uv run pytest -q          # 12 tests, no model needed
uv run python run_timeline.py state_nav/model-demo3 --open   # the run as a scrubbable timeline, one page (demo)
```

Modes: `model` (Haiku through `llm.py`), `heuristic` (same loop, no model), `fixed` (timetable only).
`--preset demo` gives a room that can fill; `--booking 420:12` makes a booked party of 12 walk in at 3pm.
Each day's summary carries `hidden_score` from the simulation's grader: for the eval table only, never shown to an agent.
The model needs `API_KEY` in `agent/.env` (Anthropic) — without it every model call fails and the
run says so, then continues on the heuristic path.

## The morning (`daily_agent/`, a LangGraph)

```
gather -> ask_memory -> plan -> write
```

- gather (code): the weekday and calendar (`sg_calendar.py`), the 24-hour forecast, the owner's
  notes for the day (`notes.py`, one file per date under `state/notes/`), the checklist
  (`checklist.py`: standing habits in `state/checklist.md` + today's items), and the baseline
  (`baseline.py`: medians from history for this weekday, or the seed room adjusted by the forecast).
- ask_memory (code): every checklist item with a question goes through the memory door
  (`memory_door.py`, wrapping Lionel's `MemoryAgent`). Unknowns land in `state/question_bank.json`
  with who asked, when, and the situation.
- plan (model, one call + one repair): the day's blocks `{from, to, settings, reasons, assumes}`.
  `assumes` is the premise of the block in two words (weather fair/cloudy/rain, crowd
  quiet/moderate/busy) so the intraday agent can check it without a model.
- write (code): `state/plans/<date>.json` and `.md`, the file the intraday agent reads.

## Questions to memory (`questions.py`, built)

Every question the team asks memory is about the kind of situation, in memory's own words: no counts,
clock times or dates, one relationship per question, starts with a question word, ends with "?". An
owner's note is mapped to the question memory would hold about that kind of note (a booking -> "what
should be done about a big group booked in this cafe?"); an unrecognised note keeps its words with the
particulars stripped. The note itself travels in the bank entry's situation. With a model, the daily
agent may instead ask up to three model-phrased questions per note; a malformed one falls back to the
template. `is_well_formed` is the check the tests apply to every banked question.

## The day (`intraday_agent/`, a LangGraph per check)

```
observe -> detect -+-> hold
                   +-> ask_memory -> replan -> push
```

The runner pushes the plan minute by minute (ramped: 6 lux, 0.1 C, 0.5 dB per minute).
Every 15 minutes `detect` compares the plan with the room. What fires, and its cooldown:
forecast category changed (60 min); fullness category differs, two checks in a row, after the
first half hour (60); CO2 over the 800 ppm target or the 1000 limit (60); room more than 2 C from
the setpoint after the first hour (120); guests leaving 50% faster than the hour before, with at
least 5 people around (120); empty room after 9pm -> energy mode by rule, no model (once); an
owner message (always); an owner's note for today that arrived after the morning plan, from the
calendar (looked at every 60 min) or the notes file (every check), if its window is still ahead
(built). Hard bounds hold in code whatever the model says: 21-26 C, 40-70 dB.
A late override note puts its settings into its window by rule; a late fact note is asked of memory,
recorded as the premise of the blocks it covers, and marks them busy when it speaks of a group or
booking (built).
Only then does the model run (`replan`, one call), or the heuristic without a model. The model
gets only the remaining blocks and returns only the windows it changes (a patch); untouched
stretches survive as they were. When memory has content, the morning's questions go through it
as one batch: one match call and one answer call for all of them, instead of two per question.

## Block context at every replan (built)

Every plan block carries the window it was born with (`origin_from`, `origin_to`), its 2-hour
`stretch` (0-6 on the 08-10 ... 20-22 grid, the grid `baseline.py` learns on) and who made it
(`origin`: the morning plan, or a push at a given minute with its note). A push that cuts a block
keeps the block's birth window; a block a push changed is stamped as that push's.
At every check the snapshot carries `where` (13:10 is 70 min into the 12:00-14:00 stretch, 50 min
left, and which block is in force and how it came to exist) and `previous_stretch` (what the last
completed stretch planned, ran on average, what the room did, who came and left, what fired and
what was pushed). Both go into the replan prompt in plain words and to the heuristic path.
A change cap in code (`CHANGE_CAP` in `blocks.py`: 100 lux, 1 C, 4 dB, 1 fan step) holds every lever
a replan moved to within that distance of what the plan already said for that time, so a replan
adjusts and never lurches; the room then ramps there at 6 lux, 0.1 C and 0.5 dB per minute, so every
change stays gradual. Blocks the replan left alone, owner overrides and the energy mode are not
touched; what was held is logged and written into the push note. Crowd, forecast, temperature-drift and
light-drift discrepancies fire at most once per stretch. The master record carries a `stretch` column.

## Caps (no runaway loops)

- Every graph is a straight line; the only retry is one repair attempt when a plan fails validation.
- `--max-model-calls` (default 12): intraday attempts per day, then heuristic.
- `--budget-calls` (default 80) and `--budget-usd` (default 1.00): hard stop for the whole run,
  counted on attempts, failed ones included. Once tripped, every remaining call is skipped, the
  run says `BUDGET STOP` and finishes on the heuristic path.

## Owner's notes from Google Calendar (`calendar_notes.py`, built)

The owner keeps one calendar for the cafe. Each morning, before the daily graph runs, the events on
that calendar for the day become owner's notes: the title is the note, start and end are the window,
an all-day event is the whole day, a title starting with `override` and carrying `lever=value` pairs is
an override note, the description is appended as detail. The calendar is read through the calendar MCP
server (`@cocal/google-calendar-mcp`) with LangChain's MCP client; no model call. Notes are filed in
`state/notes/<date>.json` with source `calendar`, once per event, so a rerun adds nothing twice. If the
calendar cannot be reached the morning log says so and the day runs on the notes on file.
`--calendar "Vibechecker owner"` (or `VIBE_CALENDAR` in `agent/.env`); one-time setup in `CALENDAR_SETUP.md`.

## The owner's desk (`owner_desk.py`, `owner_says.py` at the root, built)

The owner says a sentence ("book a group of 12 next Friday 3 to 5pm"); one model call reads it into
a title, a date, a window and, only if the owner gave numbers, exact settings. Plain code turns that
into the calendar server's create-event call on the owner's calendar, through the same MCP client the
morning read uses. The title is written the way an owner's note is written, so the next morning's read
turns the event back into the same note. The desk shows the event and waits for a yes before writing
(`--yes` skips the question, `--dry-run` never writes); it only creates, never edits or deletes.
Dates like "next Friday" resolve against `--as-of` (default today). Model: `OWNER_MODEL` in
`agent/.env`, else `DEFAULT_MODEL`.

## What is written

- `state/history.db` — the master record (`record.py` through Lionel's data handler): one row per
  minute with date, weekday, month, time, settings, sensors, and the intent: `block_from`,
  `block_reason`, `set_by` (daily or intraday).
- `state/question_bank.json` — open questions for the analysis team.
- `state/tokens.jsonl`, `state/messages.jsonl` — every model call: agent, day, loop, purpose (morning
  plan, note questions, memory match, memory answer, memory chain walk, intraday replan), the questions or
  events behind it, tokens, latency, cost, and the full prompt and reply (`tracking.py`).
- `state/days/<date>.json`, `state/summary.json` — per-day numbers and the token roll-ups: by day and
  agent, by loop, by purpose, and by question (a batched call split evenly between its questions; answers
  memory gave without a model listed as free) (built). `run_report.py` shows the same tables.

## Shared with the analysis team

`blocks.py` (block shape and validators), `memory_door.py` (calls `MemoryAgent`, never the memory
directly), `record.py` (calls `DataHandlerAgent.ingest_day`). Lionel's memory file can be passed
with `--memory path/to/memory.json`.
