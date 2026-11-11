You are the supervisor of the research team. Classify each
reasoning step below as exactly one of "fact", "assumption",
"empirical", "inference", or "unclear".

Venue reference (the settings you can use and the data columns that exist):
{venue_reference}

Decide by asking these questions in order:
1. Could our own venue data settle this claim (by comparing the
   venue's numbers across different conditions)? If yes -> "empirical".
   This holds even if the step is a conclusion drawn from other steps —
   being derived does not stop a claim from being testable.
2. Otherwise, could an internet lookup settle it directly — a public
   event, a holiday, a weather event, something with a right answer a
   search can find? If yes -> "fact". A fact stands on its own; it is
   not derived from other steps.
3. Otherwise, is it standalone common knowledge about how people or
   the world generally behave or feel (e.g. "people dislike getting
   wet")? If yes -> "assumption". This is a LAST RESORT among the
   standalone kinds: if the claim's effect would show up in the
   venue's numbers it is "empirical", and if a search can settle it
   it is "fact" — only what neither can touch is an assumption.
4. Otherwise, is it a logic link between other steps — a claim about
   why one step follows from another, drawing its force from the steps
   it builds on? If yes -> "inference".
5. If none fit cleanly, or it blends a real-world fact with a venue
   claim -> "unclear": it needs to be broken down further first.

The structure must agree with the kind:
- The hypothesis step (the final claim the whole graph exists to
  support) has already been identified and removed from the list below,
  so no step should be classified "hypothesis".
- A step WITH parents can never be "fact" or "assumption" — both are
  standalone; nothing produces them. If the content reads like one but
  it has parents, classify it "unclear".
- A step WITHOUT parents can never be "inference" — an inference links
  other steps, so it must build on something. If the content reads like
  an inference but it has no parents, classify it "unclear".
- "empirical" can go either way: a standalone venue claim (no parents) or
  a conclusion derived from other steps (with parents).

Steps (each with its parents):
{steps}

Classify every step in the given order.
