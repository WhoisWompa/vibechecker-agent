"""A seeded experience memory for the demo, written with Lionel's memory.py API.

Until the analysis team produces real verdicts, this file gives the memory
door something to answer from: twelve made-up facts about this cafe, each an
EmpiricalNode with a question, a prediction, a verdict (True / False / None =
tested but not settled) and an experiment write-up. It is also the contract
for Lionel: a memory saved by ``ExperienceMemory.save`` with EmpiricalNodes
carrying ``question``, ``prediction`` and ``conclusion`` is all
the navigation team reads.

Every question here is word for word the question the navigation team asks
(``questions.py`` and ``checklist.py``), so the memory door answers it itself,
without a model. The numbers are invented for the demo (Ashley, 6 Sep 2026).

    uv run python agent/Agent_architecture/navigation_team/seed_memory.py state_nav/demo/memory.json
"""

from __future__ import annotations

import sys
from pathlib import Path

_ARCH = Path(__file__).resolve().parents[1]
if str(_ARCH) not in sys.path:
    sys.path.insert(0, str(_ARCH))

from memory import ExperienceMemory, ExperimentRecord  # noqa: E402

# (question, prediction, hypothesis, confidence, conclusion, experiment write-up)
# confidence is kept in the tuples for the record but no longer stored: Lionel's memory
# (7 Sep 2026) dropped it in favour of the experiment's statistics.
FACTS = [
    ("what should be done about a big group booked in this cafe?",
     "for a booked group, turning the music down 5 dB and the air 1 C cooler from 20 minutes before "
     "they arrive keeps the group and the other guests longer",
     "a booked big group stays longer with quieter music and slightly cooler air", 0.8, True,
     dict(method="before-after on the cafe's own record",
          setup="four Friday afternoons with a booked group of 10+ vs four without",
          procedure="compared mean stay of all guests 15:00-17:00, music -5 dB and air -1 C applied from 14:40",
          metrics="mean stay (minutes), departures per occupant-minute",
          success_criteria="stay at least 10% longer with the adjustment, t-test p < 0.05",
          results="stay 31 min vs 26 min, p = 0.03",
          limitations="four days each side; one group had a birthday cake, which may have held them")),
    ("what do people want from this room when it rains?",
     "when the 2-hour forecast turns to rain, guests stay longer if the room is a touch warmer (+0.5 C) "
     "and brighter (+50 lux); cooling further makes them leave",
     "a warmer, brighter room during rain keeps people longer", 0.7, True,
     dict(method="A-B across rainy afternoons", setup="six rainy afternoons, alternating the adjustment on and off",
          procedure="from the first rain forecast, +0.5 C and +50 lux on 'on' days; baseline on 'off' days",
          metrics="mean stay, entered per hour", success_criteria="stay longer on 'on' days, t-test p < 0.05",
          results="stay 29 vs 24 min, p = 0.04; entered unchanged", limitations="six days; heavy-rain days only twice")),
    ("what works in this room on a very hot day?",
     "on days over 33 C outside, one notch more fan and 0.5 C cooler keeps people longer",
     "more air and slightly cooler settings help on very hot days", 0.5, None,
     dict(method="before-after", setup="two hot days only", procedure="fan +1 and -0.5 C from open",
          metrics="mean stay", success_criteria="stay 10% longer", results="27 vs 26 min, not significant",
          limitations="two days is too few; retest when more hot days are recorded")),
    ("what should be done when the cafe closes early?",
     "dimming by 150 lux and turning the music down 4 dB from 45 minutes before last order lets guests finish "
     "naturally; nobody has to be asked to leave and the last hour's stay is unchanged",
     "an early wind-down of light and music empties the room gently before an early close", 0.75, True,
     dict(method="before-after on early-close days", setup="five early-close evenings with the wind-down vs four without",
          procedure="-150 lux and -4 dB from 45 minutes before last order", metrics="guests still seated at close, mean stay of the last hour",
          success_criteria="fewer than 3 guests seated at close, last-hour stay not shorter", results="1.4 vs 5.5 seated at close; stay 22 vs 23 min",
          limitations="early closes are rare; five evenings")),
    ("what should be done when there is an event in this cafe?",
     "turning the music up 4 dB and the lights down 100 lux during a live set was tested and made guests leave "
     "faster; during an event leave volume and light at the baseline and only add one notch of fan for the crowd",
     "a louder, dimmer room during a live event keeps the audience longer", 0.7, False,
     dict(method="A-B across event evenings", setup="four live-music evenings with +4 dB / -100 lux vs four at baseline",
          procedure="settings switched from the set's start", metrics="departures per occupant-minute during the set, mean stay",
          success_criteria="departures lower on adjusted evenings", results="departures 0.021 vs 0.014 per occupant-minute, worse; stay 24 vs 30 min",
          limitations="different acts each night; the crowd noise itself may have driven departures")),
    ("what should be done during maintenance or a delivery in this cafe?",
     "one notch more fan during a delivery hour (door open, CO2 and dust up) with lights unchanged; the effect on stay is unclear",
     "extra air during a delivery hour keeps guests as long as a normal hour", 0.5, None,
     dict(method="before-after", setup="three delivery mornings with fan +1 vs three without",
          procedure="fan +1 for the delivery hour", metrics="CO2 peak, mean stay in that hour",
          success_criteria="CO2 peak lower and stay unchanged", results="CO2 peak 740 vs 810 ppm; stay 19 vs 21 min, not significant",
          limitations="three mornings; deliveries vary in length")),
    ("what should be done when children or students fill this cafe?",
     "on school-holiday afternoons, 100 lux brighter and 0.5 C cooler lengthens stays; the room is noisier anyway, "
     "so the music can come down 3 dB without anyone noticing",
     "a brighter, cooler, slightly quieter room suits a room full of children or students", 0.7, True,
     dict(method="A-B across school-holiday afternoons", setup="six afternoons, alternating the adjustment",
          procedure="+100 lux, -0.5 C, -3 dB from 14:00 on 'on' days", metrics="mean stay 14:00-17:00, CO2 peak",
          success_criteria="stay longer on 'on' days", results="stay 33 vs 28 min, p = 0.04", limitations="one school holiday period only")),
    ("what settings suit this room on a quiet day?",
     "dimming below 350 lux on a quiet afternoon shortens the stays of the few guests who are there; on a quiet day "
     "keep the light at the baseline and save energy on the fan instead",
     "a dimmer room on a quiet day is cosier and keeps the few guests longer", 0.75, False,
     dict(method="A-B across quiet afternoons", setup="eight afternoons under 8 seated, alternating -200 lux and baseline",
          procedure="lights -200 lux from 14:00 on 'dim' days", metrics="mean stay 14:00-17:00",
          success_criteria="stay longer on dim days", results="stay 18 vs 24 min, dim days worse, p = 0.02",
          limitations="quiet afternoons cluster in the same month")),
    ("what should be done on a promotion day in this cafe?",
     "on a promotion day the room fills faster than the fan clears CO2; fan +1 from open keeps CO2 under the "
     "1000 ppm limit and stays are unchanged",
     "extra air from open keeps a promotion-day crowd comfortable", 0.8, True,
     dict(method="before-after on promotion days", setup="three promotion days with fan +1 from open vs two without",
          procedure="fan +1 from 08:00", metrics="minutes over 1000 ppm CO2, mean stay",
          success_criteria="no minute over 1000 ppm, stay unchanged", results="0 vs 76 minutes over the limit; stay 25 vs 24 min",
          limitations="five days; promotions differ in pull")),
    ("what settings suit a Friday in this cafe?",
     "Fridays run warmer and louder after 6pm; a setpoint 0.5 C lower from 17:00 keeps the evening crowd longer, "
     "the rest of the day is a normal weekday",
     "a slightly cooler Friday evening keeps the after-work crowd longer", 0.7, True,
     dict(method="before-after across Fridays", setup="four Fridays with -0.5 C from 17:00 vs four without",
          procedure="setpoint -0.5 C from 17:00", metrics="mean stay 18:00-21:00",
          success_criteria="stay longer", results="stay 34 vs 29 min, p = 0.04", limitations="eight Fridays; one had a public holiday eve")),
    ("what settings suit this cafe on a public holiday?",
     "on a public holiday the lunch crowd arrives about an hour later than on a weekday; moving the lunch block "
     "to 13:00-15:00 may fit better, not yet settled",
     "a public holiday's lunch rush comes an hour later than a weekday's", 0.5, None,
     dict(method="record comparison", setup="three public holidays vs weekday average",
          procedure="compared arrivals per hour", metrics="hour of peak arrivals",
          success_criteria="peak at 13:00 or later on all three", results="peaks at 13:00, 13:00 and 12:00",
          limitations="three holidays; no settings change tested yet")),
    ("what settings work on a fair & warm day?",
     "on a fair and warm day the baseline settings hold; no adjustment tested has beaten them",
     "the baseline is right for a fair and warm day", 0.6, True,
     dict(method="A-B across fair days", setup="ten fair, warm days alternating baseline and -0.5 C",
          procedure="-0.5 C on alternate days", metrics="mean stay", success_criteria="the adjusted days beat the baseline",
          results="stay 26 vs 26 min, no difference", limitations="only one alternative tried")),
]


def seed(path: str) -> ExperienceMemory:
    mem = ExperienceMemory()
    for question, prediction, hypothesis, confidence, conclusion, record in FACTS:
        r = mem.add_empirical(question, prediction)
        h = mem.add_hypothesis(hypothesis)
        mem.connect_to_hypothesis(r, h)
        mem.record_result(r, conclusion, ExperimentRecord(**record))
        mem.check(h)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    mem.save(path)
    return mem


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "state_nav/demo/memory.json"
    m = seed(out)
    print(f"seeded {len(m.reasoning)} nodes, {len(m.hypotheses)} hypotheses -> {out}")
