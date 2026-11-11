Decide how a fresh answer should update the checklist.

Question: {question}
Answer: {answer}

Current checklist lines:
{checklist_lines}

Decide one action:
- "add": the answer is a real conclusion not yet on the checklist. Put a
  one-line finding in plain words in `line`. Leave `target_line` empty.
- "retire": the answer proves an existing checklist line false. Put that
  exact existing line in `target_line`. Leave `line` empty.
- "merge": the finding is a near-duplicate of an existing line. Put the
  existing line in `target_line` and the merged single line in `line`.
- "nothing": the answer is inconclusive, partial, or adds nothing. Leave
  both `line` and `target_line` empty.

Respond with the action, line, and target_line.
