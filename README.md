# glassbox

**A data analyst agent that shows its work.**

Ask a business question in English. The agent writes SQL, runs it, *checks its
own answer*, revises when the check fails, and reports the result — with every
step, every token and every revision visible in the UI.

Most text-to-SQL demos show you an answer. This one shows you why it believes
the answer, and admits when it doesn't.

```
START → write_sql → (repeat? → nudge ↩) → execute ─ok→ verify ─ok→ answer → END
            ▲                                 │            │
            └──── revise (error / critique) ──┴────────────┘   (attempts ≤ 3)
```

## Run it

```bash
.venv/bin/python server.py        # → http://127.0.0.1:8000
```

```bash
.venv/bin/python eval/run_eval.py        # score against the gold set
.venv/bin/python eval/run_eval.py -k 3   # 3 runs each — measures reliability
```

First run builds `data/olist.duckdb` automatically.

## The data

Eight relational tables shaped exactly like the Kaggle **Brazilian E-Commerce
(Olist)** dataset: `orders`, `order_items`, `customers`, `products`, `sellers`,
`order_payments`, `order_reviews`, `product_category_name_translation`.

Real joins are the point — a single flat table gives the agent nothing to
decide, and an agent that isn't deciding is just a prompt.

Without Kaggle credentials the loader generates a synthetic dataset with the
same schema, so everything runs today. **To use the real data**, download the
dataset from Kaggle and drop the CSVs into `data/olist/`, then:

```bash
.venv/bin/python db.py     # rebuilds; detects real CSVs automatically
```

Gold answers in `eval/questions.yaml` are written against the synthetic data —
recompute them after switching.

## Models are configurable per role

Three roles, genuinely different difficulty, so they don't need the same model:

| Role | Job | Difficulty |
|---|---|---|
| `sql_writer` | schema reasoning + correct SQL | hard |
| `verifier` | does this SQL answer this question? | medium |
| `answerer` | turn a result table into a sentence | easy |

```bash
export DATA_AGENT_MODEL_DEFAULT=ollama:gpt-oss:20b            # everything local
export DATA_AGENT_MODEL_SQL_WRITER=anthropic:claude-sonnet-5  # upgrade one role
export DATA_AGENT_MODEL_VERIFIER=openai:gpt-4o-mini
```

Format is `provider:model` (`ollama`, `openai`, `anthropic`). API providers need
their package installed (`uv pip install langchain-anthropic`) and the usual key
in the environment. The UI shows which model produced every run, so
*"how much model does each part actually need?"* is a measurable question:
change one role, rerun the eval, compare.

## How it's scored

**Execution accuracy.** For each question a human-written gold SQL is run, the
agent's SQL is run, and the *results* are compared — never the SQL text, since
many correct queries answer one question.

Matching rules: numeric tolerance, column names ignored, row order ignored
unless the question is a ranking, and gold values must appear in the agent's
rows (extra columns are fine, but the row count must match so nothing passes by
dumping the whole table).

Two numbers, and the gap between them is the interesting one:
- **accuracy** — share of runs that matched gold
- **pass^k** — share of questions correct on *every* run

## Baseline (all three roles on gpt-oss:20b, local)

| Metric | Value |
|---|---|
| accuracy | 7/8 = 87.5% |
| pass^1 | 7/8 = 87.5% |
| wall clock | 168s for 8 questions |

### What the failure taught us

`revenue_delivered` failed — and **the verifier passed it anyway**. That is the
false-success case, the thing this project exists to study.

The agent summed `order_payments.payment_value` (1,287,361.10); the gold sums
`order_items.price` (1,202,737.26). The difference is freight. Both queries are
defensible SQL — the disagreement is over what "revenue" *means*, and the system
prompt already states the house definition. The agent ignored it, and a verifier
looking only at question + SQL + result had no way to see the violation.

Two conclusions worth more than the score: business-term ambiguity is the real
enemy in text-to-SQL, not syntax; and a verifier that can't see the rules can't
enforce them.

Also observed live: a run where the model aliased `order_reviews AS or` — a
reserved word — DuckDB rejected it, the error was fed back, and attempt 2 fixed
the alias. Error-driven revision works.

## Layout

```
config.py         model routing per role
db.py             builds DuckDB (real Olist CSVs or synthetic)
tools.py          schema introspection + read-only SQL runner
graph.py          the LangGraph agent — nodes, routers, guarded cycles
server.py         FastAPI + SSE: streams each node to the browser
static/index.html the glass-box UI
eval/             gold questions + execution-accuracy harness
```

Safety by construction: the connection is **read-only**, DDL/DML is rejected,
and results are row-capped. The database has to stay trustworthy — it's the
oracle everything is graded against.

## Roadmap

1. **Give the verifier the rulebook.** It currently sees question + SQL +
   result. Add the metric definitions (what "revenue" means, which status
   counts as a sale) so it can catch the definitional failure above.
2. **A business-glossary tool** so the writer resolves terms instead of guessing.
3. **Row/column-level permissions.** A `profile` on each request that restricts
   which tables and columns are visible, enforced in `tools.py` — not by asking
   the model nicely. The right shape for a real deployment: an analyst sees
   salaries, a support agent doesn't.
4. **More questions.** Eight is enough to prove the harness; twenty with harder
   joins, window functions and date math is enough to trust the number.
5. **Charts** for results with a natural visual form.
