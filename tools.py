"""What the agent can do to the database: look at it, and query it. Nothing else.

The connection is read-only and results are row-capped, so a bad query costs a
few milliseconds instead of the dataset. The database stays a trustworthy
oracle — which is the whole basis for scoring this agent.
"""

import re
import threading
from typing import Any

import db
import profile_db

MAX_ROWS = 200          # what we return to the caller
PROMPT_ROWS = 15        # what we show the model (context is expensive)
TIMEOUT_S = 25          # a generated query must never hang the whole run

# Anything that could change data or reach outside the query engine.
FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|create|alter|attach|copy|install|load|"
    r"pragma|export|import)\b",
    re.IGNORECASE,
)


def schema_text() -> str:
    """Schema *and* value profile for the prompt.

    Structure alone (`order_status VARCHAR`) is not enough to write a correct
    filter — you also need the values ('delivered', 'shipped', …) and the
    ranges (data ends 2018-10-17). Both are computed once at build time and
    cost ~1,000 tokens, far less than the failures they prevent.
    """
    return profile_db.as_text()


def distinct_values(table: str, column: str, limit: int = 20) -> list:
    """Sample the actual values in a column — the fix for a model guessing
    'FURNITURE' when the data says 'moveis_decoracao'."""
    con = db.connect()
    try:
        rows = con.execute(
            f"SELECT DISTINCT {column} FROM {table} "
            f"WHERE {column} IS NOT NULL LIMIT {int(limit)}"
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        con.close()


def run_sql(query: str) -> dict[str, Any]:
    """Execute a SELECT. Returns {ok, columns, rows, row_count} or {ok:False, error}.

    The error string is returned verbatim on purpose: DuckDB's messages name
    the offending column or syntax, which is exactly the signal the revise
    step needs to fix itself.
    """
    if FORBIDDEN.search(query):
        return {"ok": False, "error": "Only read-only SELECT queries are allowed."}

    con = db.connect()
    out: dict[str, Any] = {}

    def work():
        try:
            cur = con.execute(query)
            columns = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchmany(MAX_ROWS)
            out.update({"ok": True, "columns": columns,
                        "rows": [list(r) for r in rows], "row_count": len(rows),
                        "truncated": len(rows) == MAX_ROWS})
        except Exception as e:
            out.update({"ok": False, "error": f"{type(e).__name__}: {e}"})

    # A generated query can be accidentally quadratic — one bad join against
    # the 1M-row geolocation table would otherwise block the run forever.
    # DuckDB can be interrupted from another thread, so run it in one.
    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(TIMEOUT_S)
    if t.is_alive():
        con.interrupt()
        t.join(5)
        out = {"ok": False,
               "error": (f"Query cancelled after {TIMEOUT_S}s. It is too "
                         f"expensive — simplify it: avoid joining geolocation "
                         f"unless needed, and make sure every join has a key.")}
    try:
        con.close()
    except Exception:
        pass
    return out


def result_preview(result: dict, max_rows: int = PROMPT_ROWS) -> str:
    """Render a result as compact text for the model to inspect.

    Deliberately small: the verifier needs to see the SHAPE and a few values
    to judge whether the query answered the question, not the whole table.
    """
    if not result.get("ok"):
        return f"ERROR: {result.get('error')}"
    if not result["rows"]:
        return "(0 rows)"
    head = " | ".join(result["columns"])
    body = "\n".join(
        " | ".join("NULL" if v is None else str(v) for v in row)
        for row in result["rows"][:max_rows]
    )
    more = ""
    if result["row_count"] > max_rows:
        more = f"\n... ({result['row_count'] - max_rows} more rows)"
    return f"{head}\n{body}{more}"
