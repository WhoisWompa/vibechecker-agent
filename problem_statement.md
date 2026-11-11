**Problem Statement**
Design for a World in Transformation
- Change is everywhere - in how we live, learn and relate to one another. Transformation takes time, effort and the right support at the right moment.

This is Your chance to build something that helps. We envision a solution that *plans, acts and adapts over time.*

Your team will choose the problem and decide who it serves. You will design a solution that *thinks ahead, take action, and leaves people genuinely better off*.

---

# Our Problem

## Who it serves
Owners who operate a **shared space** — cafe, co-working floor, library, company workspace. People other than the owner come in to do something: work, study, meet, rest, eat.

## The problem
A space's physical environment is not neutral. Lighting, temperature, sound and ventilation measurably shape how people behave inside it and how they feel — whether they can concentrate, how long they stay, whether they enjoy being there.

But in nearly every shared space, that environment is **static**. Thermostat set once. Lights set once. Playlist set once. Chosen by a human, by intuition, at one moment, then left alone through the entire day, dead mid-afternoon and a packed lunch rush alike.

Doing it properly is simply out of reach right now:
- **Constant supervision.** The ideal setting shifts from one time step to another, with occupancy, weather, time of day, and what people came to do. Nobody stands at the thermostat all day reacting to that.
- **Data they don't have.** Knowing whether a change *helped* means measuring what happened afterwards and tying it back to the change. No instrumentation, no feedback loop.
- **Expertise they were never meant to have.** They run a business; they aren't environmental psychologists.

So the environment sits at a fixed compromise that is not optimal at any particular moment, leaving everyone in it slightly short of what the space could do for them.

## The opportunity
An agent does what the owner can't: watch continuously, act incrementally, measure the result, improve.

## What we're building
An agent that continuously and adaptively tunes the ambient environment settings to its optimal state so occupants can perform their task to the highest standard, or enjoy themselves, depending on what the space is for.

**Sense** → live sensor state (occupancy, temperature, light, noise, dwell time, arrival/departure rate)
**Plan** → reason about current state against the owner's declared objective, decide next settings
**Act** → apply to actuators (lighting, HVAC, sound, ventilation)
**Adapt** → observe the effect on the outcome, carry it forward; converge on what works for *this* room, *this* crowd, *this* hour

### The objective is an input, not a constant
Engine is venue-agnostic; the declared goal differs per deployment.

| Venue | What it's for | Optimises toward |
|---|---|---|
| Cafe | Eating, meeting, short work | *(open — see Q1)* |
| Co-working | Focused work | Sustained concentration, member satisfaction |
| Library | Study | Conduciveness to sustained study |
| Company workspace | Team output | Alertness, collaboration quality |

Same sensors, same actuators, same engine — different learned policy.

## Hackathon scope
One venue: **a cafe**. **Simulated**, no real hardware — the simulation supplies the sensor stream and models occupant response. Demo shows the agent starting from a naive fixed setting and converging over simulated time on settings that measurably improve the outcome.


