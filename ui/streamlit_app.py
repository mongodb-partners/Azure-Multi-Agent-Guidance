"""Streamlit chat UI for the Multi-Agent Career Assistant.

Talks to the FastAPI `/chat` SSE endpoint, rendering a live trace (routing decision, tool
calls, per-specialist activity) alongside the final synthesized answer. No auth (local demo).

Run:  streamlit run ui/streamlit_app.py
"""
from __future__ import annotations

import json
import os

import requests
import streamlit as st

from theme import apply_mongodb_theme, section_title, sidebar_metric

DEFAULT_API = os.getenv("API_URL", f"http://localhost:{os.getenv('PORT', '3000')}")

st.set_page_config(page_title="Career Assistant — MongoDB × Azure Foundry", page_icon="💼", layout="wide")
apply_mongodb_theme()


def _fmt_int(n) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "0"


def fetch_usage_kpis(api_url: str) -> dict | None:
    """Fetch the aggregate KPIs from the API /metrics endpoint (once per page load)."""
    try:
        return requests.get(f"{api_url}/metrics", timeout=8).json().get("kpis", {})
    except Exception:  # noqa: BLE001
        return None


def render_usage_metrics(api_url: str) -> None:
    """MongoDB-styled 'Usage & Cost' section in the left navbar (updates on page refresh)."""
    section_title("Usage &amp; Cost")
    kpis = fetch_usage_kpis(api_url)
    if kpis is None:
        st.sidebar.caption("Metrics unavailable — check the API URL.")
        return
    latency_s = (kpis.get("avg_latency_ms", 0) or 0) / 1000.0
    sidebar_metric("Total tokens consumed", _fmt_int(kpis.get("total_tokens", 0)))
    sidebar_metric("Total cost (USD)", f"${kpis.get('total_cost_usd', 0):,.4f}")
    sidebar_metric("Avg latency", f"{latency_s:.2f} s")
    sidebar_metric("Avg tokens / request", f"{kpis.get('avg_tokens_per_request', 0):,.1f}")
    sidebar_metric("Avg tool calls / request", f"{kpis.get('avg_tool_calls_per_request', 0):,.2f}")
    sidebar_metric("Avg LLM calls / request", f"{kpis.get('avg_llm_calls_per_request', 0):,.2f}")
    st.sidebar.caption("Values refresh when the page reloads.")


# --------------------------------------------------------------------------- #
# SSE client
# --------------------------------------------------------------------------- #
def stream_chat(api_url: str, message: str, session_id: str, user_id: str):
    """Yield (event_type, data_dict) from the SSE /chat endpoint."""
    resp = requests.post(
        f"{api_url}/chat",
        json={"message": message, "session_id": session_id, "user_id": user_id},
        stream=True,
        timeout=300,
        headers={"Accept": "text/event-stream"},
    )
    resp.raise_for_status()
    event_type = "message"
    for raw in resp.iter_lines(decode_unicode=True):
        if raw is None or raw == "":
            continue
        if raw.startswith("event:"):
            event_type = raw[len("event:"):].strip()
        elif raw.startswith("data:"):
            payload = raw[len("data:"):].strip()
            try:
                data = json.loads(payload) if payload else {}
            except json.JSONDecodeError:
                data = {"raw": payload}
            yield event_type, data
            event_type = "message"


def render_trace(events: list[tuple[str, dict]]) -> None:
    for etype, data in events:
        if etype == "routing":
            specialists = ", ".join(data.get("specialists", []))
            st.markdown(f"🧭 **Routing** (`{data.get('backend')}`) → **{specialists}**")
        elif etype == "retrieval":
            st.markdown(f"🔎 Hybrid retrieval grounded **{data.get('agent')}**")
        elif etype == "specialist_start":
            st.markdown(f"▶️ **{data.get('agent')}** working…")
        elif etype == "specialist_source":
            src = data.get("source")
            label = "Foundry managed agent runtime" if src == "registered" else "in-process (inline)"
            st.markdown(f"&nbsp;&nbsp;🏛️ `{data.get('agent')}` served by **{label}**")
        elif etype == "tool_call":
            st.markdown(f"&nbsp;&nbsp;🔧 `{data.get('agent')}` → MongoDB MCP `{data.get('tool')}`")
        elif etype == "synthesizing":
            st.markdown(f"🧬 Synthesizing answers from {', '.join(data.get('agents', []))}")
        elif etype == "notice":
            st.info(data.get("message", ""))
        elif etype == "error":
            st.error(f"{data.get('agent', '')}: {data.get('error')}")


# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.header("⚙️ Connection")
    api_url = st.text_input("API URL", DEFAULT_API)
    session_id = st.text_input("Session ID", "demo-session")
    user_id = st.text_input("User ID", "demo-user")

    if st.button("Check API health"):
        try:
            h = requests.get(f"{api_url}/health", timeout=10).json()
            st.success("API healthy")
            st.json(h)
        except Exception as e:  # noqa: BLE001
            st.error(f"Cannot reach API: {e}")

# ---- Usage & Cost metrics (left navbar; refreshed on page load) ----
render_usage_metrics(api_url)

with st.sidebar:

    # Telemetry / observability status
    try:
        h = requests.get(f"{api_url}/health", timeout=5).json()
        if h.get("telemetry_enabled"):
            st.success("📈 Telemetry: App Insights ON")
        else:
            st.info("📉 Telemetry: local metrics only\n\n(set APPLICATIONINSIGHTS_CONNECTION_STRING to export to Azure)")
    except Exception:  # noqa: BLE001
        pass
    ai_url = os.getenv("APP_INSIGHTS_PORTAL_URL")
    if ai_url:
        st.markdown(f"[Open App Insights →]({ai_url})")
    st.caption("See the **Metrics** page (sidebar) for the live dashboard.")

    with st.expander("Specialists"):
        try:
            ags = requests.get(f"{api_url}/agents", timeout=10).json().get("agents", [])
            for a in ags:
                tag = "" if a.get("routable") else " _(router)_"
                st.markdown(f"**{a['name']}**{tag}")
                st.caption(a["description"])
        except Exception as e:  # noqa: BLE001
            st.caption(f"(agents unavailable: {e})")

    st.caption("Try: *Find jobs matching my CV and tell me about those companies.*")


# --------------------------------------------------------------------------- #
# Chat
# --------------------------------------------------------------------------- #
st.title("💼 Career Assistant")
st.caption("MongoDB Atlas × Azure AI Foundry Agent Service")

if "messages" not in st.session_state:
    st.session_state.messages = []

for m in st.session_state.messages:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])

if prompt := st.chat_input("Ask about jobs, career growth, or companies…"):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        trace_box = st.expander("🔬 Agent trace", expanded=True)
        reasoning_box = st.expander("🧠 Reasoning (live)", expanded=True)
        reasoning_slot = reasoning_box.empty()
        reasoning_slot.caption("_Waiting for the model to start reasoning…_")
        answer_box = st.empty()
        events: list[tuple[str, dict]] = []
        final_text = ""
        streamed_text = ""       # answer accumulated live from `delta` events
        reasoning_text = ""      # reasoning accumulated live from `reasoning` events
        streamed_final = False   # True when `final` says the answer already streamed
        usage: dict = {}
        try:
            for etype, data in stream_chat(api_url, prompt, session_id, user_id):
                if etype == "reasoning":
                    reasoning_text += data.get("text", "")
                    reasoning_slot.markdown(reasoning_text)
                elif etype == "delta":
                    # Live answer token chunk — append and re-render progressively.
                    streamed_text += data.get("text", "")
                    answer_box.markdown(streamed_text + " ▌")
                elif etype == "final":
                    final_text = data.get("text", "")
                    streamed_final = bool(data.get("streamed"))
                    # If the answer already streamed live, keep it (avoids flicker); otherwise
                    # (direct fallback / synthesized reply) render the authoritative final text.
                    answer_box.markdown(final_text if (final_text and not streamed_final) else streamed_text)
                    if not final_text:
                        final_text = streamed_text
                elif etype == "usage":
                    usage = data
                elif etype == "done":
                    break
                else:
                    events.append((etype, data))
                    with trace_box:
                        render_trace([events[-1]])
        except Exception as e:  # noqa: BLE001
            final_text = f"⚠️ Error talking to the API: {e}"
            answer_box.error(final_text)
        else:
            # Stream ended cleanly: settle the answer (drop the live cursor) and, if no `final`
            # event arrived, fall back to whatever streamed live.
            if not final_text:
                final_text = streamed_text
            answer_box.markdown(final_text)

        # Inline per-turn telemetry: latency + token usage.
        if usage:
            tok = usage.get("tokens", {}) or {}
            latency = usage.get("latency_ms", 0)
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Latency", f"{latency/1000:.2f}s")
            c2.metric("Total tokens", tok.get("total", 0))
            c3.metric("Prompt", tok.get("prompt", 0))
            c4.metric("Completion", tok.get("completion", 0))

    st.session_state.messages.append({"role": "assistant", "content": final_text or "(no answer)"})
