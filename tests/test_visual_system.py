"""Institutional Graphite visual system — palette integrity and semantic separation.

Proves the decorative application palette is graphite/steel only, that no decorative
purple, teal, royal-blue, yellow or gold survives, that semantic status colors
(red / green / amber / terracotta) remain reserved and distinct, that the structural
header fixes from 66dbb92 are intact, and that rendering mutates nothing.

Source-level assertions read app.py; render assertions drive the real app via AppTest.
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT))

APP = str(ROOT / "app.py")
_SRC = (ROOT / "app.py").read_text(encoding="utf-8")

# ── the authorized Institutional Graphite foundation ─────────────────────────
GRAPHITE = {
    "carbon": "#111827", "graphite": "#1F2937", "panel_hdr": "#2B3139",
    "deep_slate": "#334155", "steel": "#475569", "steel_hover": "#3F4C5E",
    "steel_active": "#2F3947", "light_steel": "#CBD5E1", "focus_steel": "#A8B6C8",
    "pale_steel": "#EEF1F4", "canvas": "#F4F6F8", "main_text": "#17202A",
    "muted_text": "#667085", "border": "#D0D5DD", "strong_border": "#98A2B3",
    "input_hover": "#66788A", "focus_ring": "#DCE3EA", "table_hdr_border": "#46515F",
}
# Terracotta is reserved for pending/attention only.
TERRACOTTA = {"#F5E9E2", "#C58B6C", "#6A3D2A", "#9B5438"}

# Semantic colors that must survive untouched (status, not decoration).
SEMANTIC_RED = {"#7b0000", "#a01818", "#8b0000", "#a00000", "#650000", "#fde8e8",
                "#dc7c7c", "#e9a0a0", "#fdeaea", "#fdecec", "#fff1f1",
                # UNSUPPORTED / CONTRADICTED text + border (the AI-accuracy palette).
                # Same false-positive shape as the muted reds above: dark or desaturated
                # red satisfies b >= g and r > g without being decorative purple.
                "#8f1d1d", "#d98f8f"}
# Completion/allowed-disposition states kept their original blue-teal on purpose:
# "Do not change a semantic status color." They are status, not decoration.
SEMANTIC_COMPLETE = {"#2e728f", "#e8f4f8", "#1a5276", "#8aaabf", "#9fc6d8"}
SEMANTIC_GREEN = {"#1e8449", "#27865a", "#176437", "#2f9c69", "#238c50"}
SEMANTIC_AMBER = {"#eab308", "#fff8e1", "#5d4000", "#f0c040", "#7d4e00"}


def _hexes() -> set[str]:
    return {h.upper() for h in re.findall(r"#[0-9A-Fa-f]{6}", _SRC)}


def _rgb(h: str):
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _allowed() -> set[str]:
    ok = {v.upper() for v in GRAPHITE.values()} | {c.upper() for c in TERRACOTTA}
    ok |= {c.upper() for c in SEMANTIC_RED | SEMANTIC_GREEN | SEMANTIC_AMBER}
    return ok


def _run(**state):
    at = AppTest.from_file(APP, default_timeout=120)
    for k, v in state.items():
        at.session_state[k] = v
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def _committed_hashes() -> dict:
    files = ["pending_overrides.csv", "audit_log.csv", "case_lifecycle.csv",
             "ai_outputs.csv", "human_reviews.csv"]
    return {n: hashlib.sha256((DATA / n).read_bytes()).hexdigest() for n in files}


# ── 1 — no decorative purple ─────────────────────────────────────────────────
def test_no_decorative_purple_remains():
    """Purple = blue >= green AND red > green. Semantic reds are excluded."""
    offenders = []
    for h in _hexes():
        r, g, b = _rgb(h)
        if b >= g and r > g and h not in {c.upper() for c in SEMANTIC_RED | SEMANTIC_AMBER}:
            offenders.append(h)
    assert not offenders, f"decorative purple remains: {sorted(offenders)}"


def test_specific_plum_values_are_gone():
    for plum in ("#17141D", "#2A2230", "#4C3A55", "#6B5578", "#584462", "#45344E",
                 "#D8C7E0", "#C8B5D4", "#F0EBF2", "#F4F2F5", "#211C26", "#BDAFC4",
                 "#765F83", "#604C6C", "#8D7798", "#E4D9E8"):
        assert plum not in _SRC, f"plum value still present: {plum}"


# ── 2,3,4 — no decorative teal / royal blue / gold ───────────────────────────
def test_no_decorative_teal_or_royal_blue_or_gold():
    for dead in ("#245d74", "#1a4f66", "#255d75", "#4a8ba5", "#6ba3ba",
                 "#cfeaf5", "#eaf4f8", "#f3fafc", "#9fb7c7", "#16324a", "#173453",
                 "#2457C5", "#1E46A0", "#173A86", "#AEC7FF", "#C7D7F7",
                 "#F5B942", "#E3A72F", "#A15C00", "#e7f2f7", "#0f4d67"):
        assert dead not in _SRC, f"decorative legacy color still present: {dead}"


def test_legacy_teal_survives_only_on_semantic_completion_states():
    """The COMPLETE / allowed-disposition states keep their original blue-teal because
    a semantic status color must not be restyled. Teal must appear nowhere else."""
    allowed_markers = ("gate-complete", "hro-metric-complete", "badge-blue",
                       "# COMPLETE / allowed disposition")
    for ln in _SRC.splitlines():
        if any(t in ln for t in SEMANTIC_COMPLETE):
            assert any(m in ln for m in allowed_markers),                 f"legacy teal used on a non-completion surface: {ln.strip()[:90]}"


def test_decorative_surfaces_use_graphite():
    """Header, nav-active, table/panel headers and inputs use the graphite scale."""
    css = _SRC
    assert "linear-gradient(135deg,#111827 0%,#1F2937 62%,#26313F 100%)" in css   # id-bar
    assert "linear-gradient(180deg,#526276 0%,#475569 100%)" in css               # nav active / role-switch
    assert "linear-gradient(180deg,#374151 0%,#2B3139 100%)" in css               # table/panel headers
    assert "#46515F" in css                                                       # table header border
    assert re.search(r"focus-within>div\{[^}]*border:1px solid #475569[^}]*box-shadow:0 0 0 3px #DCE3EA", css)


# ── 5,6,7 — semantic colors remain reserved and distinct ─────────────────────
def test_semantic_red_reserved_for_risk_and_failure():
    for red in ("#7b0000", "#a01818"):
        assert red in _SRC, f"semantic red {red} was removed"
    # red must not be reused as a decorative accent surface
    assert "#a01818" not in _SRC.split(".id-bar{")[1].split("}")[0]


def test_semantic_green_reserved_for_pass_and_complete():
    for green in ("#1e8449", "#176437"):
        assert green in _SRC, f"semantic green {green} was removed"


def test_semantic_amber_and_terracotta_reserved_for_pending():
    assert "#eab308" in _SRC and "#fff8e1" in _SRC          # warning / pending amber
    for t in TERRACOTTA:
        assert t in _SRC, f"pending-attention terracotta {t} was removed"
    # the header pending pill uses terracotta, never gold
    pill = _SRC.split(".hdr-pending{")[1].split("}")[0]
    assert "#F5E9E2" in pill and "#C58B6C" in pill and "#6A3D2A" in pill


def test_reset_demo_pastel_is_the_only_yellow_utility_surface():
    """Reset Demo is the single authorized pastel-yellow utility control. Its palette
    is confined to the keyed rule and never becomes a general application accent."""
    i = _SRC.index("div.st-key-demo_reset .stButton>button{")
    block = _SRC[i:i + 1700]
    pastel = ("#FFF9D9", "#F8EDB8", "#D8C779", "#584C1F", "#F5E6A3", "#C5B15D",
              "#4D421B", "#EED985", "#B49A3F", "#453A14", "#F2E6AE",
              "#F3F0DF", "#D8D1A9", "#938B68")
    for c in pastel:
        assert c in block, f"{c} missing from the Reset Demo rules"
        assert _SRC.count(c) == block.count(c), f"{c} leaked outside Reset Demo"
    # it must not be reused on the header, nav, tables or the role-switch
    for surface in (".id-bar{", ".sub-nav{", 'div[data-baseweb="tab-list"]{',
                    ".lv-table thead tr", 'stColumn"]:nth-child(3) .stButton>button{'):
        j = _SRC.find(surface)
        if j != -1:
            assert not any(c in _SRC[j:j + 400] for c in pastel), \
                f"pastel yellow leaked onto {surface}"


def test_semantic_and_decorative_palettes_do_not_collide():
    decorative = {v.upper() for v in GRAPHITE.values()}
    semantic = {c.upper() for c in SEMANTIC_RED | SEMANTIC_GREEN | SEMANTIC_AMBER | TERRACOTTA}
    assert not (decorative & semantic), "a decorative color is also used as a status color"


# ── 8 — graphite first, steel second ─────────────────────────────────────────
def test_reads_as_graphite_first_steel_second():
    """The darkest graphite anchors the chrome; steel is the accent beneath it."""
    assert _SRC.count("#111827") >= 1                       # carbon header
    assert _SRC.count("#1F2937") >= 1                       # graphite header
    assert _SRC.count("#475569") > _SRC.count("#111827")    # steel is the working accent
    assert ".stApp{background:#F4F6F8" in _SRC              # cool-gray canvas


# ── 4 (structure) — 66dbb92 header fixes intact ──────────────────────────────
def test_header_structural_fixes_from_66dbb92_intact():
    assert '<div class="id-bar-logo" role="heading" aria-level="1">' in _SRC   # non-h1 brand
    assert '<h1 class="id-bar-logo"' not in _SRC                                # no anchor-icon heading
    assert 'div[data-testid="stColumn"]:nth-child(3) .stButton>button' in _SRC  # corrected selector
    assert 'div[data-testid="column"]:nth-child(3)' not in _SRC                 # legacy token absent
    mq = _SRC[_SRC.rindex("@media (max-width:700px)", 0, _SRC.index(".id-bar{flex-direction:column")):]
    assert "flex-direction:column" in mq                                        # mobile stacking
    assert "grid-template-columns:minmax(0,1fr) minmax(0,1fr)" in mq            # side-by-side buttons
    assert "display:none" not in mq.split("}\n}")[0]                            # nothing hidden


# ── 5 (nav) — navigation labels/order/behavior unchanged ─────────────────────
def test_navigation_labels_and_order_unchanged():
    at = _run()
    labels = [t.label.strip() for t in at.tabs]
    assert labels[:5] == ["Alert Queue", "Manager Review", "Risk Settings",
                          "Change Log", "Audit Trail"]


# ── 6 (keys) — callbacks and widget keys unchanged ───────────────────────────
def test_callbacks_and_widget_keys_unchanged():
    at = _run(view_as="Analyst")
    keys = {b.key for b in at.button if b.key}
    assert "demo_reset" in keys
    for token in ('key="demo_reset"', 'key="demo_reset_go"', 'key="demo_reset_cancel"',
                  "_open_demo_reset()", "reset_override_demo(", "record_override_decision("):
        assert token in _SRC, f"callback/key changed: {token}"


# ── 7 — render is read-only ──────────────────────────────────────────────────
def test_render_writes_no_audit_and_no_csv(monkeypatch):
    import src.audit as audit_mod
    calls: list = []
    monkeypatch.setattr(audit_mod, "log_event", lambda **kw: calls.append(kw) or {})
    before = _committed_hashes()
    at = _run()
    assert any("id-bar-logo" in m.value for m in at.markdown)
    assert calls == [], f"rendering wrote audit events: {calls}"
    assert _committed_hashes() == before
