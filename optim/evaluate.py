#!/usr/bin/env python
"""Score the navigation team in heuristic mode over a fixed grid of seeds, start dates and presets.

    uv run python optim/evaluate.py --set train --label mychange [--compare optim/runs/baseline-train.json]

Writes optim/runs/<label>-<set>.json and prints a table. With --compare it prints paired deltas
against the reference file and an ACCEPT / REJECT verdict under the agreed rule:
  comfort (primary) must rise on average by more than 0.005 and win at least 60% of paired runs,
  hidden stay must not fall by more than 0.5 min, pushes per day must not exceed 1.25x + 1 of the reference.
For --set holdout the comfort requirement is only "not worse" (delta >= 0).
The grader's numbers (hidden_score) are read as scalars only. Nothing in simulation/ is imported here.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "optim" / "runs"
DAYS = 2

SETS = {
    # Friday->Saturday and Monday->Tuesday, a room that fills (demo) and the default room
    "train": dict(seeds=[1, 2, 3, 4, 5, 6], starts=["2024-04-05", "2024-01-08"], presets=[None, "demo"]),
    "holdout": dict(seeds=[11, 12, 13, 14, 15, 16], starts=["2024-07-12", "2024-10-14"], presets=[None, "demo"]),
    "quick": dict(seeds=[1, 2, 3], starts=["2024-04-05"], presets=["demo"]),
}


def configs(name: str):
    s = SETS[name]
    for seed in s["seeds"]:
        for start in s["starts"]:
            for preset in s["presets"]:
                yield {"seed": seed, "start": start, "preset": preset,
                       "key": f"s{seed}-{start}-{preset or 'default'}"}


def run_one(cfg: dict, label: str, days: int) -> dict:
    state = RUNS / label / cfg["key"]
    shutil.rmtree(state, ignore_errors=True)
    cmd = [sys.executable, str(ROOT / "run_navigation.py"), "--days", str(days), "--seed", str(cfg["seed"]),
           "--start", cfg["start"], "--mode", "heuristic", "--state", str(state), "--quiet"]
    if cfg["preset"]:
        cmd += ["--preset", cfg["preset"]]
    t0 = time.time()
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if p.returncode != 0:
        return {**cfg, "error": (p.stderr or p.stdout)[-2000:]}
    summ = json.loads((state / "summary.json").read_text())
    days_ = summ["days"]
    hs = [d.get("hidden_score") or {} for d in days_]
    comfort = [h.get("comfort") for h in hs if h.get("comfort") is not None]
    stay = [h.get("stay") for h in hs if h.get("stay") is not None]
    block = [h.get("block") for h in hs if h.get("block") is not None]
    return {
        **cfg,
        "comfort": sum(comfort) / len(comfort) if comfort else None,
        "comfort_by_day": comfort,
        "stay": sum(stay) / len(stay) if stay else None,
        "block": sum(block) / len(block) if block else None,
        "replans": sum(d["replans"] for d in days_) / len(days_),
        "mean_occupancy": sum(d["mean_occupancy"] for d in days_) / len(days_),
        "entered": sum(d["entered"] for d in days_) / len(days_),
        "secs": round(time.time() - t0, 1),
    }


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def aggregate(rows):
    ok = [r for r in rows if "error" not in r]
    out = {"n": len(ok), "errors": len(rows) - len(ok),
           "comfort": mean(r["comfort"] for r in ok), "stay": mean(r["stay"] for r in ok),
           "block": mean(r["block"] for r in ok), "replans": mean(r["replans"] for r in ok),
           "mean_occupancy": mean(r["mean_occupancy"] for r in ok)}
    for preset in ("default", "demo"):
        sub = [r for r in ok if (r["preset"] or "default") == preset]
        out[f"comfort_{preset}"] = mean(r["comfort"] for r in sub)
        out[f"stay_{preset}"] = mean(r["stay"] for r in sub)
    return out


def fmt(x, nd=3):
    return "-" if x is None else f"{x:.{nd}f}"


def compare(rows, ref_rows, holdout: bool):
    ref = {r["key"]: r for r in ref_rows if "error" not in r}
    pairs = [(r, ref[r["key"]]) for r in rows if "error" not in r and r["key"] in ref]
    if not pairs:
        return {"verdict": "REJECT", "reasons": ["no paired runs"]}
    dc = [(a["comfort"] or 0) - (b["comfort"] or 0) for a, b in pairs]
    ds = [(a["stay"] or 0) - (b["stay"] or 0) for a, b in pairs]
    wins = sum(1 for d in dc if d > 0.0005)
    losses = sum(1 for d in dc if d < -0.0005)
    rep_new = mean(a["replans"] for a, _ in pairs)
    rep_ref = mean(b["replans"] for _, b in pairs)
    res = {"pairs": len(pairs), "comfort_delta": mean(dc), "comfort_wins": wins, "comfort_losses": losses,
           "stay_delta": mean(ds), "replans_new": rep_new, "replans_ref": rep_ref,
           "worst_comfort_delta": min(dc), "best_comfort_delta": max(dc)}
    reasons = []
    if holdout:
        if mean(dc) < 0:
            reasons.append(f"holdout comfort worse ({mean(dc):+.4f})")
    else:
        if mean(dc) <= 0.005:
            reasons.append(f"comfort gain {mean(dc):+.4f} <= 0.005")
        if wins / max(1, wins + losses) < 0.6:
            reasons.append(f"comfort wins {wins}/{wins + losses} < 60%")
    if mean(ds) < -0.5:
        reasons.append(f"stay fell {mean(ds):+.2f} min")
    if rep_new > rep_ref * 1.25 + 1:
        reasons.append(f"pushes/day {rep_new:.1f} > {rep_ref * 1.25 + 1:.1f}")
    res["verdict"] = "ACCEPT" if not reasons else "REJECT"
    res["reasons"] = reasons
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="train", choices=sorted(SETS))
    ap.add_argument("--label", required=True)
    ap.add_argument("--compare", default=None, help="reference results json (usually the baseline for the same set)")
    ap.add_argument("--days", type=int, default=DAYS)
    ap.add_argument("--jobs", type=int, default=max(2, (os.cpu_count() or 4) - 2))
    ap.add_argument("--keep-state", action="store_true", help="keep the per-run state dirs (default: delete after scoring)")
    a = ap.parse_args()

    cfgs = list(configs(a.set))
    t0 = time.time()
    with ThreadPoolExecutor(a.jobs) as ex:
        rows = list(ex.map(lambda c: run_one(c, a.label, a.days), cfgs))
    if not a.keep_state:
        shutil.rmtree(RUNS / a.label, ignore_errors=True)
    agg = aggregate(rows)
    out = {"label": a.label, "set": a.set, "days": a.days, "aggregate": agg, "runs": rows,
           "wall_secs": round(time.time() - t0, 1)}
    if a.compare:
        ref = json.loads(Path(a.compare).read_text())
        out["compare"] = compare(rows, ref["runs"], holdout=(a.set == "holdout"))
        out["compare"]["against"] = ref["label"]
    RUNS.mkdir(parents=True, exist_ok=True)
    path = RUNS / f"{a.label}-{a.set}.json"
    path.write_text(json.dumps(out, indent=1))

    print(f"{a.label} [{a.set}] {agg['n']} runs x {a.days} days, {agg['errors']} errors, {out['wall_secs']}s")
    print(f"  comfort {fmt(agg['comfort'])}  (default {fmt(agg['comfort_default'])}, demo {fmt(agg['comfort_demo'])})"
          f"  stay {fmt(agg['stay'], 1)}  block {fmt(agg['block'])}  pushes/day {fmt(agg['replans'], 1)}"
          f"  occ {fmt(agg['mean_occupancy'], 1)}")
    for r in rows:
        if "error" in r:
            print(f"  ERROR {r['key']}: {r['error'][-300:]}")
    if a.compare:
        c = out["compare"]
        print(f"  vs {c['against']}: comfort {c['comfort_delta']:+.4f} (wins {c['comfort_wins']}, losses {c['comfort_losses']},"
              f" worst {c['worst_comfort_delta']:+.4f}, best {c['best_comfort_delta']:+.4f})"
              f"  stay {c['stay_delta']:+.2f}  pushes/day {c['replans_new']:.1f} vs {c['replans_ref']:.1f}")
        print(f"  {c['verdict']}" + (": " + "; ".join(c["reasons"]) if c["reasons"] else ""))
    print(f"  saved {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
