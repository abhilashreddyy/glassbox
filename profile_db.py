"""Value profiles: what the agent needs to know that the schema doesn't say.

A schema says `order_status VARCHAR`. It does not say the values are
'delivered'/'shipped'/'canceled', or that the data stops on 2018-10-17. Correct
SQL is impossible against values you have never seen — that blindness is what
produced the empty-result failure.

This computes the missing half once, deterministically, with no LLM involved,
and caches it beside the database.

    .venv/bin/python profile_db.py          # rebuild and print
"""

import json
from pathlib import Path

import db

CACHE = db.DATA / "profile.json"

LOW_CARD = 25      # list every value at or below this
SAMPLE_CARD = 300  # above LOW_CARD but below this: show the commonest few
SAMPLE_N = 8

NUMERIC = ("INT", "DOUBLE", "DECIMAL", "FLOAT", "BIGINT", "HUGEINT")
TEMPORAL = ("DATE", "TIMESTAMP", "TIME")


def _col_profile(con, table: str, col: str, dtype: str, rows: int) -> dict:
    out: dict = {"type": dtype}
    nulls = con.execute(
        f'SELECT count(*) FROM "{table}" WHERE "{col}" IS NULL'
    ).fetchone()[0]
    if nulls:
        out["null_pct"] = round(100 * nulls / rows, 1) if rows else 0.0

    up = dtype.upper()
    if any(t in up for t in TEMPORAL) or any(t in up for t in NUMERIC):
        lo, hi = con.execute(
            f'SELECT min("{col}"), max("{col}") FROM "{table}"'
        ).fetchone()
        if lo is not None:
            out["min"], out["max"] = str(lo), str(hi)
        return out

    n = con.execute(
        f'SELECT count(DISTINCT "{col}") FROM "{table}"'
    ).fetchone()[0]
    out["distinct"] = n
    if 0 < n <= LOW_CARD:
        out["values"] = [
            {"v": str(v), "n": c}
            for v, c in con.execute(
                f'SELECT "{col}", count(*) c FROM "{table}" '
                f'WHERE "{col}" IS NOT NULL GROUP BY 1 ORDER BY c DESC'
            ).fetchall()
        ]
    elif n <= SAMPLE_CARD:
        out["top"] = [
            str(v)
            for (v,) in con.execute(
                f'SELECT "{col}" FROM "{table}" WHERE "{col}" IS NOT NULL '
                f"GROUP BY 1 ORDER BY count(*) DESC LIMIT {SAMPLE_N}"
            ).fetchall()
        ]
    return out


def build(force: bool = False) -> dict:
    """Profile every column. Cached — this is many queries, and the answer only
    changes when the database is rebuilt."""
    if CACHE.exists() and not force:
        if CACHE.stat().st_mtime >= db.DB_PATH.stat().st_mtime:
            return json.loads(CACHE.read_text())

    con = db.connect()
    try:
        prof: dict = {}
        for (table,) in con.execute("SHOW TABLES").fetchall():
            rows = con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            cols = {}
            for name, dtype, *_ in con.execute(f'DESCRIBE "{table}"').fetchall():
                cols[name] = _col_profile(con, table, name, dtype, rows)
            prof[table] = {"rows": rows, "columns": cols}
    finally:
        con.close()

    CACHE.write_text(json.dumps(prof, indent=1))
    return prof


def as_text(prof: dict | None = None) -> str:
    """Render for the prompt. Compact — this is sent on every SQL-writing call."""
    prof = prof or build()
    out = []
    for table, meta in prof.items():
        out.append(f'{table} ({meta["rows"]:,} rows)')
        for col, c in meta["columns"].items():
            bits = [c["type"]]
            if "values" in c:
                bits.append(
                    "= " + ", ".join(f'{v["v"]}({v["n"]:,})' for v in c["values"])
                )
            elif "top" in c:
                bits.append(f'{c["distinct"]:,} distinct, common: '
                            + ", ".join(c["top"]))
            elif "distinct" in c:
                bits.append(f'{c["distinct"]:,} distinct values')
            if "min" in c:
                bits.append(f'range {c["min"]} … {c["max"]}')
            if c.get("null_pct"):
                bits.append(f'{c["null_pct"]}% null')
            out.append(f"  {col}: " + "  ".join(bits))
    return "\n".join(out)


def date_bounds(prof: dict | None = None) -> dict:
    """{'orders.order_purchase_timestamp': (min, max), ...} — used by the
    deterministic checks to catch a filter that can't match anything."""
    prof = prof or build()
    return {
        f"{t}.{col}": (c["min"], c["max"])
        for t, meta in prof.items()
        for col, c in meta["columns"].items()
        if "min" in c and any(x in c["type"].upper() for x in TEMPORAL)
    }


if __name__ == "__main__":
    p = build(force=True)
    text = as_text(p)
    print(text)
    print(f"\n--- {len(text)} chars ≈ {len(text)//4} tokens ---")
