"""Glass-box web UI: ask a question, watch the agent think.

Every node emits an event as it completes and the page renders it live, so the
process is visible rather than hidden behind a spinner — the SQL it wrote, the
rows it got, the verifier's verdict, every revision, and the token cost of each
model call.

    .venv/bin/python server.py      →  http://127.0.0.1:8000
"""

import asyncio
import json
import os
import threading
import time
import traceback
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse

import graph
import tools
from config import active_models

ROOT = Path(__file__).resolve().parent
app = FastAPI(title="data-agent")

# A run is long (seconds to minutes of model calls) and mostly waiting. Two
# limits keep it from harming a host application it is mounted into:
#
#   MAX_CONCURRENT  caps how many runs exist at once, so the process cannot
#                   accumulate unbounded threads and model spend.
#   RUN_TIMEOUT_S   caps wall clock, so one wedged model call cannot hold a
#                   slot forever.
#
# The endpoint itself is `async def`: a sync endpoint would occupy one of
# anyio's 40 shared threadpool slots for the whole run, and 40 concurrent
# questions would stall every other endpoint in the host app.
MAX_CONCURRENT = int(os.environ.get("GLASSBOX_MAX_CONCURRENT", "4"))
RUN_TIMEOUT_S = float(os.environ.get("GLASSBOX_RUN_TIMEOUT", "300"))
_slots = asyncio.Semaphore(MAX_CONCURRENT)


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/meta")
def meta():
    import db

    return {"models": active_models(), "schema": tools.schema_text(),
            "data_mode": db.build(), "examples": EXAMPLES}


@app.get("/api/graph")
def graph_structure():
    """Nodes and edges of the compiled graph — the UI draws this, so the
    diagram is generated from the real graph and cannot drift from the code."""
    return graph.structure()


EXAMPLES = [
    "What is the total revenue from delivered orders?",
    "Which product category has the worst average review score?",
    "Top 5 states by number of delivered orders",
    "What is the average delivery delay in days versus the estimate?",
    "Which payment type is most common, and what share of orders is it?",
    "Monthly revenue for 2018",
    "How much revenue came from customers in Lisbon?",
]


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, default=str)}\n\n"


@app.get("/api/ask")
async def ask(q: str):
    """Server-sent events, one per graph node as it finishes.

    The graph is blocking, so it runs on its own thread and hands results back
    through an asyncio.Queue via call_soon_threadsafe. The consumer here is
    pure async — it never occupies a threadpool slot, which is what makes this
    safe to mount alongside other APIs.
    """

    async def gen():
        loop = asyncio.get_running_loop()
        bus: asyncio.Queue = asyncio.Queue()
        totals = {"in": 0, "out": 0, "secs": 0.0, "llm_calls": 0}
        final: dict = {}

        def produce():
            try:
                for node, update in graph.stream(q):
                    loop.call_soon_threadsafe(bus.put_nowait, ("node", node, update))
            except Exception as e:
                traceback.print_exc()
                loop.call_soon_threadsafe(
                    bus.put_nowait, ("error", None, f"{type(e).__name__}: {e}")
                )
            finally:
                loop.call_soon_threadsafe(bus.put_nowait, None)

        if _slots.locked() and _slots._value <= 0:
            yield _sse("tick", {"after": "queued", "secs": 0})

        async with _slots:
            threading.Thread(target=produce, daemon=True).start()
            yield _sse("start", {"question": q})

            started = time.time()
            last_node, since = "starting", time.time()
            while True:
                if time.time() - started > RUN_TIMEOUT_S:
                    yield _sse("error", {"error": f"run exceeded {RUN_TIMEOUT_S:.0f}s"})
                    return
                try:
                    item = await asyncio.wait_for(bus.get(), timeout=2.0)
                except asyncio.TimeoutError:
                    yield _sse("tick", {"after": last_node,
                                        "secs": round(time.time() - since)})
                    continue
                if item is None:
                    break
                kind, node, payload = item
                if kind == "error":
                    yield _sse("error", {"error": payload})
                    return
                for ev in payload.get("events", []):
                    u = ev.get("usage")
                    if u:
                        totals["in"] += u.get("in", 0)
                        totals["out"] += u.get("out", 0)
                        totals["secs"] += u.get("secs", 0)
                        totals["llm_calls"] += 1
                    yield _sse("node", ev)
                for key in ("sql", "answer", "verdict", "critique"):
                    if payload.get(key) is not None:
                        final[key] = payload[key]
                last_node, since = node, time.time()
            yield _sse("done", {**final, "totals": totals})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    import uvicorn

    print("→ http://127.0.0.1:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
