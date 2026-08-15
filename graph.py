"""The agent: question in, verified answer out.

    START → write_sql → (repeat? → nudge ↩) → execute ─ok→ verify ─ok→ answer → END
                ▲                                 │            │
                └──── revise (error/critique) ────┴────────────┘   (attempts ≤ 3)

Two cycles, both guarded by `attempts`:
  1. SQL errored        → feed DuckDB's own message back and rewrite
  2. SQL ran but is wrong → the verifier's critique goes back and rewrites

The verify node is the point of the project. Anyone can prompt a model for SQL;
the interesting engineering is deciding whether the result actually answers the
question, and doing something about it when it doesn't.
"""

import json
import re
import time
from typing import Annotated, Optional, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

import tools
from config import get_model, model_name

MAX_ATTEMPTS = 3


# ── state ───────────────────────────────────────────────────────────────────
def append(old: list, new: list) -> list:
    return (old or []) + (new or [])


class S(TypedDict):
    question: str
    schema: str
    sql: Optional[str]
    result: Optional[dict]
    verdict: Optional[str]          # "ok" | "bad"
    critique: Optional[str]         # why the verifier rejected it
    feedback: Annotated[list, append]   # everything learned this run, fed back
    tried: Annotated[list, append]      # SQL fingerprints already attempted
    attempts: int
    answer: Optional[str]
    events: Annotated[list, append]     # what the UI renders (the glass box)


def _norm(sql: str) -> str:
    """Fingerprint: whitespace/case-insensitive, so cosmetic edits don't count
    as a new attempt. (String, not tuple — checkpointers serialize state.)"""
    return re.sub(r"\s+", " ", (sql or "").strip().lower()).rstrip(";")


def _ev(node: str, **kw) -> dict:
    return {"node": node, "t": round(time.time(), 3), **kw}


def _call(role: str, system: str, user: str) -> tuple[str, dict]:
    """One LLM call. Returns (text, usage) — usage is what the UI meters."""
    t0 = time.time()
    msg = get_model(role).invoke([SystemMessage(system), HumanMessage(user)])
    u = getattr(msg, "usage_metadata", None) or {}
    usage = {
        "model": model_name(role),
        "in": u.get("input_tokens", 0),
        "out": u.get("output_tokens", 0),
        "secs": round(time.time() - t0, 2),
    }
    text = msg.content if isinstance(msg.content, str) else str(msg.content)
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
- Use only the tables and columns in the schema. Never invent names.
- Prefer explicit JOINs. Alias aggregates with clear names (e.g. AS total_revenue).
- Revenue means order_items.price unless the question says otherwise; freight is separate.
- Category names are Portuguese; join product_category_name_translation for English.
- If the question implies completed sales, filter orders.order_status = 'delivered'.
- Add ORDER BY and LIMIT when the question asks for "top"/"worst"/"best" N."""


def write_sql(state: S) -> dict:
    parts = [f"Schema:\n{state['schema']}", f"\nQuestion: {state['question']}"]
    if state.get("feedback"):
        parts.append(
            "\nPrevious attempts failed. Fix these problems:\n"
            + "\n".join(f"- {f}" for f in state["feedback"][-4:])
        )
    text, usage = _call("sql_writer", WRITE_SYS, "\n".join(parts))
    sql = _extract_sql(text)
    return {
        "sql": sql,
        "events": [_ev("write_sql", sql=sql, usage=usage,
                       attempt=state.get("attempts", 0) + 1)],
    }


def nudge(state: S) -> dict:
    """Same SQL twice. The model can't see its own repetition as a problem —
    so code notices and edits the prompt. (Proven in the tau2 replay: identical
    context repeats forever; one explicit line changes the output.)"""
    return {
        "attempts": state["attempts"] + 1,
        "feedback": [
            f"You already tried this exact query and it did not work: {state['sql']} "
            "Do NOT submit it again. Change the approach — different joins, "
            "different filters, or a different aggregation."
        ],
        "events": [_ev("nudge", sql=state["sql"])],
    }


def execute(state: S) -> dict:
    t0 = time.time()
    result = tools.run_sql(state["sql"])
    ev = _ev("execute", ok=result["ok"], secs=round(time.time() - t0, 3),
             rows=result.get("row_count"), error=result.get("error"),
             preview=tools.result_preview(result, 8),
             # the UI renders the actual table, not just the text preview
             columns=result.get("columns", []), data=result.get("rows", [])[:50])
    return {"result": result, "tried": [_norm(state["sql"])], "events": [ev]}


VERIFY_SYS = """You check whether a SQL query truly answers a question. You are the last line of defence against a confident wrong answer.

Reply with exactly one line of JSON:
{"verdict": "ok", "reason": "..."} or {"verdict": "bad", "reason": "..."}

Say "bad" if: the query answers a different question, a required filter is
missing (e.g. delivered-only), the aggregation or grouping is wrong, the result
is empty when data should exist, or the numbers are implausible.
Say "ok" if the query and result genuinely answer the question. Do not demand
extra columns or stylistic changes."""


def verify(state: S) -> dict:
    user = (
        f"Question: {state['question']}\n\n"
        f"SQL:\n{state['sql']}\n\n"
        f"Result:\n{tools.result_preview(state['result'])}"
    )
    text, usage = _call("verifier", VERIFY_SYS, user)
    verdict, reason = "ok", ""
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            parsed = json.loads(m.group(0))
            verdict = str(parsed.get("verdict", "ok")).lower()
            reason = str(parsed.get("reason", ""))
        except json.JSONDecodeError:
            verdict = "bad" if re.search(r"\bbad\b", text, re.I) else "ok"
            reason = text[:300]
    else:
        verdict = "bad" if re.search(r"\bbad\b", text, re.I) else "ok"
        reason = text[:300]
    verdict = "bad" if verdict not in ("ok", "bad") else verdict
    return {
        "verdict": verdict,
        "critique": reason,
        "events": [_ev("verify", verdict=verdict, reason=reason, usage=usage)],
    }


ANSWER_SYS = """You report a data result to a business user. Two or three sentences.
State the actual numbers from the result. No SQL, no hedging, no invented facts.
If the result is empty or the query failed, say plainly that you could not answer."""


def answer(state: S) -> dict:
    caveat = ""
    if state.get("verdict") == "bad":
        caveat = ("\n\nNOTE: this result did not pass verification "
                  f"({state.get('critique')}). Say so honestly in your answer.")
    result = state.get("result") or {"ok": False, "error": "no query succeeded"}
    user = (f"Question: {state['question']}\n\n"
            f"Result:\n{tools.result_preview(result)}{caveat}")
    text, usage = _call("answerer", ANSWER_SYS, user)
    return {"answer": text, "events": [_ev("answer", text=text, usage=usage)]}


# ── routers: control flow lives in code, not in the model's output ──────────
def after_write(state: S) -> str:
    if _norm(state["sql"]) in (state.get("tried") or []):
        return "nudge" if state["attempts"] < MAX_ATTEMPTS else "answer"
    return "execute"


def after_execute(state: S) -> str:
    if state["result"]["ok"]:
        return "verify"
    if state["attempts"] >= MAX_ATTEMPTS:
        return "answer"                      # out of retries: answer honestly
    return "revise_error"


def after_verify(state: S) -> str:
    if state["verdict"] == "ok" or state["attempts"] >= MAX_ATTEMPTS:
        return "answer"
    return "revise_critique"


def revise_error(state: S) -> dict:
    return {
        "attempts": state["attempts"] + 1,
        "feedback": [f"The query failed with: {state['result']['error']}"],
        "events": [_ev("revise", why="sql_error", detail=state["result"]["error"])],
    }


def revise_critique(state: S) -> dict:
    return {
        "attempts": state["attempts"] + 1,
        "feedback": [f"A reviewer rejected the previous query: {state['critique']}"],
        "events": [_ev("revise", why="verify_failed", detail=state["critique"])],
    }


# ── graph ───────────────────────────────────────────────────────────────────
def build_graph():
    g = StateGraph(S)
    for name, fn in (("write_sql", write_sql), ("nudge", nudge), ("execute", execute),
                     ("verify", verify), ("answer", answer),
                     ("revise_error", revise_error),
                     ("revise_critique", revise_critique)):
        g.add_node(name, fn)

    g.add_edge(START, "write_sql")
    g.add_conditional_edges("write_sql", after_write)
    g.add_edge("nudge", "write_sql")
    g.add_conditional_edges("execute", after_execute)
    g.add_conditional_edges("verify", after_verify)
    g.add_edge("revise_error", "write_sql")
    g.add_edge("revise_critique", "write_sql")
    g.add_edge("answer", END)
    return g.compile()


APP = build_graph()


def initial_state(question: str) -> dict:
    return {"question": question, "schema": tools.schema_text(), "sql": None,
            "result": None, "verdict": None, "critique": None, "feedback": [],
            "tried": [], "attempts": 0, "answer": None, "events": []}


def ask(question: str) -> dict:
    """Blocking run — used by the eval harness."""
    return APP.invoke(initial_state(question), {"recursion_limit": 40})


def stream(question: str):
    """Yield each node's update as it happens — this is what the UI renders."""
    for step in APP.stream(initial_state(question), {"recursion_limit": 40}):
        for node, update in step.items():
            yield node, update


if __name__ == "__main__":
    import sys

    q = " ".join(sys.argv[1:]) or "Which product category has the worst average review score?"
    final = ask(q)
    for e in final["events"]:
        print(f"[{e['node']}] " + json.dumps({k: v for k, v in e.items()
                                              if k not in ("node", "t")})[:220])
    print("\nSQL:\n" + (final["sql"] or "-"))
    print("\nANSWER:\n" + (final["answer"] or "-"))
