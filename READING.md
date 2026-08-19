# Reading the backend

A guided path through ~1,600 lines of Python, bottom-up, so nothing references
something you haven't seen yet. Four sessions, about two hours total.

Each session lists **what to look for** and a **question to answer**. If you can
answer the question without re-reading, move on.

```
  488 lines   Session 1 — the data layer      db.py · profile_db.py · tools.py
  446 lines   Session 2 — the agent           graph.py                 ← the core
  180 lines   Session 3 — verification        checks.py
  373 lines   Session 4 — the edges           config.py · eval · server.py · trace.py
```

---

## The map

| File | Lines | What it is |
|---|---|---|
| `db.py` | 241 | Builds the DuckDB database from Kaggle CSVs, or generates a synthetic stand-in |
| `profile_db.py` | 133 | Computes value profiles once at build time — no LLM |
| `tools.py` | 114 | The only path to data: read-only SQL, schema text, query timeout |
| `graph.py` | 446 | **The agent** — state, nodes, routers, cycles |
| `checks.py` | 180 | Deterministic verification: precheck and empty-result diagnosis |
| `config.py` | 120 | Per-role model routing across ollama / bedrock / openrouter / openai / anthropic |
| `eval/run_eval.py` | 123 | Execution-accuracy scoring |
| `server.py` | 130 | FastAPI + SSE, streams every node to the browser |
| `trace.py` | 109 | Replays a run including every prompt |
| `static/index.html` | 405 | Glass-box UI and live graph view (skip unless you want the frontend) |

---

## Session 1 — the data layer  ·  ~30 min

What the agent can see, and what it is allowed to touch.

### `db.py` — skim only

- [ ] `CSV_TABLES` — the nine Olist files and the short table names they become
- [ ] `build()` — real CSVs if present, synthetic generation otherwise
- [ ] **`connect()`** — read the two-line docstring. `read_only=True` is the line
      that makes the database a trustworthy oracle rather than something the
      agent can corrupt.

Ignore `_generate()` — 100 lines of synthetic data, interesting only if you
lose the Kaggle download.

### `profile_db.py` — read properly

- [ ] `_col_profile()` — for each column: values if low-cardinality, min/max if a
      date or number, null rate always
- [ ] `as_text()` — how that renders into the ~1,000 tokens the prompt carries
- [ ] `date_bounds()` — used later by `checks.py`; note who consumes it

> **Question:** why is the threshold `COUNT(DISTINCT) ≤ 25`, and what class of
> failure appears if you skip profiling and send only the schema?

### `tools.py` — read properly

- [ ] `FORBIDDEN` — the regex that rejects DDL and DML
- [ ] `run_sql()` — the thread + `con.interrupt()` timeout, and why it exists
- [ ] `result_preview()` — deliberately small; the verifier needs shape, not data

> **Question:** why does `run_sql` return DuckDB's raw error string rather than a
> friendly message? (The answer is the whole revise loop.)

---

## Session 2 — the agent  ·  ~45 min  ·  the core

Read `graph.py` in four passes, **not** top to bottom.

### Pass 1 — state (`class S`, ~line 101)

- [ ] Which three fields use the `append` reducer, and why those three
- [ ] `pending` — how a node that detects a problem hands its explanation on
- [ ] `_norm()` and the comment about strings vs tuples in checkpointed state

Nothing else in the file makes sense before this.

### Pass 2 — nodes

Each is state in → **partial** update out. Never mutates.

- [ ] `write_sql` — builds the prompt from schema + glossary + accumulated
      feedback, then runs `precheck_sql` before anything is executed
- [ ] `execute` — runs it, records the SQL fingerprint in `tried`
- [ ] `diagnose_empty` — pure Python, no model call. Ask yourself why.
- [ ] `verify` — the only node that asks a model for a judgment
- [ ] `revise` — the single way back to `write_sql`
- [ ] `answer` — carries the diagnosis and any failed verdict into the reply

### Pass 3 — routers (~30 lines, all the control flow)

- [ ] `after_write` · `after_execute` · `after_diagnose` · `after_verify`
- [ ] `_is_empty()` — why `row_count == 0` is not sufficient

### Pass 4 — `build_graph()`

- [ ] How the routers wire to nodes
- [ ] The `path_map` on every conditional edge, and the comment explaining that
      without it LangGraph can only see 2 of 16 edges

> **Question:** four different things can reject an attempt — a precheck, a SQL
> error, an empty-result diagnosis, a verifier critique. Trace how each one
> reaches `write_sql` through a single `revise` node.

---

## Session 3 — verification  ·  ~25 min

`checks.py` is the most interesting code here, and the shortest.

- [ ] `precheck_sql()` — catches a query anchored to `current_date` against data
      that ended in 2018, **before** spending a database round trip
- [ ] `_latest_data_date()` — read the docstring on why it names the *canonical*
      date column rather than the one with the furthest max
- [ ] `_row_probe()` — the comment on copying the parsed tree instead of
      assembling a new SELECT (this was a real bug)
- [ ] `diagnose()` — progressive relaxation: drop one AND-ed filter at a time

> **Question:** why must `diagnose` count *source* rows rather than result rows?
> Hint: what does `SELECT SUM(price) ... WHERE city = 'Lisbon'` return when
> nothing matches?

---

## Session 4 — the edges  ·  ~30 min

- [ ] **`config.py`** — `model_name()` resolves per call, not at import. The
      comment says why; it was a bug that would have silently invalidated any
      model comparison.
- [ ] **`eval/run_eval.py`** — `rows_match()` is the function that matters:
      numeric tolerance, order-insensitivity, and the subset rule. Read the
      docstring on why results are compared and never SQL text.
- [ ] **`server.py`** — the queue + heartbeat pattern, and why LangGraph's
      `stream()` alone made the page look frozen
- [ ] **`trace.py`** — skim; it renders what `graph.py` already records

---

## Self-test

You understand the backend when you can answer these cold:

1. Where would you enforce that a given user cannot query the `customers`
   table? Name the file and the function.
2. A question returns one row containing `NULL`. Trace the exact node path from
   `execute` to `answer`.
3. Why does `verify` run last rather than first?
4. You want a "cost budget per question" guard. Which node detects the breach,
   and which router acts on it?
5. The agent writes the same SQL twice. What stops it, and why is the query not
   simply run again?

---

## Then: the first real change

**Fix the reserved-word alias bug.**

Bedrock Qwen3-32B scores 13/17, and three of the four failures are one bug: it
aliases a CTE as `do`, a reserved DuckDB keyword. DuckDB reports
`syntax error at or near "do"`, which does not tell the model *why*, so the
revise loop burns all three attempts making the identical mistake.

The prompt already says *"never alias a table with a reserved word (or, is, in,
as...)"* and the model does not generalize from the examples. So the fix belongs
in code, not in the prompt — which is the whole thesis of this project.

Where it goes:

- `checks.py` → `precheck_sql()` — detect a reserved word used as an alias and
  return a message that names the alias and suggests a replacement
- rerun `eval/run_eval.py` and watch 13/17 move

Small, real, measurable, and it touches both files that matter.
