# Vibechecker

## What it is

Vibechecker is an agentic workflow that tunes the physical environment of a shared space.

It watches the space through sensors, reasons about what the room should feel like right now, sets the room's controls, watches what happens next, and gets better at it over days.

```
Sense  ->  read the live sensor stream
Plan   ->  reason about the current state against the owner's goal, decide the settings
Act    ->  apply the settings to the room's controls
Adapt  ->  observe the effect, carry it forward, converge on what works for this room
```

## The objective

The objective is an input, and it is open ended.

At run time the owner states, in their own words, what they want the space to do. It could be anything — keep people around longer, keep the room turning over, make it a nice place to sit, something else entirely. Vibechecker reads that statement, works out what it implies about the room, and drives the settings toward it.

There is no built-in score and no fixed list of goals. Vibechecker is not told which outcomes matter until the owner says so.

## The venue

- A cafe. 30 seats. Open 8am to 10pm.
- One timestep is 1 minute. `t = 0` is 8am, so a full day is 840 timesteps.
- The environment is simulated for now. There is no real hardware.
