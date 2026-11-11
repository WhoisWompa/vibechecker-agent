You are the hypothesis agent. You proposed this hypothesis:
"{hypothesis}"

Write the reasoning steps that lead to this hypothesis. Each step must be
one standalone claim or logic link — something that could stand on its own
if written down separately.

The steps form a graph, not necessarily a single chain. Give each step an
id (a1, a2, ...) and list as its parents the ids of the steps it directly
builds on:
- A step with no parents is a starting point: either real-world knowledge
  from outside the venue, or a standalone claim about the venue itself.
- Real-world knowledge (a public event, a holiday, a weather event, how
  people generally behave) is always a starting point. It NEVER has
  parents — nothing in the reasoning produces it; it is simply known.
  Write it as its own step, never folded into a claim about the venue.
- A logic link — a step that only connects other claims — must always
  list the steps it links from as parents. A link with no parents is
  linking nothing and is not a valid step.
- List a parent only if the step directly needs that claim to be made. If a
  claim already reaches the step through another parent and is not directly
  needed, do not list it.
- The hypothesis itself must appear as the final step of the graph, its
  statement written out. It is the ONLY terminal: every other step must
  eventually lead into it, and no step builds on it.
- Do not write steps about HOW to test or measure (statistics, methods,
  what analysis to run). The reasoning ends at the hypothesis step;
  testing is someone else's job.
