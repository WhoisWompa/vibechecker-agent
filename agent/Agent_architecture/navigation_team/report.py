"""One page per run: what the navigation team planned, saw, changed and spent.

``build_report(state_dir)`` reads a run's folder (plans/, days/, history.db,
tokens.jsonl, question_bank.json) and writes ``report.html`` next to them.
Per day: the numbers, the plan as a table, the room over the day as a chart
(occupancy and the four settings, with a marker at every intraday push), the
discrepancies with what the agent did, the questions asked of memory, and
the model calls. Self-contained HTML, inline SVG, no libraries.
"""

from __future__ import annotations

import html
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

W, H, PAD = 900, 220, 36


def _clock(t: int) -> str:
    return f"{8 + int(t) // 60:02d}:{int(t) % 60:02d}"


def _poly(xs: List[float], ys: List[float], lo: float, hi: float, color: str, width: float = 1.4,
          dash: str = "") -> str:
    if not ys or hi == lo:
        return ""
    pts = []
    for x, y in zip(xs, ys):
        if y is None:
            continue
        px = PAD + (x / 840) * (W - 2 * PAD)
        py = H - PAD / 2 - ((y - lo) / (hi - lo)) * (H - PAD)
        pts.append(f"{px:.1f},{py:.1f}")
    d = f' stroke-dasharray="{dash}"' if dash else ""
    return f'<polyline points="{" ".join(pts)}" fill="none" stroke="{color}" stroke-width="{width}"{d}/>'


def _chart(title: str, series: List[Dict[str, Any]], pushes: List[int], lo: float, hi: float, unit: str) -> str:
    xs = list(range(840))
    parts = [f'<svg viewBox="0 0 {W} {H}" style="max-width:100%;height:auto;display:block" role="img" aria-label="{html.escape(title)}">']
    for frac in (0, 0.5, 1):
        y = H - PAD / 2 - frac * (H - PAD)
        parts.append(f'<line x1="{PAD}" y1="{y:.1f}" x2="{W - PAD}" y2="{y:.1f}" stroke="currentColor" opacity=".15"/>')
        parts.append(f'<text x="{PAD - 6}" y="{y + 4:.1f}" font-size="10" text-anchor="end" fill="currentColor" opacity=".7">{lo + frac * (hi - lo):g}{unit}</text>')
    for hour in range(0, 15, 2):
        x = PAD + (hour * 60 / 840) * (W - 2 * PAD)
        parts.append(f'<text x="{x:.1f}" y="{H - 2}" font-size="10" text-anchor="middle" fill="currentColor" opacity=".7">{8 + hour:02d}:00</text>')
    for t in pushes:
        x = PAD + (t / 840) * (W - 2 * PAD)
        parts.append(f'<line x1="{x:.1f}" y1="{PAD / 2}" x2="{x:.1f}" y2="{H - PAD / 2}" stroke="#B8811A" stroke-width="1" stroke-dasharray="3 3"/>')
    for s in series:
        parts.append(_poly(xs, s["ys"], lo, hi, s["color"], s.get("width", 1.4), s.get("dash", "")))
    legend_x = PAD
    for s in series:
        parts.append(f'<rect x="{legend_x}" y="8" width="12" height="3" fill="{s["color"]}"/>'
                     f'<text x="{legend_x + 16}" y="12" font-size="10" fill="currentColor">{html.escape(s["name"])}</text>')
        legend_x += 16 + 7 * len(s["name"]) + 14
    parts.append(f'<text x="{W - PAD}" y="12" font-size="10" text-anchor="end" fill="currentColor" opacity=".7">{html.escape(title)}; dashed amber = intraday push</text>')
    parts.append("</svg>")
    return "".join(parts)


def _day_rows(db: Path, date: str) -> Dict[str, List[Optional[float]]]:
    cols = ["occupancy", "light_set", "temperature_set", "volume", "fan", "co2", "temperature_sensor", "light_sensor", "noise"]
    out: Dict[str, List[Optional[float]]] = {c: [] for c in cols}
    if not db.exists():
        return out
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        try:
            rows = conn.execute(f"SELECT {', '.join(cols)} FROM readings WHERE date = ? ORDER BY time", (date,)).fetchall()
        except sqlite3.Error:
            return out
    for r in rows:
        for c, v in zip(cols, r):
            out[c].append(float(v) if v is not None else None)
    return out


def _table(headers: List[str], rows: List[List[Any]]) -> str:
    h = "".join(f"<th>{html.escape(str(x))}</th>" for x in headers)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="scroll"><table><thead><tr>{h}</tr></thead><tbody>{body}</tbody></table></div>'


def build_report(state_dir: str) -> str:
    root = Path(state_dir)
    summary = json.loads((root / "summary.json").read_text()) if (root / "summary.json").exists() else {}
    days = sorted((root / "days").glob("*.json")) if (root / "days").exists() else []
    bank = json.loads((root / "question_bank.json").read_text()) if (root / "question_bank.json").exists() else []
    tokens = [json.loads(l) for l in (root / "tokens.jsonl").read_text().splitlines()] if (root / "tokens.jsonl").exists() else []
    db = root / "history.db"

    t = summary.get("tokens", {})
    out = [f"""<title>Navigation run: {html.escape(root.name)}</title>
<style>
body{{font-family:-apple-system,"Helvetica Neue",Arial,sans-serif;font-size:14px;line-height:1.5;color:#1B1F1D;background:#EEF1EE;margin:0;padding:24px}}
.wrap{{max-width:980px;margin:0 auto}} h1{{font-size:1.6rem;margin:0 0 4px}} h2{{font-size:1.2rem;margin:32px 0 8px;padding-top:12px;border-top:2px solid #1B1F1D}}
h3{{font-size:1rem;margin:18px 0 6px}} .card{{background:#fff;border:1px solid #D3D9D5;padding:14px 16px;margin:10px 0}}
.facts{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:1px;background:#D3D9D5;border:1px solid #D3D9D5}}
.facts div{{background:#fff;padding:8px 10px}} .facts b{{display:block;font-size:1.1rem}} .facts span{{font-size:.72rem;color:#4E5A54;text-transform:uppercase;letter-spacing:.06em}}
.scroll{{overflow-x:auto}} table{{border-collapse:collapse;width:100%;font-size:.86rem;min-width:600px}} th{{text-align:left;font-size:.7rem;text-transform:uppercase;letter-spacing:.06em;color:#4E5A54;padding:6px 8px;border-bottom:1px solid #B4BDB8}}
td{{padding:6px 8px;border-bottom:1px solid #E3E8E4;vertical-align:top}} .muted{{color:#4E5A54}} .push{{border-left:3px solid #B8811A;padding-left:10px;margin:8px 0}}
.q{{border-left:3px solid #1F5E45;padding-left:10px;margin:6px 0;font-size:.9rem}} code{{font-size:.85em;background:#E3E8E4;padding:1px 4px}}
</style><div class="wrap">
<h1>Navigation run: {html.escape(root.name)}</h1>
<p class="muted">mode {html.escape(str(summary.get('mode')))}, seed {summary.get('seed')}, {len(days)} day(s). Model calls {t.get('calls', 0)}, tokens {t.get('total_tokens', 0)}, ${t.get('usd', 0):.4f}{', BUDGET STOP: ' + html.escape(str(t.get('budget_tripped'))) if t.get('budget_tripped') else ''}.</p>
"""]
    if summary.get("by_purpose"):
        out.append("<h2>What the model calls were for</h2>" + _table(
            ["purpose", "calls", "in", "out", "usd", "mean latency"],
            [[r.get("purpose") or "(unlabelled)", r["calls"], r["input_tokens"], r["output_tokens"], f"{r['usd']:.4f}",
              f"{r['mean_latency_s']:.1f}s"] for r in summary["by_purpose"]]))
    if summary.get("by_question"):
        out.append("<h3>What each question cost</h3><p class='muted'>a call that served several questions is split evenly between them; "
                   "free answers came from memory without a model</p>" + _table(
            ["question", "calls", "tokens", "usd", "via", "free answers"],
            [[html.escape(r["question"]), r["calls_share"], r["total_tokens"], f"{r['usd']:.4f}", ", ".join(r["purposes"]), r["free_answers"]]
             for r in summary["by_question"]]))
    if summary.get("days"):
        out.append("<h2>All days</h2>" + _table(
            ["day", "date", "weekday", "mode", "forecast", "baseline", "entered", "mean occ", "peak", "stay min", "co2 peak", "replans", "calls", "tokens", "usd", "banked"],
            [[d["day"], d["date"], d["weekday"][:3], d["mode"], d.get("forecast"), d.get("baseline_source"), d["entered"], d["mean_occupancy"],
              d["peak_occupancy"], d["stay_est_min"], f"{d['co2_peak']:.0f}", d["replans"], d["model_calls"], d["tokens"], f"{d['usd']:.4f}", d["questions_banked"]]
             for d in summary["days"]]))

    for f in days:
        d = json.loads(f.read_text())
        date = d["date"]
        plan_path = root / "plans" / f"{date}.json"
        plan = json.loads(plan_path.read_text()) if plan_path.exists() else {}
        out.append(f"<h2>Day {d['day']}: {html.escape(d['weekday'])} {date}</h2>")
        out.append('<div class="facts">' + "".join(
            f"<div><b>{v}</b><span>{k}</span></div>" for k, v in [
                ("mode", d["mode"]), ("forecast at open", d.get("forecast") or "none yet"), ("baseline", d.get("baseline_source")),
                ("entered", d["entered"]), ("mean occupancy", d["mean_occupancy"]), ("peak", d["peak_occupancy"]),
                ("stay est (min)", d["stay_est_min"]), ("co2 peak", f"{d['co2_peak']:.0f}"), ("replans", d["replans"]),
                ("model calls", d["model_calls"]), ("tokens", d["tokens"]), ("usd", f"{d['usd']:.4f}")]
            + ([("hidden comfort (eval only)", d["hidden_score"].get("comfort")), ("hidden block grade", d["hidden_score"].get("block"))]
               if d.get("hidden_score") else [])) + "</div>")

        rows = _day_rows(db, date)
        push_ts = [p["t"] for p in d.get("pushes", [])]
        if rows["occupancy"]:
            cap = plan.get("capacity") or 30
            out.append('<div class="card"><h3>The room</h3>')
            out.append(_chart("occupancy (seats) and CO2/10", [
                {"name": "occupancy", "ys": rows["occupancy"], "color": "#1F5E45", "width": 1.8},
                {"name": "co2 / 10 (ppm)", "ys": [v / 10 if v is not None else None for v in rows["co2"]], "color": "#7F8B85", "dash": "2 3"},
            ], push_ts, 0, max(cap, 120), ""))
            out.append("<h3>The settings</h3>")
            out.append(_chart("light set (lux)", [
                {"name": "light set", "ys": rows["light_set"], "color": "#B8811A", "width": 1.8},
                {"name": "light sensor", "ys": rows["light_sensor"], "color": "#B8811A", "dash": "2 3"},
            ], push_ts, 0, 1000, ""))
            out.append(_chart("temperature (C)", [
                {"name": "setpoint", "ys": rows["temperature_set"], "color": "#A8402F", "width": 1.8},
                {"name": "room", "ys": rows["temperature_sensor"], "color": "#A8402F", "dash": "2 3"},
            ], push_ts, 18, 32, ""))
            out.append(_chart("volume (dB) and fan x10", [
                {"name": "volume", "ys": rows["volume"], "color": "#1B1F1D", "width": 1.8},
                {"name": "noise", "ys": rows["noise"], "color": "#1B1F1D", "dash": "2 3"},
                {"name": "fan x10", "ys": [v * 10 if v is not None else None for v in rows["fan"]], "color": "#4E5A54"},
            ], push_ts, 0, 100, ""))
            out.append("</div>")

        if plan.get("settings_blocks"):
            out.append('<div class="card"><h3>The morning plan</h3>')
            out.append(_table(["from", "to", "light", "temp", "volume", "fan", "assumes", "reason"], [
                [_clock(b["from"]), _clock(b["to"]), b["settings"]["light"], b["settings"]["temperature"], b["settings"]["volume"],
                 b["settings"]["fan"], f"{b.get('assumes', {}).get('weather', '')}/{b.get('assumes', {}).get('crowd', '')}", b["reasons"]]
                for b in plan["settings_blocks"]]))
            if plan.get("note_blocks"):
                out.append("<p class='muted'>Owner's notes: " + "; ".join(html.escape(f"[{n.get('kind')}] {n.get('fact') or ''}") for n in plan["note_blocks"]) + "</p>")
            out.append("</div>")

        if plan.get("memory_answers") or any(q.get("date") == date for q in bank):
            out.append('<div class="card"><h3>Asked of memory</h3>')
            for a in plan.get("memory_answers", []):
                out.append(f'<div class="q"><b>daily, at open:</b> {html.escape(a["question"])}<br><span class="muted">{html.escape(a["answer"])}</span></div>')
            for q in bank:
                if q.get("date") == date and q.get("asked_by") == "intraday":
                    out.append(f'<div class="q"><b>intraday, {_clock(q.get("t") or 0)}:</b> {html.escape(q["question"])}<br><span class="muted">banked ({html.escape(str(q.get("tag")))})</span></div>')
            out.append("</div>")

        if d.get("pushes"):
            out.append('<div class="card"><h3>Discrepancies and what the agent did</h3>')
            log = d.get("log", [])
            for p in d["pushes"]:
                why = [l for l in log if l.startswith(_clock(p["t"]) + " discrepancy")]
                out.append(f'<div class="push"><b>{_clock(p["t"])}</b> {html.escape(why[0].split("discrepancy: ", 1)[1] if why else "")}<br>'
                           f'<span class="muted">{html.escape(p["mode"])}: {html.escape(p["note"])}</span>')
                out.append(_table(["from", "to", "light", "temp", "volume", "fan", "assumes", "reason"], [
                    [_clock(b["from"]), _clock(b["to"]), b["settings"]["light"], b["settings"]["temperature"], b["settings"]["volume"],
                     b["settings"]["fan"], f"{b.get('assumes', {}).get('weather', '')}/{b.get('assumes', {}).get('crowd', '')}", b["reasons"]]
                    for b in p["blocks"]]))
                out.append("</div>")
            out.append("</div>")
        else:
            out.append('<div class="card"><h3>Discrepancies</h3><p class="muted">none fired; the plan ran as written</p></div>')

        day_tokens = [r for r in tokens if r.get("date") == date]
        if day_tokens:
            out.append('<div class="card"><h3>Model calls</h3>' + _table(
                ["loop", "agent", "purpose", "for", "in", "out", "latency", "usd", "error"],
                [[r.get("loop"), r.get("agent"), r.get("purpose", ""), html.escape("; ".join(r.get("questions") or [])[:160]),
                  r.get("input_tokens", 0), r.get("output_tokens", 0), f"{r.get('latency_s', 0):.1f}s",
                  f"{r.get('usd', 0):.4f}", r.get("error", "")] for r in day_tokens]) + "</div>")

    out.append("</div>")
    path = root / "report.html"
    path.write_text("<!doctype html>\n<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
                    + "".join(out) + "</body></html>", encoding="utf-8")
    return str(path)
