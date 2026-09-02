"""FastAPI entry point — SSE chat over the multi-agent orchestrator.

Endpoints (mirror the reference API so the UI works unchanged):
  POST /chat      — Server-Sent Events stream of routing/tool/specialist/final events.
  GET  /agents    — list configured specialists.
  GET  /sessions  — list known chat sessions (from MongoDB memory).
  GET  /health    — liveness + effective config.
"""
from __future__ import annotations

import json
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse
from starlette.concurrency import iterate_in_threadpool

from . import memory, telemetry
from .config_loader import load_agents
from .orchestrator import run_turn
from .settings import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="MongoDB × Azure Foundry — Multi-Agent Career Assistant", version="1.0.0")

# Configure Azure Monitor / OpenTelemetry (no-op if no connection string is set).
telemetry.configure_telemetry(app)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # demo/no-auth; tighten for production
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"
    user_id: str = "demo-user"


@app.on_event("startup")
def _startup() -> None:
    try:
        memory.ensure_indexes()
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not ensure memory indexes at startup: %s", e)


@app.get("/health")
def health() -> dict:
    s = get_settings()
    return {
        "status": "ok",
        "specialist_mode": s.specialist_mode,
        "classifier_backend": s.classifier_backend,
        "chat_deployment": s.chat_deployment,
        "embedding_model": s.embedding_model,
        "mongodb_db": s.mongodb_db,
        "mcp_url": s.mongodb_mcp_url,
        "mcp_configured": bool(s.mongodb_mcp_url),
        "mcp_api_key_configured": bool(s.mcp_api_key),
        "telemetry_enabled": telemetry.is_enabled(),
    }


@app.get("/metrics")
def metrics() -> dict:
    """Aggregate in-process metrics for the UI dashboard (turns, latency, tokens)."""
    return telemetry.snapshot()


@app.get("/agents")
def agents() -> dict:
    ags = load_agents()
    return {
        "agents": [
            {
                "name": a.name,
                "description": a.description,
                "routable": a.routable,
                "tools": a.tools,
                "collections": a.collections,
            }
            for a in ags.values()
        ]
    }


@app.get("/sessions")
def sessions() -> dict:
    return {"sessions": memory.list_sessions()}


@app.get("/sessions/{session_id}/traces")
def session_traces(session_id: str) -> dict:
    """Full per-turn agent traces for one session (reasoning, tools, retrieval, tokens, raw events)."""
    return {"session_id": session_id, "traces": memory.get_traces(session_id)}


@app.post("/chat")
async def chat(req: ChatRequest) -> EventSourceResponse:
    """Stream orchestrator events as SSE. Each event: {event: <type>, data: <json>}."""

    def _sync_events():
        for ev in run_turn(req.session_id, req.user_id, req.message):
            yield ev

    async def event_publisher():
        async for ev in iterate_in_threadpool(_sync_events()):
            yield {"event": ev.get("type", "message"), "data": json.dumps(ev, default=str)}
        yield {"event": "done", "data": "{}"}

    return EventSourceResponse(event_publisher())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("src.app:app", host="0.0.0.0", port=get_settings().port, reload=True)
