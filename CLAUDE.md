- This is a hackathon project. All code will be done in python.
- The framework used for this project will be the langchain framework. ALL AGENTS BUILT IN THIS PROJECT WILL BE UTILISING the langchain framework. This includes langchain, deepagents, langgraph.
- The agents we will be building will be large language model agents.

Always refer to the problem_statement.md file for the overall idea of the project before we start

**Instructions to follow**
1. whenever a conversation enter a discussion phase, never automatically write anything down. Instead always ask the user for confirmation first, providing the final details before the user confirms, after which then u can write down.
2. Before writing a code, ensure that the user intent is clearly stated, if there are any areas of ambiguity of the discussion that can be left up to interpretation, do not assume anything, instead, ask the user what do they intent to do about that exact area.
3. if the area is too large, break it down into bit size pieces, focusing the discussion on one piece at a time.
4. Before starting any discussion, look at the docs folder for more information. You are to retrieve all relevent information pertaining to this particular area before before the discussion starts. 
5. Do not retrieve any information that is more than necessary.
6. When writing into docs, exclude reason for any decision made. write the docs in such a way that is simplified and easy for the user to understand. Do not use big jargon words to describe what is decided or the feature. 
7. Do not write into the docs what it is not to do. keep the docs lean and updated, only write what it is supposed to do or what the product feature should have, not what it should not have since its redundant information.
7. During conversation, before you output, explain to the user using common verbs and pronouns and words instead of using extremely technical wordings.
8. Assume the user have an undergraduate degree in mathematics, any mathematical terms, define it properly when telling the user.
9. when updating the doc, do not mention how to use it nor say what have been built, just write beside each part (built)
10. During discussion with the user, your output should 

IF THE BUILD IS ON THE AGENT... DO NOT WRITE ON THE SIMULATION FOLDER AT ALL. ONLY WRITE ON THE AGENT FOLDER.
IF THE BUILD IS ON THE SIMULATION... DO NOT WRITE ON THE AGENT FOLDER AT ALL, ONLY WRITE ON THE SIMULATION FOLDER.

**Agent and simulation separation**
- The simulation is a stand-in for a real venue. Vibechecker (the agent system) is built as if the environment were a real place it cannot see inside.
- In any session focused on the agent, do not open, read, or reference `simulation/`, `docs/simulation.md`, `run_sim.py`, or `simulation env.ipynb`. This applies to subagents too.
- The only environment facts available to agent work are the ones written in the `agent/` folder. `agent/goal.md` is the source of truth there.
- If an agent-side question cannot be answered from the `agent/` folder, ask the user. Do not go looking in the simulation.

**Delegation**
- You (the main agent) are NOT to build code or run simulations/experiments yourself.
- Building or editing code: give exact, complete instructions to `build-agent` (.claude/agents/build-agent.md) and let it make the changes and run the self-checks.
- Running simulations or experiments: give the exact experiment spec to `sim-run-agent` (.claude/agents/sim-run-agent.md) and let it run the code and report the output.
- Everything else stays with you: discussion, design decisions, docs, analysis of results, and reporting to the user.