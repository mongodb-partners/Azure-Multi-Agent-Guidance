"""Metrics dashboard — live observability surfaced from the API's /metrics endpoint.

Reads the in-process metrics store (turns, latency percentiles, token spend,
specialist mix, tool-call volume). When App Insights is enabled the same signals
also flow to Azure Monitor (Workbook + alerts); this page is the always-available
local view.

Run as part of:  streamlit run ui/streamlit_app.py   (appears in the sidebar)
"""
from __future__ import annotations

import datetime as dt
import os
import sys

import pandas as pd
import requests
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from theme import apply_mongodb_theme  # noqa: E402

DEFAULT_API = os.getenv("API_URL", f"http://localhost:{os.getenv('PORT', '3000')}")

st.set_page_config(page_title="Metrics — Career Assistant", page_icon="📊", layout="wide")
apply_mongodb_theme()
st.title("📊 Observability Dashboard")

api_url = st.sidebar.text_input("API URL", DEFAULT_API)
if st.sidebar.button("🔄 Refresh"):
    st.rerun()
auto = st.sidebar.checkbox("Auto-refresh (5s)", value=False)

try:
    snap = requests.get(f"{api_url}/metrics", timeout=10).json()
except Exception as e:  # noqa: BLE001
    st.error(f"Cannot reach {api_url}/metrics: {e}")
    st.stop()

# --- Telemetry status banner ---
if snap.get("enabled"):
    st.success("Telemetry export: **Application Insights ON** — data also in Azure Monitor.")
else:
    st.info(
        "Telemetry export: **local metrics only**. Set "
        "`APPLICATIONINSIGHTS_CONNECTION_STRING` on the API to export traces/metrics to Azure."
    )

totals = snap.get("totals", {})
latency = snap.get("latency_ms", {})
kpis = snap.get("kpis", {})

# --- Top-line KPIs ---
c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("Turns", totals.get("turns", 0))
c2.metric("Errors", totals.get("errors", 0))
c3.metric("p50 latency", f"{latency.get('p50', 0)/1000:.2f}s")
c4.metric("p95 latency", f"{latency.get('p95', 0)/1000:.2f}s")
c5.metric("Total tokens", f"{totals.get('tokens_total', 0):,}")
c6.metric("Total cost", f"${totals.get('cost_usd', 0):,.4f}")

st.divider()

# --- Per-request averages ---
st.subheader("Per-request averages")
a1, a2, a3, a4 = st.columns(4)
a1.metric("Avg latency", f"{(kpis.get('avg_latency_ms', 0) or 0)/1000:.2f}s")
a2.metric("Avg tokens / req", f"{kpis.get('avg_tokens_per_request', 0):,.1f}")
a3.metric("Avg tool calls / req", f"{kpis.get('avg_tool_calls_per_request', 0):,.2f}")
a4.metric("Avg LLM calls / req", f"{kpis.get('avg_llm_calls_per_request', 0):,.2f}")

st.divider()

col_left, col_right = st.columns(2)

# --- Specialist mix ---
with col_left:
    st.subheader("Specialist invocations")
    spec = snap.get("specialist_counts", {})
    if spec:
        st.bar_chart(pd.DataFrame({"invocations": spec}))
    else:
        st.caption("No specialist invocations yet.")

# --- Tool-call volume ---
with col_right:
    st.subheader("MongoDB MCP tool calls")
    tools = snap.get("tool_counts", {})
    if tools:
        st.bar_chart(pd.DataFrame({"calls": tools}))
    else:
        st.caption("No tool calls yet.")

st.divider()

# --- Token split ---
st.subheader("Token usage")
tc1, tc2, tc3 = st.columns(3)
tc1.metric("Prompt tokens", f"{totals.get('tokens_prompt', 0):,}")
tc2.metric("Completion tokens", f"{totals.get('tokens_completion', 0):,}")
tc3.metric("Total tokens", f"{totals.get('tokens_total', 0):,}")

# --- Recent turns table + latency trend ---
st.subheader("Recent turns")
turns = snap.get("recent_turns", [])
if turns:
    rows = []
    for t in turns:
        tok = t.get("tokens", {}) or {}
        rows.append(
            {
                "time": dt.datetime.fromtimestamp(t["ts"]).strftime("%H:%M:%S"),
                "latency_s": round(t.get("latency_ms", 0) / 1000, 2),
                "total_tokens": tok.get("total", 0),
                "llm_calls": t.get("llm_calls", 0),
                "tool_calls": t.get("tool_calls", 0),
                "specialists": ", ".join(t.get("specialists", [])) or "(direct)",
                "error": t.get("error", False),
            }
        )
    df = pd.DataFrame(rows)
    st.line_chart(df.set_index("time")["latency_s"])
    st.dataframe(df[::-1], use_container_width=True, hide_index=True)
else:
    st.caption("No turns recorded yet — send a message in the Chat page.")

if auto:
    import time

    time.sleep(5)
    st.rerun()
