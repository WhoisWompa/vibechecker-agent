"""Put the navigation team, the shared modules and the agent folders on sys.path.

The agent folders follow Lionel's layout (one folder per agent, bare-name
imports inside), so anything that drives them has to see all of these.
Import this module first.
"""

import sys
from pathlib import Path

NAV = Path(__file__).resolve().parent
ARCH = NAV.parent
ROOT = ARCH.parents[1]  # the project root (vibechecker/): where simulation/ lives

ANALYSIS = ARCH / "analysis_team"

# Lionel's layout after the 7 Sep 2026 merge: memory.py, llm.py, run_log.py and scanner.py at
# ARCH; memory_agent/ and reflection_agent/ beside them; the five analysis agents (and the
# data handler, which both teams use) under analysis_team/.
for _p in (ROOT, ARCH, NAV, NAV / "daily_agent", NAV / "intraday_agent",
           ARCH / "memory_agent", ARCH / "reflection_agent", ANALYSIS,
           ANALYSIS / "data_handler_agent", ANALYSIS / "supervisor_agent"):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)
