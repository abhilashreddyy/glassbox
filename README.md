# glassbox

**A data analyst agent that shows its work.**

Ask a business question in English. The agent writes SQL, runs it, *checks its
own answer*, diagnoses empty results instead of shrugging at them, revises when
a check fails, and reports back — with every step, every token and every
revision visible in the UI.

Most text-to-SQL demos show you an answer. This one shows you why it believes
the answer, and admits when it doesn't.

```
START → write_sql → (repeat? → nudge ↩) → execute ─rows→ verify ─ok→ answer → END
            ▲                              │  │                │
            │                              │  └─0 rows→ diagnose_empty
            └─────────── revise ───────────┴───────────┴────────┘  (attempts ≤ 3)
```

See [DESIGN.md](DESIGN.md) for how it works.

## Run it

```bash
.venv/bin/python server.py        # → http://127.0.0.1:8000
```

```bash
.venv/bin/python eval/run_eval.py        # score against the gold set
.venv/bin/python eval/run_eval.py -k 3   # 3 runs each — measures reliability
```

## The data

The real **Brazilian E-Commerce (Olist)** dataset from Kaggle — 9 tables:

| Table | Rows | |
|---|---|---|
| orders | 99,441 | 5-stage delivery funnel timestamps |
| order_items | 112,650 | price and freight per item |
| order_reviews | 99,224 | 1–5 score, **41% carry Portuguese free text** |
| order_payments | 103,886 | type, instalments, value |
| customers | 99,441 | zip prefix, city, state |
| products | 32,951 | category, weight, dimensions |
| sellers | 3,095 | zip prefix, city, state |
| geolocation | 1,000,163 | lat/lng per zip prefix |
| category translation | 71 | Portuguese → English |

Data spans 2016-09-04 to 2018-10-17. Real joins are the point — a single flat
table gives the agent nothing to decide, and an agent that isn't deciding is
just a prompt.

**To rebuild from scratch:** put a Kaggle token at `~/.kaggle/kaggle.json` (or
export `KAGGLE_API_TOKEN`), then:

```bash
.venv/bin/kaggle datasets download -d olistbr/brazilian-ecommerce -p data/olist --unzip
.venv/bin/python db.py && .venv/bin/python profile_db.py
```

`db.py` falls back to generating a synthetic dataset with identical table and
column names if no CSVs are present, so the project runs without credentials.

## What the agent is given

Three layers of grounding, cheapest first — because correct SQL is impossible
against values you have never seen.

1. **Schema** — tables, columns, types.
2. **Value profile** (`profile_db.py`) — computed once at build time, no LLM:
   every low-cardinality column's actual values with counts, min/max for dates
   and numbers, null rates. ~1,000 tokens.
   ```
   order_status VARCHAR = delivered(96,478), shipped(1,107), canceled(625), …
   order_purchase_timestamp TIMESTAMP range 2016-09-04 … 2018-10-17
   ```
3. **Glossary** (`glossary.yaml`) — house definitions of business terms, sent to
   the writer *and* the verifier. Revenue means `order_items.price`, "sold"
   means delivered, "share" means 0–100 not 0–1.

## Verification

Three layers, cheapest and least fallible first:

1. **The database** — SQL that doesn't parse is rejected in milliseconds, and
   DuckDB's error names the offending token. It's fed back verbatim.
2. **Deterministic checks** (`checks.py`) — no model involved:
   - **precheck**, before running: a query anchored to `current_date` against
     data that ended years ago cannot match anything. Caught pre-execution.
   - **diagnose**, after 0 rows: drop one AND-ed filter at a time and see where
     rows reappear, which localizes the emptiness to a specific condition.
3. **The LLM verifier** — last, for the part needing judgment, and given the
   glossary so it has a rulebook rather than an opinion.

## Models are configurable per role

| Role | Job | Difficulty |
|---|---|---|
| `sql_writer` | schema reasoning + correct SQL | hard |
| `verifier` | does this SQL answer this question? | medium |
| `answerer` | turn a result table into a sentence | easy |

```bash
export DATA_AGENT_MODEL_DEFAULT=ollama:gpt-oss:20b            # everything local
export DATA_AGENT_MODEL_SQL_WRITER=anthropic:claude-sonnet-5  # upgrade one role
```

Format is `provider:model` (`ollama`, `openai`, `anthropic`); API providers need
their package (`uv pip install langchain-anthropic`) and the usual key. The UI
shows which model produced every run, so *"how much model does each part
actually need?"* is measurable: change one role, rerun the eval, compare.

## How it's scored

**Execution accuracy.** A human-written gold SQL is run, the agent's SQL is run,
and the *results* are compared — never the SQL text, since many correct queries
answer one question.

Matching rules: numeric tolerance, column names ignored, row order ignored
unless the question is a ranking, and gold values must appear in the agent's rows
(extra columns are fine, but the row count must match so nothing passes by
dumping the whole table).

Two numbers, and the gap between them is the interesting one:
- **accuracy** — share of runs that matched gold
- **pass^k** — share of questions correct on *every* run

## Results

All three roles on local `gpt-oss:20b`, real Olist data, on a 24 GB Mac.

| Stage | Accuracy |
|---|---|
| 8 questions, schema only | 7/8 = 87.5% |
| 17 questions, schema only | 14/17 = 82.4% |
| 17 questions, + profiles + glossary + checks | **17/17 = 100%** |

### Read that 100% with suspicion

Three of those seventeen were failing until I added three glossary entries —
written *after seeing exactly which questions failed and why*. That is fitting
to the test set, and a number produced that way is not a measure of how the
agent handles a question it has never met.

What it does honestly show: **every failure was definitional, and a definition
fixed it.** The mechanism works. What it does not show is the hit rate on
unseen questions.

The clean measurement is a held-out set: write new questions without looking at
any failure, run once, report that. Until then, treat 100% as "the known
failure modes are closed", not as accuracy.

### What the failures taught us

Every failure this project has produced has been **definitional, not syntactic**.
The SQL runs; it answers a slightly different question.

- *"total revenue from delivered orders"* — the agent summed
  `order_payments.payment_value` (15,422,461.77) where an analyst means
  `order_items.price` (13,221,498.11). The difference is freight. Stating the
  rule in the system prompt was not enough; it took a glossary entry sent to
  both the writer and the verifier.
- *"how many distinct sellers sold at least one item"* — the agent applied the
  glossary's definition of "sold" (delivered only, 2,970) while the gold SQL
  ignored it (3,095). **The eval was wrong, not the agent.** Kept as a test that
  definitions are actually applied.
- *"which calendar month had the highest revenue"* — grouped by delivery date
  instead of purchase date.
- *"average review score for late deliveries"* — used `date_diff(day) > 0`,
  making an order two hours late count as on time.
- *"what share of reviews include a comment"* — returned 0.41 where the gold
  wanted 41.3.

The last three were fixed by adding three glossary entries. That is the whole
thesis of the project in one experiment: **business-term ambiguity is the enemy,
and the fix is a lookup both the writer and the checker can see** — not a
smarter model, and not a longer prompt.

Also observed: a run where the model aliased `order_reviews AS or` — a reserved
word — DuckDB rejected it, the error fed back, and attempt 2 fixed the alias.
Error-driven revision works.

## Seeing what the agent did

The web UI streams each step live. To see what the models were actually *sent*
— the part that explains why a query came out the way it did — replay the trace:

```bash
.venv/bin/python trace.py        # last run: nodes, then every prompt and reply
.venv/bin/python trace.py -v     # full prompts, untruncated
.venv/bin/python trace.py -l     # list saved runs
```

Traces are written to `traces/` on every run, from the UI and the CLI alike.
Local files, no service and no account — LangSmith or Langfuse would add hosted
history and cross-run comparison, but nothing you need to answer "why did it do
that?" on a single run.

## Layout

```
graph.py          the agent — nodes, routers, guarded cycles
tools.py          read-only SQL + schema text
profile_db.py     value profiles: what the schema doesn't say
checks.py         deterministic verification (precheck, diagnose)
glossary.yaml     house definitions of business terms
db.py             builds DuckDB from Olist CSVs (or synthetic)
config.py         model routing per role
server.py         FastAPI + SSE: streams each node to the browser
static/index.html the glass-box UI
eval/             gold questions + execution-accuracy harness
```

Safety by construction: the connection is **read-only**, DDL/DML is rejected, and
results are row-capped. The database has to stay trustworthy — it's the oracle
everything is graded against.

## Next

1. `plan` + `resolve` nodes — probe the data to pin ambiguous values before
   writing SQL, instead of guessing and revising.
2. `clarify` via `interrupt()` — ask the user only when the answer would
   materially change, stating the facts while asking.
3. Row/column permissions enforced in `tools.py`, with the schema filtered
   before the model sees it.
4. Hybrid text + SQL: 41% of reviews carry Portuguese comments, which is the
   only way to answer *why* a category is rated badly.
