"""Render a navigation run as one page.

    uv run python run_report.py state_nav/eval5/model        # writes state_nav/eval5/model/report.html
    uv run python run_report.py state_nav/eval5/model --open # and opens it
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "agent" / "Agent_architecture" / "navigation_team"))

from report import build_report  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("state_dir", nargs="+", help="one or more run folders")
    p.add_argument("--open", action="store_true", help="open in the browser")
    a = p.parse_args()
    for d in a.state_dir:
        path = build_report(d)
        print(path)
        if a.open:
            subprocess.run(["open", path], check=False)


if __name__ == "__main__":
    main()
