"""Sessions — browse past chat sessions and drill into detailed agent traces.

List view: all sessions (turns, tokens, cost, latency, last message).
Detail view (?session=<id>): the full per-turn agent trace for one session —
  1. Reasoning flow
  2. Tokens & cost
  3. MCP tool calls to MongoDB Atlas
  4. Context (memory) retrieved from MongoDB Atlas
  5. Raw events (JSON)

Traces are persisted by the API to the MongoDB `agent_traces` collection and read via
`GET /sessions` and `GET /sessions/{id}/traces`.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

import requests
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from theme import apply_mongodb_theme, card, reasoning_flow  # noqa: E402

DEFAULT_API = os.getenv("API_URL", f"http://localhost:{os.getenv('PORT', '3000')}")

st.set_page_config(page_title="Sessions — Career Assistant", page_icon="🗂️", layout="wide")
apply_mongodb_theme()

api_url = st.sidebar.text_input("API URL", DEFAULT_API)


def _fmt_ts(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001
        return iso


def _get(path: str) -> dict | None:
    try:
        return requests.get(f"{api_url}{path}", timeout=15).json()
    except Exception as e:  # noqa: BLE001
        st.error(f"Cannot reach {api_url}{path}: {e}")
        return None


# --------------------------------------------------------------------------- #
# Detail view (when ?session=<id> is present)
# --------------------------------------------------------------------------- #
def render_detail(session_id: str) -> None:
    top = st.columns([1, 6])
    with top[0]:
        if st.button("← All sessions"):
            st.query_params.clear()
            st.rerun()
    with top[1]:
        st.title("🗂️ Session trace")
    st.caption(f"Session `{session_id}`")

    data = _get(f"/sessions/{session_id}/traces")
    if not data:
        return
    traces = data.get("traces", [])
    if not traces:
        st.info("No persisted traces for this session yet. Traces are recorded per turn once the "
                "updated API has handled a chat in this session.")
        return

    # Session-level roll-up
    tot_tokens = sum((t.get("tokens", {}) or {}).get("total", 0) for t in traces)
    tot_cost = sum(t.get("cost_usd", 0) or 0 for t in traces)
    tot_tools = sum(len(t.get("tool_calls", []) or []) for t in traces)
    avg_lat = (sum(t.get("latency_ms", 0) or 0 for t in traces) / len(traces)) / 1000.0
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Turns", len(traces))
    m2.metric("Total tokens", f"{tot_tokens:,}")
    m3.metric("Total cost", f"${tot_cost:,.4f}")
    m4.metric("Tool calls", tot_tools)
    m5.metric("Avg latency", f"{avg_lat:.2f}s")
    st.divider()

    for i, t in enumerate(traces, start=1):
        header = t.get("user_message", "(no message)")
        header = header if len(header) <= 90 else header[:90] + "…"
        specs = ", ".join(t.get("specialists", [])) or "direct reply"
        with st.expander(f"Turn {i} · {header}  —  [{specs}]", expanded=(i == len(traces))):
            _render_turn(t)


def _render_turn(t: dict) -> None:
    tokens = t.get("tokens", {}) or {}
    tabs = st.tabs([
        "🧠 Reasoning flow",
        "💰 Tokens & cost",
        "🔧 MCP tool calls",
        "📚 Memory context",
        "🧾 Raw events (JSON)",
    ])

    # 1. Reasoning flow
    with tabs[0]:
        st.markdown("**User asked**")
        st.markdown(f"> {t.get('user_message', '')}")
        routing = t.get("routing", {}) or {}
        if routing:
            st.markdown(
                f"**Routing** — backend `{routing.get('backend','?')}` → "
                + (", ".join(routing.get("specialists", [])) or "direct")
            )
        steps = t.get("reasoning", []) or []
        if steps:
            reasoning_flow(steps)
        else:
            st.caption("No reasoning steps recorded.")
        live_reasoning = t.get("reasoning_text", "")
        if live_reasoning:
            with st.expander("🧠 Model reasoning (streamed)", expanded=False):
                st.markdown(live_reasoning)
        if t.get("final"):
            st.markdown("**Final answer**")
            st.markdown(t["final"])

    # 2. Tokens & cost
    with tabs[1]:
        c1, c2, c3 = st.columns(3)
        c1.metric("Prompt tokens", f"{tokens.get('prompt', 0):,}")
        c2.metric("Completion tokens", f"{tokens.get('completion', 0):,}")
        c3.metric("Total tokens", f"{tokens.get('total', 0):,}")
        d1, d2, d3 = st.columns(3)
        d1.metric("Cost (USD)", f"${t.get('cost_usd', 0):,.6f}")
        d2.metric("LLM calls", t.get("llm_calls", 0))
        d3.metric("Latency", f"{(t.get('latency_ms', 0) or 0)/1000:.2f}s")
        st.caption("Cost is estimated from token counts × configured per-1M pricing "
                   "(PRICE_INPUT_PER_1M / PRICE_OUTPUT_PER_1M).")

    # 3. MCP tool calls to MongoDB Atlas
    with tabs[2]:
        calls = t.get("tool_calls", []) or []
        if not calls:
            st.caption("No MongoDB MCP tool calls in this turn "
                       "(the answer used host-side retrieval / memory only).")
        else:
            st.markdown(f"**{len(calls)}** MCP tool call(s) to MongoDB Atlas:")
            for c in calls:
                agent = c.get("agent", "?")
                tool = c.get("tool", "?")
                cid = c.get("id", "")
                card(f"{tool}", f'<span class="mdb-chip">agent: {agent}</span>'
                                + (f'<span class="mdb-chip mdb-chip-tool">id: {cid}</span>' if cid else ""))

    # 4. Context (memory) retrieved from MongoDB Atlas
    with tabs[3]:
        mem = t.get("memory", {}) or {}
        history = mem.get("history", "")
        facts = mem.get("facts", "")
        st.markdown("**Long-term facts** (`agent_memory_facts`)")
        st.code(facts or "(none)", language="text")
        st.markdown("**Short-term history** (`chat_messages`)")
        st.code(history or "(none)", language="text")
        rets = t.get("retrievals", []) or []
        if rets:
            st.markdown("**Hybrid retrieval grounding** (vector + BM25 from MongoDB Atlas)")
            for r in rets:
                st.markdown(f"_{r.get('agent','?')}_ — {len(r.get('hits', []))} candidate(s)")
                st.json(r.get("hits", []), expanded=False)

    # 5. Raw events (JSON)
    with tabs[4]:
        st.caption("Every event emitted during the turn, exactly as streamed over SSE.")
        st.json(t.get("events", []), expanded=False)


# --------------------------------------------------------------------------- #
# List view
# --------------------------------------------------------------------------- #
def render_list() -> None:
    st.title("🗂️ Sessions")
    st.caption("Browse past conversations. Click a session to see its detailed agent traces.")
    if st.button("🔄 Refresh"):
        st.rerun()

    data = _get("/sessions")
    if not data:
        return
    sessions = data.get("sessions", [])
    if not sessions:
        st.info("No sessions yet — start a conversation on the **Career Assistant** page.")
        return

    # Header row
    h = st.columns([3, 1, 1.2, 1.2, 2, 1.4])
    for col, label in zip(h, ["Session", "Turns", "Tokens", "Cost (USD)", "Last activity", ""]):
        col.markdown(f"**{label}**")
    st.divider()

    for s in sessions:
        sid = s.get("session_id", "")
        c = st.columns([3, 1, 1.2, 1.2, 2, 1.4])
        c[0].markdown(f"**{sid}**  \n<span class='mdb-muted'>{(s.get('last_message') or '')[:60]}</span>",
                      unsafe_allow_html=True)
        c[1].write(s.get("turns", 0))
        c[2].write(f"{s.get('tokens_total', 0):,}")
        c[3].write(f"${s.get('cost_usd', 0):,.4f}")
        c[4].write(_fmt_ts(s.get("last")))
        if c[5].button("View traces →", key=f"view-{sid}"):
            st.query_params["session"] = sid
            st.rerun()


# --------------------------------------------------------------------------- #
# Router
# --------------------------------------------------------------------------- #
_selected = st.query_params.get("session")
if _selected:
    render_detail(_selected)
else:
    render_list()
