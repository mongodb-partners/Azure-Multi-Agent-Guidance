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

DEFAULT_API = os.getenv("API_URL", f"http://localhost:{os.getenv('PORT', '3000')}")

st.set_page_config(page_title="Career Assistant — MongoDB × Azure Foundry", page_icon="💼", layout="wide")


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
st.caption("MongoDB Atlas × Azure AI Foundry · Microsoft Agent Framework")

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
        answer_box = st.empty()
        events: list[tuple[str, dict]] = []
        final_text = ""
        try:
            for etype, data in stream_chat(api_url, prompt, session_id, user_id):
                if etype == "final":
                    final_text = data.get("text", "")
                    answer_box.markdown(final_text)
                elif etype == "done":
                    break
                else:
                    events.append((etype, data))
                    with trace_box:
                        render_trace([events[-1]])
        except Exception as e:  # noqa: BLE001
            final_text = f"⚠️ Error talking to the API: {e}"
            answer_box.error(final_text)

    st.session_state.messages.append({"role": "assistant", "content": final_text or "(no answer)"})
