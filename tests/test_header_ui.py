"""Global header (Midnight Lumen) — structure, scoping, exact colors, and read-only guarantees.

Two layers:
- RENDER tests drive the real app via AppTest and assert the header markup, the visible
  header text, the unchanged Reset Demo / role-switch keys+labels, and that merely
  rendering the header mutates nothing (no audit event, no committed-CSV change).
- SOURCE tests assert the header CSS in app.py: the brand is not an h1, selectors are
  scoped to the header, the exact desktop colors are present, the <=700px media query
  exists, and no header metadata is hidden on mobile.

Only the global header is asserted here; nothing below the control row is touched.
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


def _run(**state):
    at = AppTest.from_file(APP, default_timeout=120)
    for k, v in state.items():
        at.session_state[k] = v
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def _brand_md(at) -> str:
    """The markdown block that carries the brand row (id-bar + sub-nav)."""
    return next(m.value for m in at.markdown if "id-bar-logo" in m.value)


def _all_md(at) -> str:
    return " ".join(m.value for m in at.markdown)


def _committed_hashes() -> dict:
    files = ["pending_overrides.csv", "audit_log.csv", "case_lifecycle.csv",
             "ai_outputs.csv", "human_reviews.csv"]
    return {n: hashlib.sha256((DATA / n).read_bytes()).hexdigest() for n in files}


# The header CSS regions, comment-stripped, for selector/color assertions.
def _header_css() -> str:
    lines = []
    for ln in _SRC.splitlines():
        s = ln.strip()
        if s.startswith("/*") or s.startswith("*") or not s:
            continue
        lines.append(ln)
    return "\n".join(lines)


_CSS = _header_css()


def _header_media_block() -> str:
    """The ONE @media(max-width:700px) block that restyles the header (app.py has other
    ≤700px blocks for unrelated surfaces). Located by its first rule and brace-matched."""
    anchor = _SRC.index(".id-bar{flex-direction:column")
    start = _SRC.rindex("@media (max-width:700px)", 0, anchor)
    brace = _SRC.index("{", start)
    depth = 0
    for i in range(brace, len(_SRC)):
        if _SRC[i] == "{":
            depth += 1
        elif _SRC[i] == "}":
            depth -= 1
            if depth == 0:
                return _SRC[start:i + 1]
    raise AssertionError("unterminated header media block")


# ── 1,2,3 — brand element is a div[role=heading], not an anchored h1 ──────────
def test_brand_is_not_an_h1():
    brand = _brand_md(_run())
    assert '<h1 class="id-bar-logo"' not in brand
    assert "<h1" not in brand                                   # brand emits no heading tag
    assert '<div class="id-bar-logo"' in brand


def test_brand_uses_role_heading_aria_level_1():
    brand = _brand_md(_run())
    assert 'role="heading"' in brand
    assert 'aria-level="1"' in brand
    # exact authorized markup
    assert re.search(r'<div class="id-bar-logo" role="heading" aria-level="1">\s*Lumen\s*<span>Verify</span>\s*</div>', brand)


def test_brand_creates_no_heading_anchor():
    brand = _brand_md(_run())
    # A div[role=heading] is not a Streamlit heading, so no anchor-link is injected.
    assert "Link to heading" not in brand
    assert 'href="#lumen-verify"' not in brand
    assert "stHeadingWithActionElements" not in brand


# ── 4 — all existing visible header text still renders ───────────────────────
def test_all_header_text_still_renders():
    at = _run(view_as="Analyst")
    md = _all_md(at)
    assert "Lumen" in md and "Verify" in md                     # brand
    emp = at.session_state.current_user
    assert emp["name"] in md and emp["rank"] in md and emp["id"] in md   # identity
    assert "AML Decision Workbench" in md and "Analyst Queue" in md      # context row
    assert "pending override" in md                            # pending indicator (3 pending)
    assert "Session:" in md and "UTC" in md                    # session + timestamp
    assert "Demo mode" in md and "Switch role to access manager" in md   # banner wording unchanged


# ── 5 — Reset Demo + role-switch callbacks and keys unchanged ────────────────
def test_reset_demo_and_role_switch_keys_unchanged():
    at = _run(view_as="Analyst")
    keys = {b.key for b in at.button if b.key}
    assert "demo_reset" in keys                                # Reset Demo widget key intact
    labels = {b.label for b in at.button}
    assert "↻ Reset Demo" in labels
    assert "→ Manager" in labels                               # role-switch label (Analyst view)
    # role-switch remains keyless (positional isolation), reset dialog keys intact
    assert 'key="demo_reset"' in _SRC
    assert 'key="demo_reset_go"' in _SRC and 'key="demo_reset_cancel"' in _SRC
    assert "_open_demo_reset()" in _SRC and "reset_override_demo(" in _SRC


def test_role_switch_label_flips_with_view():
    assert "→ Analyst" in {b.label for b in _run(view_as="Manager").button}
    assert "→ Manager" in {b.label for b in _run(view_as="Analyst").button}


# ── 6 — header selectors are scoped (no leakage to global buttons/blocks) ─────
def test_header_button_selectors_are_scoped():
    # Reset Demo: keyed selector only
    assert "div.st-key-demo_reset .stButton>button{" in _CSS
    # Role-switch: scoped to the 3rd stColumn of the role-bar control row (corrected token)
    assert ('div[data-testid="stHorizontalBlock"]:has(.role-bar) '
            'div[data-testid="stColumn"]:nth-child(3) .stButton>button{') in _CSS.replace("\n", " ")
    # Control row background scoped to the block that :has(.role-bar) — one block only
    assert 'div[data-testid="stHorizontalBlock"]:has(.role-bar){' in _CSS
    # No un-scoped global header-button rule (would leak to every stButton)
    assert not re.search(r'(?m)^\.stButton>button\{[^}]*background:#2457C5', _SRC)
    # The dead legacy column-testid must NOT be used for the role-switch
    assert 'div[data-testid="column"]:nth-child(3)' not in _SRC


# ── 7 — exact desktop colors present ─────────────────────────────────────────
def test_exact_desktop_colors_present():
    required = [
        "#0B1F33", "#294763", "#F5B942", "#132B45", "#35516D", "#F4F7FB", "#C3D0DE",   # id-bar/sub-nav/badge
        "#FFF3D0", "#E3A72F", "#7A4A00", "#A15C00",                                    # pending pill
        "#D7E0EA", "#3A4657", "#142033",                                              # role-bar
        "#F3F6FA",                                                                     # control row
        "#27364A", "#FFF7DF", "#FCE9B2", "#C7D7F7", "#E8EDF3", "#8B98A8",              # reset demo states
        "#2457C5", "#1E46A0", "#173A86", "#AEC7FF",                                    # role-switch states
    ]
    for hexval in required:
        assert hexval in _SRC, f"missing header color {hexval}"


def test_id_bar_and_logo_exact_rule():
    css = _CSS.replace("\n", " ")
    assert re.search(r'\.id-bar\{[^}]*background:#0B1F33[^}]*min-height:74px[^}]*'
                     r'border-bottom:1px solid #294763[^}]*box-shadow:0 8px 24px rgba\(11,31,51,0\.18\)', css)
    assert re.search(r'\.id-bar-logo\{[^}]*font-size:30px[^}]*font-weight:800[^}]*letter-spacing:-0\.6px', css)
    assert re.search(r'\.id-bar-logo span\{[^}]*color:#F5B942', css)


# ── 8 — the <=700px header media query exists ────────────────────────────────
def test_mobile_media_query_exists():
    assert "@media (max-width:700px)" in _SRC
    mq = _header_media_block()
    for sel in (".id-bar", ".id-bar-logo", ".id-bar-user", ".sub-nav", ".sub-nav-left",
                ".sub-nav-right", ".role-bar",
                'div[data-testid="stHorizontalBlock"]:has(.role-bar)'):
        assert sel in mq, f"{sel} not restyled in the header mobile block"


# ── 9 — the mobile rule hides no header metadata ─────────────────────────────
def test_mobile_rule_hides_no_metadata():
    mq = _header_media_block()
    # no header element is removed from flow on mobile
    assert "display:none" not in mq
    assert "visibility:hidden" not in mq
    # the metadata container is laid out (grid), not hidden; the pending/session/timestamp
    # live inside .sub-nav-right, which stays visible
    assert re.search(r'\.sub-nav-right\{[^}]*display:grid', mq.replace("\n", " "))
    assert "hdr-pending" not in mq or "display:none" not in mq   # pending pill never hidden


# ── 10 — rendering the header is read-only (no audit event, no CSV mutation) ──
def test_header_render_writes_no_audit_and_no_csv(monkeypatch):
    import src.audit as audit_mod
    calls: list = []
    monkeypatch.setattr(audit_mod, "log_event", lambda **kw: calls.append(kw) or {})
    before = _committed_hashes()
    at = _run()                                                # render only; no button click
    assert "id-bar-logo" in _all_md(at)                        # header really rendered
    assert calls == [], f"rendering wrote audit events: {calls}"
    assert _committed_hashes() == before                       # no committed CSV changed
