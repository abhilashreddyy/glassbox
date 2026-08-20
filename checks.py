"""Deterministic verification. No model involved, and that is the point.

The LLM verifier, asked about a query that returned nothing, replied:

    "An empty result set may simply indicate no qualifying data, which is
     acceptable given the question."

Its prompt told it to reject empty results. It reasoned its way around the
rule — which is what prompts permit and code does not. Everything here is a
fact the database can settle, so nothing here asks a model.

Two entry points:
    precheck_sql(sql)   before running   — catch a query that cannot match
    diagnose(sql)       after 0 rows     — find WHICH filter emptied it
"""

import re
from datetime import date, datetime

import sqlglot
from sqlglot import exp

import profile_db
import tools

DIALECT = "duckdb"
NOW_FUNCS = re.compile(r"\b(current_date|current_timestamp|now\s*\(\s*\)|today\s*\(\s*\))\b", re.I)


def _latest_data_date() -> tuple[str, str] | None:
    """A date column to anchor windows to, and how far it reaches.

    Staleness is judged on the LATEST timestamp anywhere — if any column
    reaches today, a `current_date` filter may be legitimate. But the column we
    *name* in the advice is the canonical event date when there is one:
    pointing a model at `shipping_limit_date` because it happens to have the
    furthest max is technically true and useless.
    """
    bounds = profile_db.date_bounds()
    if not bounds:
        return None
    newest = max(hi for _lo, hi in bounds.values())
    preferred = next(
        (k for k in bounds if "purchase" in k.lower()),
        max(bounds.items(), key=lambda kv: kv[1][1])[0],
    )
    return preferred, bounds[preferred][1], newest


def precheck_sql(sql: str) -> list[str]:
    """Problems visible without running the query.

    Currently one, and it is the exact bug we observed: a filter anchored to
    *today* against a dataset that ended years ago. `current_date - INTERVAL
    3 months` is perfectly good SQL and guaranteed to return nothing here.
    """
    problems = []
    if NOW_FUNCS.search(sql):
        latest = _latest_data_date()
        if latest:
            col, col_hi, newest = latest
            try:
                stale_days = (date.today() - datetime.fromisoformat(newest).date()).days
            except ValueError:
                stale_days = 0
            if stale_days > 90:
                table, _, column = col.partition(".")
                problems.append(
                    f"The query is anchored to today, but this dataset ends "
                    f"{stale_days} days ago ({col} runs to {col_hi}), so a "
                    f"relative window matches nothing. Anchor to the latest date "
                    f"IN THE DATA instead, e.g. "
                    f"(SELECT max({column}) FROM {table})."
                )
    return problems


STRIP = ("group", "having", "order", "limit", "offset", "distinct",
         "qualify", "windows")


def _row_probe(tree: exp.Expression, where: exp.Expression | None) -> str | None:
    """`SELECT count(*)` over the same FROM/JOINs, with a chosen WHERE.

    Counting the *result* is useless — an aggregate returns one row whether or
    not anything matched. What we need is how many source rows survive the
    filters, so the SELECT list, GROUP BY, HAVING and LIMIT are all discarded.

    Built by copying the original tree and stripping it, rather than assembling
    a new SELECT: copying carries FROM, JOINs and CTEs across for free, and
    reassembling them by hand silently loses clauses.
    """
    if not isinstance(tree, exp.Select):
        return None                      # UNION and friends: not worth guessing
    t = tree.copy()
    for key in STRIP:
        t.set(key, None)
    t.set("expressions", [exp.Count(this=exp.Star())])
    t.set("where", exp.Where(this=where.copy()) if where is not None else None)
    return t.sql(dialect=DIALECT)


def _count(sql: str | None) -> int | None:
    if sql is None:
        return None
    r = tools.run_sql(sql)
    if not r.get("ok") or not r.get("rows"):
        return None
    return r["rows"][0][0]


def diagnose(sql: str) -> dict:
    """A zero-row result is ambiguous: no such data, or a wrong query.

    Drop one AND-ed condition at a time and see where rows appear. That
    localizes the emptiness to a specific filter — or shows the data genuinely
    isn't there, which is a real answer rather than a shrug.
    """
    try:
        tree = sqlglot.parse_one(sql, read=DIALECT)
    except Exception as e:
        return {"ok": False, "error": f"could not parse SQL: {e}"}

    where = tree.find(exp.Where)
    unfiltered = _count(_row_probe(tree, None))

    if where is None:
        return {"ok": True, "conditions": [], "unfiltered": unfiltered,
                "culprits": [],
                "summary": (f"No filters, and the source rows total "
                            f"{unfiltered:,}." if unfiltered
                            else "The source tables are empty for this join.")}

    cond = where.this
    parts = list(cond.flatten()) if isinstance(cond, exp.And) else [cond]

    findings, culprits = [], []
    for i, dropped in enumerate(parts):
        rest = [p for j, p in enumerate(parts) if j != i]
        merged = None
        for p in rest:
            merged = p.copy() if merged is None else exp.And(this=merged, expression=p.copy())
        n = _count(_row_probe(tree, merged))

        # Also ask what this condition matches ON ITS OWN. Zero means the value
        # simply is not in the data — `city = 'Lisbon'` against a Brazilian
        # dataset. That is unfixable, and telling the model to "fix the filter"
        # burns every retry on a question no rewrite can answer. Non-zero means
        # the condition is fine alone and only fails in combination, which IS
        # worth rewriting.
        alone = _count(_row_probe(tree, dropped))
        text = dropped.sql(dialect=DIALECT)
        findings.append({"dropped": text, "rows_without_it": n,
                         "rows_matching_it_alone": alone})
        if n:                                  # rows appear once this one is gone
            culprits.append({"condition": text, "rows_without_it": n,
                             "value_absent": alone == 0})

    if culprits:
        worst = max(culprits, key=lambda c: c["rows_without_it"])
        if worst["value_absent"]:
            summary = (
                f"Nothing in the data matches `{worst['condition']}` at all — not "
                f"in combination, and not on its own. The value asked for does "
                f"not exist here. ({worst['rows_without_it']:,} rows match every "
                f"other filter.)"
            )
        elif len(culprits) == 1:
            summary = (
                f"The result is empty because of `{worst['condition']}` — without "
                f"it {worst['rows_without_it']:,} rows match. Every other filter "
                f"is fine."
            )
        else:
            summary = (
                f"Removing any of {len(culprits)} conditions brings rows back; the "
                f"biggest is `{worst['condition']}` ({worst['rows_without_it']:,} rows)."
            )
    else:
        summary = (
            f"No single filter is at fault — the combination genuinely matches "
            f"nothing, out of {unfiltered:,} source rows."
            if unfiltered else "The joined tables produce no rows at all."
        )

    return {"ok": True, "conditions": findings, "unfiltered": unfiltered,
            "culprits": culprits, "summary": summary}


if __name__ == "__main__":
    bad = ("SELECT c.customer_unique_id, SUM(oi.price) AS spend "
           "FROM orders o JOIN order_items oi ON o.order_id = oi.order_id "
           "JOIN customers c ON o.customer_id = c.customer_id "
           "WHERE o.order_status = 'delivered' "
           "AND o.order_purchase_timestamp >= current_date - INTERVAL 3 MONTH "
           "GROUP BY 1 ORDER BY spend DESC LIMIT 3")
    print("precheck:", precheck_sql(bad), "\n")
    d = diagnose(bad)
    for c in d["conditions"]:
        print(f'  without {c["dropped"]:<60} -> {c["rows_without_it"]}')
    print("\n", d["summary"])
