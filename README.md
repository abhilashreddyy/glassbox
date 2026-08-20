# glassbox

**A data-analyst agent that verifies its own answers — and shows you the evidence.**

Ask a business question in English. The agent grounds itself in the real data,
writes SQL, runs it, **checks whether the result actually answers the question**,
diagnoses empty results instead of shrugging at them, revises when a check fails,
and reports back with every step, token, and revision visible.

Most text-to-SQL demos show you an answer. This one shows you *why it believes*
the answer — and tells you when it doesn't.

```
START → write_sql → (repeat? → nudge ↩) → execute ─rows→ verify ─ok→ answer → END
            ▲                              │  │                │
            │                              │  └─0 rows→ diagnose_empty
            └─────────── revise ───────────┴───────────┴────────┘  (attempts ≤ 3)
```

Built on LangGraph over the real 100k-order [Olist e-commerce
dataset](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce) in DuckDB.
Runs entirely on a local 20B model, or on any API model, per node.

---

## Why this exists

An LLM that writes SQL is easy. The problem is that **nothing checks the
answer.** A model can produce SQL that parses, runs, returns a clean number, and
answers a subtly different question than the one asked. The user sees a
confident number and has no reason to doubt it.

Every wrong answer this project produced was of exactly that kind — the SQL was
never broken. So the engineering went into the checks, not the prompt.

## Results

Execution accuracy on 17 hand-written gold questions, real Olist data.

| Configuration | Accuracy | Wall clock | Cost |
|---|---|---|---|
| `gpt-oss:20b` local, schema only (8 questions) | 7/8 — 87.5% | — | $0 |
| `gpt-oss:20b` local, schema only | 14/17 — 82.4% | — | $0 |
| **`gpt-oss:20b` local, full grounding** | **17/17 — 100%** | 548s | $0 |
| `qwen3-32b` on Bedrock, before alias repair | 13/17 — 76.5% | 46s | $0.015 |
| **`qwen3-32b` on Bedrock, after alias repair** | **16/17 — 94.1%** | **53s** | **$0.015** |

A local 20B is *more accurate* than a hosted 32B here; the hosted one is **10x
faster** for a cent and a half. Neither is strictly better — which is why each
node resolves its own model.

### Read the 100% with suspicion

Three of those seventeen were failing until three glossary entries were added,
written *after seeing exactly which questions failed and why*. That is fitting to
the test set, and a number produced that way does not measure how the agent
handles a question it has never seen.

What it honestly shows: **every failure was definitional, and a definition fixed
it.** The clean measurement is a held-out set — written without looking at any
failure, run once. Until then, 100% means "the known failure modes are closed",
not "accuracy".

### The alias repair: 13/17 → 16/17

Qwen aliased a CTE `do` — the natural short form of `delivered_orders`, and a
reserved SQL keyword. DuckDB reports only `syntax error at or near "do"`, which
never says *why*, so the revise loop burned all four attempts reproducing the
same alias. Three questions failed this way.

The prompt already said *"never alias a table with a reserved word"* with
examples. The model did not generalise from them — the same lesson as the revenue
glossary: **a rule in a prompt is a suggestion.**

Two changes, at both ends:

- **Prevention** — the prompt now asks for a trailing underscore on every alias
  (`o_`, `do_`), which makes a keyword collision impossible by construction.
- **Repair** — reserved words come from `duckdb_keywords()` (nothing hardcoded,
  nothing to go stale), and sqlglot renames any offending alias and every
  qualified reference *before* the query runs.

Renaming an alias cannot change what a query means, so there is nothing to
consult the model about: no retry, no extra model call. The rename is recorded in
the event stream rather than applied silently — a glass box that quietly rewrites
its own inputs would be worse than one that fails honestly.

All three questions now pass with **zero** retries.

### The last failure is the verifier working

`repeat_customers` asks how many customers placed more than one order. Gold is
2,997. Qwen returned **0** three times: it knew from the glossary that it needed
`customer_unique_id`, but never worked out that the column lives on `customers`
and requires a join, so it grouped `orders.customer_id` — unique per order.

The verifier rejected all three, each time with an accurate reason, **without
ever seeing the gold answer**. A confident "0 repeat customers" would otherwise
have shipped. `gpt-oss:20b` answers this one correctly.

## What the failures taught

Every failure was **definitional, not syntactic** — the query ran and answered a
slightly different question:

| Question | What went wrong |
|---|---|
| *total revenue from delivered orders* | summed `payment_value` (15,422,461.77) where an analyst means `order_items.price` (13,221,498.11) — the gap is freight |
| *revenue from customers in Lisbon* | `SUM()` over zero rows returns one row containing `NULL`; the agent reported "no revenue records exist" and **the verifier approved it** |
| *highest-revenue calendar month* | grouped by delivery date instead of purchase date |
| *average review score for late deliveries* | used `date_diff(day) > 0`, so an order two hours late counted as on time |
| *share of reviews with a comment* | returned `0.41` where the analyst wanted `41.3` |
| *distinct sellers who sold ≥1 item* | agent applied the glossary; **the gold SQL was wrong.** Kept as a regression test |

Two conclusions drove the architecture:

1. **Business-term ambiguity is the enemy, not SQL syntax.** The fix is a
   glossary lookup sent to the writer *and* the checker — not a bigger model.
2. **A verifier that can't see the rules can't enforce them.** Asked about an
   empty result, the LLM verifier replied: *"An empty result set may simply
   indicate no qualifying data, which is acceptable given the question."* Its
   prompt told it to reject empty results. It reasoned around the rule — which is
   what prompts permit and code does not.

## Architecture

**The model decides what to try; code decides what happens next.** Every routing
decision is a deterministic function reading state. No LLM output routes
anything.

### Grounding — three tiers, cheapest first

Correct SQL is impossible against values you have never seen.

1. **Schema** — tables, columns, types.
2. **Value profile** ([`profile_db.py`](profile_db.py)) — computed once at build
   time, no LLM: every low-cardinality column's actual values with counts,
   min/max for dates and numbers, null rates. ~1,000 tokens.
   ```
   order_status VARCHAR = delivered(96,478), shipped(1,107), canceled(625), …
   order_purchase_timestamp TIMESTAMP range 2016-09-04 … 2018-10-17
   ```
3. **Glossary** ([`glossary.yaml`](glossary.yaml)) — house definitions, sent to
   the writer *and* the verifier.

### Verification — three layers, least fallible first

1. **The database.** SQL that doesn't parse is rejected in milliseconds, and
   DuckDB's error names the offending token. Fed back verbatim, because it is the
   teaching signal.
2. **Deterministic checks** ([`checks.py`](checks.py)) — no model involved:
   - **precheck**, before execution: a query anchored to `current_date` against
     data that ended in 2018 cannot match anything. Caught without running it.
   - **diagnose**, after an empty result: drop one `AND`-ed filter at a time via
     `sqlglot` and see where rows reappear, localizing the emptiness.
     ```
     without o.order_status = 'delivered'                    → 0
     without o.order_purchase_timestamp >= CURRENT_DATE - 3M  → 110,197   ← culprit
     ```
     The user then hears *"Springfield has 1,204 delivered orders, but none after
     2018-10-17"* instead of *"no data"*.
3. **The LLM verifier** — last, for the judgment call only, with the glossary so
   it has a rulebook rather than an opinion.

A zero-row result is ambiguous: it means *no such data* or *my query is wrong*,
and those are indistinguishable without a second check. Treating it as either one
without evidence is how confident wrong answers get made.

## Inference workload profile

Every model call records prompt tokens, completion tokens, and latency. Measured
over local runs on `gpt-oss:20b` (24 GB Apple Silicon, unified memory):

| Role | Job | Median in | Median out | Median latency | tok/s |
|---|---|---|---|---|---|
| `sql_writer` | schema reasoning + correct SQL | 2,080 | 622 | 17.6s | 35.4 |
| `verifier` | does this SQL answer this question? | 854 | 284 | 5.9s | 48.1 |
| `answerer` | result table → sentence | 213 | 177 | 5.3s | 33.6 |

The profile is deliberately lopsided: one hard call carries a 2k-token grounded
prompt and long reasoning output, while two cheap calls handle judgment and
prose. That asymmetry is why **each role resolves its own model**:

```bash
export DATA_AGENT_MODEL_DEFAULT=ollama:gpt-oss:20b            # all local
export DATA_AGENT_MODEL_SQL_WRITER=anthropic:claude-sonnet-5  # upgrade one role
```

Which makes *"how much model does each part actually need?"* a measurement rather
than an opinion: change one role, rerun the eval, compare accuracy against tokens
and latency.

Memory is measured too. `num_ctx=16384` holds 15 GB resident against 14 GB at
8192, while the largest real prompt is ~2,100 tokens — on a 24 GB machine that
unused gigabyte decides whether a run swaps.

## Evaluation

**Execution accuracy**: a human-written gold SQL is run, the agent's SQL is run,
and the *results* are compared — never the SQL text, since many correct queries
answer one question.

Matching rules: numeric tolerance, column names ignored, row order ignored unless
the question is a ranking, and gold values must appear in the agent's rows (extra
columns are fine, but row counts must match, so nothing passes by dumping the
whole table).

Two numbers, and the gap between them is the interesting one:

- **accuracy** — share of runs matching gold
- **pass^k** — share of questions correct on *every* run, which is what
  stochastic agents actually deliver

```bash
.venv/bin/python eval/run_eval.py         # single pass
.venv/bin/python eval/run_eval.py -k 3    # reliability
```

## Observability

Every run writes a full trace: each node, and each model call's system prompt,
user prompt, and raw reply verbatim.

```bash
.venv/bin/python trace.py       # last run: nodes, then every prompt and reply
.venv/bin/python trace.py -v    # untruncated
.venv/bin/python trace.py -l    # list runs
```

The web UI renders the same thing live, including a graph view generated from the
**compiled LangGraph** — so the diagram cannot drift from the code — that lights
up nodes and numbers edges as they fire.

> Getting that diagram required declaring `path_map`s on every conditional edge.
> Without them LangGraph can only see 2 of 16 edges, because a router is an
> arbitrary function; this is why many LangGraph diagrams render uselessly sparse.

## Running it

```bash
uv venv --python 3.12
uv pip install duckdb fastapi uvicorn langgraph langchain-core langchain-ollama \
               pyyaml sqlglot kaggle
.venv/bin/python server.py          # → http://127.0.0.1:8000
```

Local models via [Ollama](https://ollama.com) (`ollama pull gpt-oss:20b`), or set
`DATA_AGENT_MODEL_*` to any OpenAI/Anthropic model.

**Data.** With a Kaggle token at `~/.kaggle/kaggle.json`:

```bash
.venv/bin/kaggle datasets download -d olistbr/brazilian-ecommerce -p data/olist --unzip
.venv/bin/python db.py && .venv/bin/python profile_db.py
```

Without credentials, `db.py` generates a synthetic dataset with identical table
and column names, so everything runs unchanged.

## Layout

```
graph.py          the agent — nodes, routers, guarded cycles
checks.py         deterministic verification (precheck, empty-result diagnosis)
profile_db.py     value profiles computed at build time
glossary.yaml     house definitions of business terms
tools.py          read-only SQL, schema text, 25s query cap
config.py         per-role model routing
server.py         FastAPI + SSE, streams every node to the browser
static/index.html live graph view and glass-box UI
eval/             gold questions + execution-accuracy harness
trace.py          replay any run, prompts included
```

Safety by construction: read-only connection, DDL/DML rejected, results
row-capped, and a query timeout via `con.interrupt()` — one accidental join
against the 1M-row geolocation table would otherwise block a run indefinitely.

See **[DESIGN.md](DESIGN.md)** for the full architecture, including the planned
`plan` / `resolve` / `clarify` nodes and why they aren't built yet.

## What's next

Ordered by expected value, not by novelty:

1. **A held-out question set** — the honest accuracy number, and the reason not to
   trust the current one.
2. **Ground the verifier further** — give it row counts and date ranges, and let
   it run its own probe queries. It is currently an opinion; this makes it an
   investigation.
3. **`plan` / `resolve` nodes** — resolve high-cardinality values (thousands of
   cities) and ambiguous terms *before* generating SQL, with `interrupt()` to ask
   the user only when the answer would materially change.

The evidence so far says definitions beat machinery, which is exactly why #1
comes before #3.

---

MIT licensed. Dataset: [Olist Brazilian E-Commerce](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce) (CC BY-NC-SA 4.0).
