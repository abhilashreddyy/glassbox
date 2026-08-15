"""Execution-accuracy eval: run gold SQL, run agent SQL, compare RESULTS.

    .venv/bin/python eval/run_eval.py            # one run per question
    .venv/bin/python eval/run_eval.py -k 3       # 3 runs each: reliability

Why results and not SQL text: many correct queries answer one question, so
grading text punishes difference rather than error. This is the same principle
as tau2 grading final database state instead of conversation quality.

Two numbers matter, and the gap between them is the interesting one:
  accuracy   — share of runs that matched gold
  pass^k     — share of questions correct on EVERY run (stochastic agents look
               much better on the first number than the second)
"""

import argparse
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import graph  # noqa: E402
import tools  # noqa: E402


def normalize(result: dict, ordered: bool) -> list | None:
    """Comparable form of a result set.

    - numbers rounded (1234.5601 == 1234.56)
    - column NAMES dropped (revenue vs total_revenue is the same answer)
    - row order ignored unless the question is a top-N/ranking
    """
    if not result.get("ok"):
        return None
    rows = []
    for row in result["rows"]:
        cells = []
        for v in row:
            if isinstance(v, bool):
                cells.append(v)
            elif isinstance(v, (int, float)):
                cells.append(round(float(v), 2))
            else:
                cells.append(str(v).strip().lower() if v is not None else None)
        rows.append(tuple(cells))
    return rows if ordered else sorted(rows, key=repr)


def rows_match(gold: list, got: list | None, ordered: bool) -> bool:
    """Gold values must appear in the agent's rows — the standard text-to-SQL
    subset rule.

    Asking "worst category" and getting (furniture_decor, 2.41) instead of
    (furniture_decor,) is a correct answer with an extra column, not a wrong
    one. Row COUNT must still match, so an agent can't pass by dumping the
    whole table and hoping the gold row is in there somewhere.
    """
    if got is None or len(gold) != len(got):
        return False
    if ordered:
        return all(set(g).issubset(set(r)) for g, r in zip(gold, got))
    remaining = list(got)
    for g in gold:
        for i, r in enumerate(remaining):
            if set(g).issubset(set(r)):
                remaining.pop(i)
                break
        else:
            return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", type=int, default=1, help="runs per question")
    args = ap.parse_args()

    questions = yaml.safe_load((ROOT / "eval" / "questions.yaml").read_text())
    per_q, total_runs, total_ok = {}, 0, 0
    t_start = time.time()

    for q in questions:
        gold = normalize(tools.run_sql(q["gold"]), q.get("ordered", False))
        if gold is None:
            print(f"!! gold SQL failed for {q['id']} — fix the eval, not the agent")
            continue

        outcomes = []
        for _ in range(args.k):
            t0 = time.time()
            try:
                final = graph.ask(q["question"])
                got = normalize(final.get("result") or {}, q.get("ordered", False))
                ok = rows_match(gold, got, q.get("ordered", False))
            except Exception as e:
                final, ok = {"verdict": f"crash: {type(e).__name__}"}, False
            outcomes.append(ok)
            total_runs += 1
            total_ok += ok
            mark = "PASS" if ok else "FAIL"
            print(f"  {mark}  {q['id']:<20} {time.time()-t0:>5.1f}s  "
                  f"verify={final.get('verdict')}  attempts={final.get('attempts')}")
        per_q[q["id"]] = outcomes

    n_q = len(per_q)
    all_pass = sum(1 for v in per_q.values() if all(v))
    print("\n" + "=" * 60)
    print(f"questions {n_q}   runs {total_runs}   elapsed {time.time()-t_start:.0f}s")
    print(f"accuracy  {total_ok}/{total_runs} = {total_ok/max(total_runs,1):.1%}")
    print(f"pass^{args.k}    {all_pass}/{n_q} = {all_pass/max(n_q,1):.1%}"
          "   (correct on every run)")
    failed = [k for k, v in per_q.items() if not all(v)]
    if failed:
        print("not always correct: " + ", ".join(failed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
