"""MongoDB Design Language (LeafyGreen) theming for the Streamlit UI.

Applies the MongoDB brand palette, typography, and component styling so the app is visually
consistent with https://www.mongodb.design. Import and call `apply_mongodb_theme()` at the top
of each page, and use `sidebar_metric()` to render KPI cards in the sidebar.
"""
from __future__ import annotations

import streamlit as st

# --- MongoDB / LeafyGreen palette -------------------------------------------------------------
GREEN_BASE = "#00ED64"      # MongoDB "spring green"
GREEN_DARK = "#00684A"      # forest green (primary actions / accents)
GREEN_DARKEST = "#023430"
BLACK = "#001E2B"           # LeafyGreen black
GRAY_DARK = "#1C2D38"
GRAY_MID = "#5C6C75"
GRAY_LIGHT = "#889397"
GRAY_LIGHTER = "#E8EDEB"
BG = "#F9FBFA"              # off-white background
WHITE = "#FFFFFF"
BLUE = "#016BF8"

_CSS = f"""
<style>
/* ---- Typography (LeafyGreen font stack) ---- */
html, body, [class*="css"], .stApp, button, input, textarea {{
  font-family: 'Euclid Circular A', -apple-system, BlinkMacSystemFont, 'Segoe UI',
    Roboto, 'Helvetica Neue', Arial, sans-serif !important;
}}

/* ---- App background (match the navbar's dark LeafyGreen black) ---- */
.stApp {{ background-color: {BLACK}; }}
.stApp, .stApp p, .stApp li, .stApp span, .stApp label,
[data-testid="stChatMessage"] * {{ color: {GRAY_LIGHTER}; }}
[data-testid="stCaptionContainer"], .stApp .stCaption {{ color: {GRAY_LIGHT} !important; }}

/* ---- Headings (light on the dark background) ---- */
h1, h2, h3 {{ color: {WHITE}; letter-spacing: -0.3px; }}
h1 {{ font-weight: 700; }}

/* ---- Sidebar ---- */
section[data-testid="stSidebar"] {{
  background-color: {BLACK};
  border-right: 1px solid {GREEN_DARKEST};
}}
section[data-testid="stSidebar"] * {{ color: {GRAY_LIGHTER}; }}
section[data-testid="stSidebar"] h1,
section[data-testid="stSidebar"] h2,
section[data-testid="stSidebar"] h3 {{ color: {WHITE}; }}
section[data-testid="stSidebar"] .stTextInput input {{
  background-color: {GRAY_DARK}; color: {WHITE}; border: 1px solid {GRAY_MID};
}}

/* ---- Primary buttons ---- */
.stButton > button {{
  background-color: {GREEN_BASE}; color: {BLACK}; border: none; border-radius: 24px;
  font-weight: 600; padding: 0.4rem 1.1rem; transition: background-color .15s ease;
}}
.stButton > button:hover {{ background-color: {GREEN_DARK}; color: {WHITE}; }}

/* ---- Chat bubbles (raised dark panel on the dark background) ---- */
[data-testid="stChatMessage"] {{
  background-color: {GRAY_DARK}; border: 1px solid {GREEN_DARKEST}; border-radius: 12px;
  padding: 0.4rem 0.8rem;
}}

/* ---- Chat input bar ---- */
[data-testid="stChatInput"], [data-testid="stChatInput"] textarea {{
  background-color: {GRAY_DARK}; color: {WHITE}; border-color: {GRAY_MID};
}}

/* ---- st.metric cards (main area) ---- */
[data-testid="stMetric"] {{
  background: {GRAY_DARK}; border: 1px solid {GREEN_DARKEST}; border-radius: 12px;
  padding: 0.8rem 1rem;
}}
[data-testid="stMetricValue"] {{ color: {GREEN_BASE}; font-weight: 700; }}
[data-testid="stMetricLabel"] {{ color: {GRAY_LIGHT}; }}

/* ---- Expanders / trace panel on the dark background ---- */
[data-testid="stExpander"] {{
  background-color: {GRAY_DARK}; border: 1px solid {GREEN_DARKEST}; border-radius: 12px;
}}
[data-testid="stExpander"] summary {{ color: {GRAY_LIGHTER}; }}

/* ---- Text inputs in the main area ---- */
.stApp .stTextInput input {{
  background-color: {GRAY_DARK}; color: {WHITE}; border: 1px solid {GRAY_MID};
}}

/* ---- Links (brighter green for contrast on dark) ---- */
a, a:visited {{ color: {GREEN_BASE}; }}

/* ---- MongoDB-styled sidebar KPI cards ---- */
.mdb-kpi {{
  background: linear-gradient(135deg, {GRAY_DARK} 0%, {GREEN_DARKEST} 100%);
  border: 1px solid {GREEN_DARK}; border-radius: 12px;
  padding: 10px 12px; margin-bottom: 8px;
}}
.mdb-kpi .mdb-kpi-label {{
  color: {GREEN_BASE}; font-size: 0.68rem; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.6px; margin: 0;
}}
.mdb-kpi .mdb-kpi-value {{
  color: {WHITE}; font-size: 1.35rem; font-weight: 700; margin: 2px 0 0 0; line-height: 1.1;
}}
.mdb-kpi .mdb-kpi-sub {{ color: {GRAY_LIGHT}; font-size: 0.66rem; margin: 1px 0 0 0; }}

/* ---- Sidebar section title ---- */
.mdb-section-title {{
  display: flex; align-items: center; gap: 8px;
  color: {WHITE}; font-weight: 700; font-size: 0.95rem;
  margin: 8px 0 6px 0;
}}
.mdb-leaf {{ color: {GREEN_BASE}; }}

/* ---- Reasoning-flow timeline (Sessions detail) ---- */
.mdb-flow {{ border-left: 2px solid {GREEN_DARK}; margin: 4px 0 4px 6px; padding-left: 14px; }}
.mdb-flow-step {{ position: relative; margin: 0 0 12px 0; }}
.mdb-flow-step::before {{
  content: ""; position: absolute; left: -21px; top: 3px; width: 10px; height: 10px;
  border-radius: 50%; background: {GREEN_BASE}; border: 2px solid {BLACK};
}}
.mdb-flow-step .mdb-flow-label {{ color: {GREEN_BASE}; font-weight: 700; font-size: 0.86rem; }}
.mdb-flow-step .mdb-flow-detail {{ color: {GRAY_LIGHTER}; font-size: 0.82rem; margin-top: 2px; }}

/* ---- Chips / badges ---- */
.mdb-chip {{
  display: inline-block; background: {GREEN_DARKEST}; color: {GREEN_BASE};
  border: 1px solid {GREEN_DARK}; border-radius: 20px; padding: 2px 10px;
  font-size: 0.74rem; font-weight: 600; margin: 2px 4px 2px 0;
}}
.mdb-chip-tool {{ background: {GRAY_DARK}; color: {WHITE}; border-color: {GRAY_MID}; }}

/* ---- Panel card ---- */
.mdb-card {{
  background: {GRAY_DARK}; border: 1px solid {GREEN_DARKEST}; border-radius: 12px;
  padding: 12px 14px; margin-bottom: 10px;
}}
.mdb-card .mdb-card-title {{ color: {GREEN_BASE}; font-weight: 700; font-size: 0.9rem; }}
.mdb-card .mdb-card-body {{ color: {GRAY_LIGHTER}; font-size: 0.85rem; margin-top: 4px; }}
.mdb-muted {{ color: {GRAY_LIGHT}; font-size: 0.8rem; }}
</style>
"""


def apply_mongodb_theme() -> None:
    """Inject the MongoDB Design Language CSS. Call once per page (after set_page_config)."""
    st.markdown(_CSS, unsafe_allow_html=True)


def sidebar_metric(label: str, value: str, sub: str | None = None) -> None:
    """Render a MongoDB-styled KPI card in the sidebar."""
    sub_html = f'<p class="mdb-kpi-sub">{sub}</p>' if sub else ""
    st.sidebar.markdown(
        f'<div class="mdb-kpi">'
        f'<p class="mdb-kpi-label">{label}</p>'
        f'<p class="mdb-kpi-value">{value}</p>'
        f"{sub_html}</div>",
        unsafe_allow_html=True,
    )


def section_title(text: str) -> None:
    """Render a MongoDB-styled sidebar section header with a leaf glyph."""
    st.sidebar.markdown(
        f'<div class="mdb-section-title"><span class="mdb-leaf">🍃</span>{text}</div>',
        unsafe_allow_html=True,
    )


def _esc(s: str) -> str:
    """Minimal HTML escaping for values interpolated into markup."""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def reasoning_flow(steps: list[dict]) -> None:
    """Render an ordered reasoning-flow timeline from a list of {step, detail, ...} dicts."""
    import re

    items = []
    for s in steps:
        label = _esc(s.get("step", ""))
        # allow simple **bold** and `code` markdown in the detail
        detail = _esc(s.get("detail", ""))
        detail = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", detail)
        detail = re.sub(r"`(.+?)`", r"<code>\1</code>", detail)
        items.append(
            f'<div class="mdb-flow-step"><div class="mdb-flow-label">{label}</div>'
            f'<div class="mdb-flow-detail">{detail}</div></div>'
        )
    st.markdown(f'<div class="mdb-flow">{"".join(items)}</div>', unsafe_allow_html=True)


def chips(values: list[str], *, tool: bool = False) -> None:
    """Render a row of MongoDB-styled chips/badges."""
    cls = "mdb-chip mdb-chip-tool" if tool else "mdb-chip"
    html = "".join(f'<span class="{cls}">{_esc(v)}</span>' for v in values)
    st.markdown(html or '<span class="mdb-muted">—</span>', unsafe_allow_html=True)


def card(title: str, body_html: str) -> None:
    """Render a MongoDB-styled panel card (body_html is trusted/prebuilt)."""
    st.markdown(
        f'<div class="mdb-card"><div class="mdb-card-title">{_esc(title)}</div>'
        f'<div class="mdb-card-body">{body_html}</div></div>',
        unsafe_allow_html=True,
    )
