"""Check that this copy of Vibechecker runs on this machine, in one command.

    uv run python check_setup.py               # everything that needs no model (about a minute)
    uv run python check_setup.py --with-model  # also one cheap Haiku call through agent/.env
    uv run python check_setup.py --quick       # skip the tests and the simulated day

What it checks, in order: Python and the packages, the weather dataset the simulation replays,
agent/.env and whether the AWS keys are valid (an STS call, no model cost), the test suite, the
analysis side's self-checks, one heuristic day of the navigation team plus a dry run of the
analysis pass over it, and (with --with-model) one model call. Prints a table; exits 1 if a
required step fails.
"""

from __future__ import annotations

import argparse
import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable

PACKAGES = ["pandas", "pydantic", "dotenv", "langchain", "langchain_core", "langgraph", "langchain_aws",
            "boto3", "sentence_transformers", "sklearn"]
SELF_CHECKS = [
    "agent/Agent_architecture/memory.py",
    "agent/Agent_architecture/scanner.py",
    "agent/Agent_architecture/reflection_agent/reflection_agent.py",
    "agent/Agent_architecture/memory_agent/memory_agent.py",
    "agent/Agent_architecture/analysis_team/workflow.py",
    "agent/Agent_architecture/analysis_team/data_handler_agent/data_handler_agent.py",
    "agent/Agent_architecture/analysis_team/researcher_agent/pipeline.py",
]
DATASET = ROOT / "data" / "weather"

rows: list[tuple[str, str, str]] = []  # (step, PASS/FAIL/SKIP/WARN, detail)


def record(step: str, ok: bool | None, detail: str = "", warn: bool = False) -> None:
    status = "SKIP" if ok is None else ("PASS" if ok else ("WARN" if warn else "FAIL"))
    rows.append((step, status, detail))
    print(f"  {status:<4} {step}" + (f": {detail}" if detail else ""), flush=True)


def run(cmd: list[str], timeout: int = 600) -> tuple[int, str]:
    p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout,
                       env={**os.environ, "PYTHONWARNINGS": "ignore"})
    return p.returncode, (p.stdout + p.stderr).strip()


def check_python_and_packages() -> None:
    v = sys.version_info
    record("python >= 3.11", v >= (3, 11), f"{v.major}.{v.minor}.{v.micro} at {PY}")
    missing = []
    for name in PACKAGES:
        try:
            importlib.import_module(name)
        except Exception:
            missing.append(name)
    hint = "" if not missing else f"missing {missing}; run: uv sync --extra bedrock --extra calendar --extra memory --extra dev"
    record("packages importable", not missing, hint or f"{len(PACKAGES)} packages")


def check_dataset() -> None:
    csv = DATASET / "Historical2hourWeatherForecast2024.csv"
    ok = DATASET.is_dir() and csv.exists() and (DATASET / "24hr").is_dir()
    record("weather dataset", ok, str(DATASET) if ok else
           f"copy the 2-hour CSV (Historical2hourWeatherForecast2024.csv, ~190 MB, not in the repo) into data/weather/ under {ROOT}")


def check_env() -> bool:
    env_path = ROOT / "agent" / ".env"
    if not env_path.exists():
        record("agent/.env", False, "missing; copy agent/.env.example to agent/.env and fill it in")
        return False
    from dotenv import dotenv_values
    cfg = dotenv_values(env_path)
    provider = (cfg.get("PROVIDER") or "").lower()
    model = cfg.get("DEFAULT_MODEL") or ""
    record("agent/.env", bool(provider and model), f"PROVIDER={provider or '?'} DEFAULT_MODEL={model or '?'}")
    if not provider.startswith("bedrock"):
        record("aws credentials", None, f"provider {provider!r} does not use AWS")
        return bool(cfg.get("API_KEY"))
    keys = all(cfg.get(k) for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"))
    profile = cfg.get("AWS_PROFILE") or ""
    if not keys and not profile:
        record("aws credentials", False, "paste the three AWS keys into agent/.env (they expire every 12 h) or set AWS_PROFILE",
               warn=True)
        return False
    try:
        import boto3
        session_kwargs = {"region_name": cfg.get("AWS_DEFAULT_REGION") or "us-east-1"}
        if keys:
            session_kwargs.update(aws_access_key_id=cfg["AWS_ACCESS_KEY_ID"], aws_secret_access_key=cfg["AWS_SECRET_ACCESS_KEY"],
                                  aws_session_token=cfg["AWS_SESSION_TOKEN"])
        else:
            session_kwargs["profile_name"] = profile
        arn = boto3.Session(**session_kwargs).client("sts").get_caller_identity()["Arn"]
        record("aws credentials", True, f"valid ({arn.split('/')[-1]}), no model called")
        return True
    except Exception as exc:
        msg = str(exc)
        expired = "Expired" in msg or "InvalidClientTokenId" in msg
        record("aws credentials", False, ("expired: paste fresh keys" if expired else msg[:120]), warn=True)
        return False


def check_tests() -> None:
    t0 = time.time()
    code, out = run([PY, "-m", "pytest", "-q", "-W", "ignore::DeprecationWarning"])
    last = out.splitlines()[-1] if out else ""
    record("pytest (navigation team + bridge)", code == 0, f"{last} in {time.time() - t0:.0f}s")


def check_self_checks() -> None:
    bad = []
    for rel in SELF_CHECKS:
        # stdout only: memory_agent.py's check loads the embedding model, which logs a progress bar to stderr
        p = subprocess.run([PY, rel], cwd=str(ROOT), capture_output=True, text=True, timeout=300,
                           env={**os.environ, "PYTHONWARNINGS": "ignore"})
        last = p.stdout.strip().splitlines()[-1].strip() if p.stdout.strip() else ""
        if p.returncode != 0 or last != "ok":
            err = (p.stderr.strip().splitlines() or [""])[-1]
            bad.append(f"{rel}: {last or err or 'no output'}"[:160])
    record("analysis self-checks", not bad, f"{len(SELF_CHECKS)} scripts print ok" if not bad else "; ".join(bad))


def check_simulated_day(dataset_ok: bool) -> None:
    if not dataset_ok:
        record("one heuristic day + analysis dry run", None, "needs the weather dataset")
        return
    tmp = Path(tempfile.mkdtemp(prefix="vibechecker-check-"))
    try:
        t0 = time.time()
        code, out = run([PY, "run_navigation.py", "--days", "1", "--seed", "3", "--start", "2024-04-05", "--mode", "heuristic",
                         "--preset", "demo", "--note", "big group of 12 at 3pm-5pm", "--memory", "state_nav/demo/memory.json",
                         "--state", str(tmp / "nav"), "--quiet"])
        closed = "close:" in out
        wrote = all((tmp / "nav" / f).exists() for f in ("history.db", "question_bank.json", "memory.json", "summary.json"))
        record("one heuristic day (no model)", code == 0 and closed and wrote,
               (out.splitlines()[-1][:120] if code != 0 else f"history.db, question bank, memory written in {time.time() - t0:.0f}s"))
        code, out = run([PY, "run_analysis.py", "--state", str(tmp / "nav"), "--dry-run"])
        record("analysis pass dry run", code == 0 and "open question" in out, out.splitlines()[0][:120] if out else "no output")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_model_call() -> None:
    sys.path.insert(0, str(ROOT / "agent" / "Agent_architecture"))
    try:
        from llm import get_llm, resolve_model
        t0 = time.time()
        reply = get_llm("memory", max_tokens=20).invoke("Reply with the single word: ok")
        text = reply.content if isinstance(reply.content, str) else str(reply.content)
        usage = getattr(reply, "usage_metadata", None) or {}
        record("one model call (MEMORY_MODEL)", "ok" in text.lower(),
               f"{resolve_model('memory')} answered in {time.time() - t0:.1f}s, {usage.get('total_tokens', '?')} tokens")
    except Exception as exc:
        record("one model call (MEMORY_MODEL)", False, str(exc)[:160])


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--with-model", action="store_true", help="also make one cheap model call through agent/.env")
    p.add_argument("--quick", action="store_true", help="skip the test suite and the simulated day")
    a = p.parse_args()
    print(f"vibechecker setup check in {ROOT}")
    check_python_and_packages()
    dataset_ok = DATASET.is_dir()
    check_dataset()
    creds_ok = check_env()
    if not a.quick:
        check_tests()
        check_self_checks()
        check_simulated_day(dataset_ok)
    if a.with_model:
        if creds_ok:
            check_model_call()
        else:
            record("one model call (MEMORY_MODEL)", None, "credentials not valid, skipped")
    failed = [r for r in rows if r[1] == "FAIL"]
    warned = [r for r in rows if r[1] == "WARN"]
    print()
    print(f"{len(rows) - len(failed) - len(warned)} passed, {len(warned)} warning(s), {len(failed)} failed")
    if failed:
        print("fix first: " + "; ".join(r[0] for r in failed))
    elif warned:
        print("everything without a model works; " + "; ".join(r[2] for r in warned))
    else:
        print("all good: run the two-day command in README.md")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
