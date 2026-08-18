"""Replay a run: every node, and every prompt the models actually saw.

The web UI answers "what did it do?". This answers "why did it write that?" —
the only reliable way to tell a bad prompt from a bad model.

    .venv/bin/python trace.py              # last run, summary
    .venv/bin/python trace.py -v           # + full prompts and raw replies
    .venv/bin/python trace.py -l           # list saved runs
    .venv/bin/python trace.py traces/X.json
"""

import argparse
import json
import sys
from pathlib import Path

TRACES = Path(__file__).resolve().parent / "traces"
RULE = "─" * 78


def _fmt(text: str, verbose: bool, head: int = 12) -> str:
    lines = (text or "").splitlines()
    if verbose or len(lines) <= head:
        return "\n".join("    " + ln for ln in lines)
    shown = "\n".join("    " + ln for ln in lines[:head])
    return f"{shown}\n    … {len(lines) - head} more lines (-v to show)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", help="trace file (default: latest)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="show prompts and raw replies in full")
    ap.add_argument("-l", "--list", action="store_true", help="list saved runs")
    a = ap.parse_args()

    if a.list:
        runs = sorted(p for p in TRACES.glob("*.json") if p.name != "latest.json")
        for p in runs:
            d = json.loads(p.read_text())
            print(f"{p.name}  {d.get('question','')[:60]}")
        print(f"\n{len(runs)} runs in {TRACES}")
        return 0

    path = Path(a.path) if a.path else TRACES / "latest.json"
    if not path.exists():
        sys.exit(f"no trace at {path} — ask a question first")
    d = json.loads(path.read_text())

    print(RULE)
    print(f"QUESTION  {d['question']}")
    print(f"started   {d.get('started')}   attempts {d.get('attempts')}"
          f"   verdict {d.get('verdict')}")
    print(RULE)

    print("\nWHAT THE GRAPH DID\n")
    for e in d.get("events", []):
        node = e["node"]
        if node == "write_sql":
            u = e.get("usage", {})
            print(f"  write_sql  attempt {e.get('attempt')}   "
                  f"{u.get('in',0)}→{u.get('out',0)} tok  {u.get('secs',0)}s")
            print(_fmt(e.get("sql", ""), a.verbose, 8))
            if e.get("precheck"):
                for p in e["precheck"]:
                    print(f"    ⚠ precheck: {p}")
        elif node == "execute":
            print(f"  execute    {'ok' if e.get('ok') else 'FAILED'}  "
                  f"{e.get('rows')} rows  {e.get('secs')}s")
            if e.get("error"):
                print(f"    ✗ {e['error']}")
        elif node == "diagnose_empty":
            print(f"  diagnose   {e.get('summary')}")
            for c in e.get("conditions") or []:
                print(f"    without {c['dropped'][:56]:<58} → {c['rows_without_it']}")
        elif node == "verify":
            print(f"  verify     {e.get('verdict','').upper()}  — {e.get('reason','')[:150]}")
        elif node == "revise":
            print(f"  revise     {e.get('detail','')[:150]}")
        elif node == "nudge":
            print("  nudge      repeated query blocked")
        elif node == "answer":
            print("  answer")
        print()

    print(RULE)
    calls = d.get("calls", [])
    tin = sum(c["usage"].get("in", 0) for c in calls)
    tout = sum(c["usage"].get("out", 0) for c in calls)
    print(f"\nWHAT THE MODELS SAW   {len(calls)} calls, {tin:,} in / {tout:,} out\n")
    for i, c in enumerate(calls, 1):
        u = c["usage"]
        print(f"── call {i}: {c['role']}  [{u['model']}]  "
              f"{u.get('in',0)}→{u.get('out',0)} tok  {u.get('secs')}s")
        print("  SYSTEM:")
        print(_fmt(c["system"], a.verbose, 6))
        print("  USER:")
        print(_fmt(c["user"], a.verbose, 10))
        print("  REPLY:")
        print(_fmt(c["response"], a.verbose, 8))
        print()

    print(RULE)
    print("ANSWER:", (d.get("answer") or "-")[:400])
    return 0


if __name__ == "__main__":
    sys.exit(main())
