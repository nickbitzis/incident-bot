"""Command line: serve the API and UI, run the scripted quarter, write sample outputs."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path


def cmd_simulate(a: argparse.Namespace) -> int:
    from incidentbot.metrics import summarize
    from incidentbot.postmortem import to_markdown
    from incidentbot.simulation import check, run_quarter

    t0 = time.perf_counter()
    res = run_quarter(seed=a.seed)
    elapsed = time.perf_counter() - t0
    summary = summarize(res.incidents)
    problems = check(res.incidents)
    o = summary["overall"]
    print(f"simulated {summary['total']} incidents in {elapsed:.2f}s (seed {a.seed})")
    for sev, b in summary["by_severity"].items():
        adh = b["cadence_adherence"]
        print(f"  {sev}: {b['count']:2d} incidents, median TTA {b['tta']['median']} min, "
              f"TTM {b['ttm']['median']} min, TTR {b['ttr']['median']} min, cadence "
              f"{'n/a' if adh is None else f'{adh:.0%}'}, reminders {b['reminders']}")
    ai = summary["action_items"]
    print(f"  overall: median TTR {o['ttr']['median']} min, cadence adherence "
          f"{o['cadence_adherence']:.0%}, action items {ai['done']}/{ai['total']} done")
    if a.out:
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        slim = {k: v for k, v in summary.items() if k != "incidents"}
        slim["incidents"] = summary["incidents"]
        (out / "sim-results.json").write_text(json.dumps(slim, indent=2) + "\n")
        sample = max(res.incidents, key=lambda i: (i.severity == "sev1", len(i.timeline)))
        (out / "sample-postmortem.md").write_text(to_markdown(sample.postmortem))
        print(f"wrote {out / 'sim-results.json'} and {out / 'sample-postmortem.md'}")
    if a.check:
        if problems:
            print("CHECK FAILED:")
            for p in problems:
                print(f"  {p}")
            return 1
        print(f"check passed: {len(res.incidents)} incidents resolved with complete timelines "
              "and valid postmortem drafts")
    return 0


def cmd_serve(a: argparse.Namespace) -> int:
    import uvicorn

    from incidentbot.api import create_app

    uvicorn.run(create_app(), host=a.host, port=a.port, log_level="warning")
    return 0


def cmd_healthcheck(a: argparse.Namespace) -> int:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{a.port}/healthz", timeout=3) as r:
            return 0 if json.load(r).get("status") == "ok" else 1
    except Exception:
        return 1


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="incidentbot")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("simulate", help="run the scripted quarter of 20 incidents")
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--check", action="store_true", help="exit 1 unless every incident is "
                   "resolved with a complete timeline")
    s.add_argument("--out", help="directory for sim-results.json and sample-postmortem.md")
    s.set_defaults(fn=cmd_simulate)
    v = sub.add_parser("serve", help="run the API and web UI")
    v.add_argument("--host", default="127.0.0.1")
    v.add_argument("--port", type=int, default=8850)
    v.set_defaults(fn=cmd_serve)
    h = sub.add_parser("healthcheck", help="exit 0 when the local server is healthy")
    h.add_argument("--port", type=int, default=8850)
    h.set_defaults(fn=cmd_healthcheck)
    a = ap.parse_args(argv)
    sys.exit(a.fn(a))
