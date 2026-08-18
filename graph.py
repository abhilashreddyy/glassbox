"""The agent: question in, verified answer out.

    START → write_sql → (repeat? → nudge ↩) → execute ─rows→ verify ─ok→ answer
                ▲                               │  │                │
                │                               │  └─0 rows→ diagnose_empty
                └──────────── revise ───────────┴────────────┴───────┘
                                                        (attempts ≤ 3)

Every path back to write_sql goes through one `revise` node, which reads
whatever the detecting node left in `pending`. Four things can send it there:

  1. precheck   a query that provably cannot match (caught before running it)
  2. sql error  DuckDB's own message, verbatim
  3. diagnosis  which filter emptied the result, with row counts
  4. critique   the verifier rejected a result that ran fine

The order is deliberate: 1–3 are decided by code, and only 4 asks a model.
Anything the database can settle is settled before an opinion is consulted.
"""

import contextvars
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Annotated, Optional, TypedDict

import yaml
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

import checks
import tools
from config import get_model, model_name

MAX_ATTEMPTS = 3
ROOT = Path(__file__).resolve().parent


def _load_glossary() -> str:
    """House definitions of business terms, rendered for the prompt.

    Goes to the writer AND the verifier: the rule is stated where the query is
    written, and again where it is checked. A rule only the writer sees is a
    rule the checker cannot enforce.
    """
    path = ROOT / "glossary.yaml"
    if not path.exists():
        return ""
    terms = yaml.safe_load(path.read_text()) or {}
    return "\n".join(f"- {k}: {v.strip()}" for k, v in terms.items())


GLOSSARY = _load_glossary()


# ── tracing ─────────────────────────────────────────────────────────────────
# The UI shows WHAT each node did. A trace also records what each model was
# actually SENT and what it said back verbatim — the only way to answer "why
# did it write that?" instead of guessing. Kept local: no service, no account,
# nothing leaves the machine.
TRACES = ROOT / "traces"
_TRACE: contextvars.ContextVar = contextvars.ContextVar("trace", default=None)


def _trace_start(question: str) -> dict:
    """A context variable, not a global: the server runs each request in its own
    thread, and a global would interleave two users' traces."""
    run = {"question": question,
           "started": datetime.now().isoformat(timespec="seconds"),
           "calls": [], "events": []}
    _TRACE.set(run)
    return run


def _trace_flush(final: dict) -> Path | None:
    run = _TRACE.get()
    if run is None:
        return None
    run["events"] = final.get("events", [])
    run["sql"] = final.get("sql")
    run["answer"] = final.get("answer")
    run["verdict"] = final.get("verdict")
    run["attempts"] = final.get("attempts")
    TRACES.mkdir(exist_ok=True)
    path = TRACES / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(run, indent=1, default=str))
    latest = TRACES / "latest.json"
    latest.unlink(missing_ok=True)
    latest.write_text(path.read_text())
    _TRACE.set(None)
    return path


# ── state ───────────────────────────────────────────────────────────────────
def append(old: list, new: list) -> list:
    return (old or []) + (new or [])


class S(TypedDict):
    question: str
    schema: str
    sql: Optional[str]
    result: Optional[dict]
    verdict: Optional[str]              # "ok" | "bad"
    critique: Optional[str]
    diagnosis: Optional[dict]           # why a result was empty
    pending: Optional[list]             # what the next revise should say
    feedback: Annotated[list, append]   # everything learned this run
    tried: Annotated[list, append]      # SQL fingerprints already attempted
    attempts: int
    answer: Optional[str]
    events: Annotated[list, append]     # what the UI renders


def _norm(sql: str) -> str:
    """Fingerprint: whitespace/case-insensitive, so cosmetic edits don't count
    as a new attempt. (String, not tuple — checkpointers serialize state.)"""
    return re.sub(r"\s+", " ", (sql or "").strip().lower()).rstrip(";")


def _is_empty(result: dict) -> bool:
    """Did this query actually find anything?

    `row_count == 0` is not enough. A bare aggregate over zero matching rows
    returns ONE row containing NULL — `SELECT SUM(price) ... WHERE city =
    'Lisbon'` yields `[[None]]`, which looks like a result and is not one.
    Observed in the wild: the agent reported "no revenue records exist" and the
    verifier approved it, with no diagnosis of why. That shrug is the exact
    failure this project exists to catch.
    """
    if result.get("row_count", 0) == 0:
        return True
    rows = result.get("rows") or []
    return len(rows) == 1 and all(v is None for v in rows[0])


def _ev(node: str, **kw) -> dict:
    return {"node": node, "t": round(time.time(), 3), **kw}


def _call(role: str, system: str, user: str) -> tuple[str, dict]:
    """One LLM call. Returns (text, usage) — usage is what the UI meters."""
    t0 = time.time()
    msg = get_model(role).invoke([SystemMessage(system), HumanMessage(user)])
    u = getattr(msg, "usage_metadata", None) or {}
    usage = {"model": model_name(role), "in": u.get("input_tokens", 0),
             "out": u.get("output_tokens", 0), "secs": round(time.time() - t0, 2)}
    text = msg.content if isinstance(msg.content, str) else str(msg.content)

    run = _TRACE.get()
    if run is not None:
        run["calls"].append({"role": role, "usage": usage,
                             "system": system, "user": user, "response": text})
    return text.strip(), usage


def _extract_sql(text: str) -> str:
    """Models wrap SQL in prose and code fences however they feel like."""
    fence = re.search(r"```(?:sql)?\s*(.+?)```", text, re.S | re.I)
    if fence:
        return fence.group(1).strip().rstrip(";")
    m = re.search(r"\b(WITH|SELECT)\b.+", text, re.S | re.I)
    return (m.group(0).strip().rstrip(";") if m else text.strip().rstrip(";"))


# ── nodes ───────────────────────────────────────────────────────────────────
WRITE_SYS = """You are a senior data analyst. Write ONE DuckDB SQL query that answers the user's question.

Rules:
- Output only the SQL in a ```sql code block. No explanation.
- Use only the tables and columns given. Never invent names.
- The schema lists each column's actual VALUES and RANGES — use them. Never
  filter on a value that is not in the data.
- Prefer explicit JOINs. Alias aggregates with clear names (e.g. AS total_revenue).
- Never alias a table with a reserved word (or, is, in, as...).
- Add ORDER BY and LIMIT when the question asks for "top"/"worst"/"best" N.

DEFINITIONS — these are the house meanings of business terms. Follow them
exactly, even if another reading seems reasonable:
{glossary}"""


def write_sql(state: S) -> dict:
    parts = [f"Schema and data profile:\n{state['schema']}",
             f"\nQuestion: {state['question']}"]
    if state.get("feedback"):
        parts.append("\nPrevious attempts failed. Fix these problems:\n"
                     + "\n".join(f"- {f}" for f in state["feedback"][-4:]))

    text, usage = _call("sql_writer",
                        WRITE_SYS.format(glossary=GLOSSARY or "(none)"),
                        "\n".join(parts))
    sql = _extract_sql(text)

    # Deterministic gate: catch a query that cannot match before spending a
    # database round trip and, more importantly, before the result gets
    # rationalised downstream.
    problems = checks.precheck_sql(sql)
    return {
        "sql": sql,
        "pending": problems or None,
        "events": [_ev("write_sql", sql=sql, usage=usage,
                       attempt=state.get("attempts", 0) + 1,
                       precheck=problems or None)],
    }


def nudge(state: S) -> dict:
    """Same SQL twice. The model cannot see its own repetition as a problem, so
    code notices and edits the prompt instead of re-running a known query."""
    return {
        "attempts": state["attempts"] + 1,
        "feedback": [f"You already tried this exact query and it did not work: "
                     f"{state['sql']} Do NOT submit it again. Change the "
                     f"approach — different joins, filters, or aggregation."],
        "events": [_ev("nudge", sql=state["sql"])],
    }


def execute(state: S) -> dict:
    t0 = time.time()
    result = tools.run_sql(state["sql"])
    ev = _ev("execute", ok=result["ok"], secs=round(time.time() - t0, 3),
             rows=result.get("row_count"), error=result.get("error"),
             preview=tools.result_preview(result, 8),
             columns=result.get("columns", []), data=result.get("rows", [])[:50])
    return {
        "result": result,
        "tried": [_norm(state["sql"])],
        "pending": None if result["ok"] else [f"The query failed with: {result['error']}"],
        "events": [ev],
    }


def diagnose_empty(state: S) -> dict:
    """Zero rows is ambiguous: no such data, or a wrong query.

    Drop one AND-ed filter at a time and see where rows reappear. Pure Python —
    it cannot talk itself into 'an empty result is acceptable here'.
    """
    d = checks.diagnose(state["sql"])
    pending = None
    if d.get("culprits"):
        worst = max(d["culprits"], key=lambda c: c["rows_without_it"])
        pending = [f"The result was empty. Diagnosis: removing "
                   f"`{worst['condition']}` yields {worst['rows_without_it']:,} "
                   f"rows, so that filter is wrong or too narrow. Fix it — do "
                   f"not simply drop it unless the question truly does not ask "
                   f"for it."]
    return {"diagnosis": d, "pending": pending,
            "events": [_ev("diagnose_empty", summary=d.get("summary"),
                           conditions=d.get("conditions"))]}


VERIFY_SYS = """You check whether a SQL query truly answers a question. You are the last line of defence against a confident wrong answer.

Reply with exactly one line of JSON:
{{"verdict": "ok", "reason": "..."}} or {{"verdict": "bad", "reason": "..."}}

Say "bad" if: the query answers a different question, a required filter is
missing, it contradicts a DEFINITION below, the aggregation or grouping is
wrong, or the numbers are implausible.
Say "ok" if the query and result genuinely answer the question. Do not demand
extra columns or stylistic changes.

DEFINITIONS — the house meanings. A query that contradicts one of these is
"bad" even if it looks reasonable:
{glossary}"""


def verify(state: S) -> dict:
    user = (f"Question: {state['question']}\n\n"
            f"SQL:\n{state['sql']}\n\n"
            f"Result:\n{tools.result_preview(state['result'])}")
    text, usage = _call("verifier",
                        VERIFY_SYS.format(glossary=GLOSSARY or "(none)"), user)
    verdict, reason = "ok", ""
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            parsed = json.loads(m.group(0))
            verdict = str(parsed.get("verdict", "ok")).lower()
            reason = str(parsed.get("reason", ""))
        except json.JSONDecodeError:
            verdict, reason = ("bad" if re.search(r"\bbad\b", text, re.I) else "ok"), text[:300]
    else:
        verdict, reason = ("bad" if re.search(r"\bbad\b", text, re.I) else "ok"), text[:300]
    verdict = "bad" if verdict not in ("ok", "bad") else verdict
    return {
        "verdict": verdict,
        "critique": reason,
        "pending": [f"A reviewer rejected the previous query: {reason}"] if verdict == "bad" else None,
        "events": [_ev("verify", verdict=verdict, reason=reason, usage=usage)],
    }


def revise(state: S) -> dict:
    """The single way back to write_sql. Whatever detected the problem left its
    explanation in `pending`; this promotes it to `feedback` and spends one of
    the three attempts."""
    pending = state.get("pending") or ["The previous attempt was unsatisfactory."]
    return {
        "attempts": state["attempts"] + 1,
        "feedback": list(pending),
        "pending": None,
        "events": [_ev("revise", detail=" ".join(pending)[:400])],
    }


ANSWER_SYS = """You report a data result to a business user. Two or three sentences.
State the actual numbers from the result. No SQL, no hedging, no invented facts.
If the result is empty, explain WHAT the diagnosis says is missing rather than
just saying there is no data. If the result failed verification, say so plainly."""


def answer(state: S) -> dict:
    notes = []
    if state.get("verdict") == "bad":
        notes.append(f"This result did not pass verification: {state.get('critique')}")
    d = state.get("diagnosis")
    if d and d.get("summary") and _is_empty(state.get("result") or {}):
        notes.append(f"Why the result is empty: {d['summary']}")
    result = state.get("result") or {"ok": False, "error": "no query succeeded"}
    user = (f"Question: {state['question']}\n\n"
            f"Result:\n{tools.result_preview(result)}")
    if notes:
        user += "\n\nNOTE: " + " ".join(notes)
    text, usage = _call("answerer", ANSWER_SYS, user)
    return {"answer": text, "events": [_ev("answer", text=text, usage=usage)]}


# ── routers: control flow lives in code, not in the model's output ──────────

def after_write(state: S) -> str:
    if _norm(state["sql"]) in (state.get("tried") or []):
        return "nudge" if state["attempts"] < MAX_ATTEMPTS else "answer"
    if state.get("pending"):                       # precheck found a problem
        return "revise" if state["attempts"] < MAX_ATTEMPTS else "execute"
    return "execute"


def after_execute(state: S) -> str:
    if not state["result"]["ok"]:
        return "revise" if state["attempts"] < MAX_ATTEMPTS else "answer"
    if _is_empty(state["result"]):
        return "diagnose_empty"
    return "verify"


def after_diagnose(state: S) -> str:
    if state.get("pending") and state["attempts"] < MAX_ATTEMPTS:
        return "revise"
    return "answer"                                # genuinely absent: say so


def after_verify(state: S) -> str:
    if state["verdict"] == "ok" or state["attempts"] >= MAX_ATTEMPTS:
        return "answer"
    return "revise"


# ── graph ───────────────────────────────────────────────────────────────────
def build_graph():
    g = StateGraph(S)
    for name, fn in (("write_sql", write_sql), ("nudge", nudge),
                     ("execute", execute), ("diagnose_empty", diagnose_empty),
                     ("verify", verify), ("revise", revise), ("answer", answer)):
        g.add_node(name, fn)

    g.add_edge(START, "write_sql")
    g.add_conditional_edges("write_sql", after_write)
    g.add_edge("nudge", "write_sql")
    g.add_conditional_edges("execute", after_execute)
    g.add_conditional_edges("diagnose_empty", after_diagnose)
    g.add_conditional_edges("verify", after_verify)
    g.add_edge("revise", "write_sql")
    g.add_edge("answer", END)
    return g.compile()


APP = build_graph()


def initial_state(question: str) -> dict:
    return {"question": question, "schema": tools.schema_text(), "sql": None,
            "result": None, "verdict": None, "critique": None, "diagnosis": None,
            "pending": None, "feedback": [], "tried": [], "attempts": 0,
            "answer": None, "events": []}


def ask(question: str) -> dict:
    """Blocking run — used by the eval harness."""
    _trace_start(question)
    final = APP.invoke(initial_state(question), {"recursion_limit": 40})
    _trace_flush(final)
    return final


def stream(question: str):
    """Yield each node's update as it happens — this is what the UI renders."""
    _trace_start(question)
    merged: dict = {"events": []}
    for step in APP.stream(initial_state(question), {"recursion_limit": 40}):
        for node, update in step.items():
            merged["events"].extend(update.get("events", []))
            for k in ("sql", "answer", "verdict", "attempts"):
                if update.get(k) is not None:
                    merged[k] = update[k]
            yield node, update
    _trace_flush(merged)


if __name__ == "__main__":
    import sys

    q = " ".join(sys.argv[1:]) or "top 3 spending customers in the last few months"
    final = ask(q)
    for e in final["events"]:
        print(f"[{e['node']}] " + json.dumps(
            {k: v for k, v in e.items() if k not in ("node", "t", "data", "columns")}
        )[:260])
    print("\nSQL:\n" + (final["sql"] or "-"))
    print("\nANSWER:\n" + (final["answer"] or "-"))
