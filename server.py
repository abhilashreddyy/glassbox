"""Glass-box web UI: ask a question, watch the agent think.

Every node emits an event as it completes and the page renders it live, so the
process is visible rather than hidden behind a spinner — the SQL it wrote, the
rows it got, the verifier's verdict, every revision, and the token cost of each
model call.

    .venv/bin/python server.py      →  http://127.0.0.1:8000
"""

import json
import queue
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
def ask(q: str):
    """Server-sent events, one per graph node as it finishes."""

    def gen():
        totals = {"in": 0, "out": 0, "secs": 0.0, "llm_calls": 0}
        final = {}

        # The graph runs in its own thread and posts to a queue, so this
        # generator can emit a heartbeat while a node is still working.
        # LangGraph only yields when a node COMPLETES, and a local model can
        # take a minute — without this the page looks frozen mid-run.
        bus: queue.Queue = queue.Queue()

        def produce():
            try:
                for node, update in graph.stream(q):
                    bus.put(("node", node, update))
            except Exception as e:
                traceback.print_exc()
                bus.put(("error", None, f"{type(e).__name__}: {e}"))
            finally:
                bus.put(None)

        threading.Thread(target=produce, daemon=True).start()
        yield _sse("start", {"question": q})

        last_node, since = "starting", time.time()
        while True:
            try:
                item = bus.get(timeout=2.0)
            except queue.Empty:
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
