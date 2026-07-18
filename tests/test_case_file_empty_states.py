"""Pass A — Case File consistency: neutral empty states for alerts with no human
review, plus regression that the review-present path is undisturbed.

The additive change (app.show_case_dialog) renders two neutral empty states —
"No human review recorded for this alert." and a HUMAN-REVIEW GATE panel reading
"Not evaluated because no human review exists." — in the exact positions the Human
Review and Human-Review Gate panels occupy when a review IS on file. It fabricates
no review, shows no PASS/FAIL/BLOCKED verdict, and writes no audit event.

Manager Review → correct Case File routing is proven separately in
tests/test_manager_review_case_file.py: both entry points (the Alert Queue
``?alert=`` link and the Manager "Open Case File" button) set ``open_case`` and
call the same ``app.show_case_dialog`` for the alert id — one shared component.
"""

from __future__ import annotations

import sys
from pathlib import Path

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

APP = str(ROOT / "app.py")

NO_REVIEW_ALERT = "ALERT002"   # has an AI claim, but no human_reviews row -> empty states
REVIEW_MISSING_MSG = "No human review recorded for this alert."
GATE_EMPTY_MSG = "Not evaluated because no human review exists."


def _open(alert_id: str) -> str:
    """Open an alert's Case File via the shared open_case trigger; return joined markdown."""
    at = AppTest.from_file(APP, default_timeout=120)
    at.session_state["open_case"] = alert_id
    at.run()
    assert not at.exception, f"Case File for {alert_id} raised: {[str(e.value) for e in at.exception]}"
    return " ".join(m.value for m in at.markdown)


def test_alert_without_review_renders_both_empty_states():
    md = _open(NO_REVIEW_ALERT)
    # both neutral empty states appear, in the Human Review + gate positions
    assert REVIEW_MISSING_MSG in md, "Human Review empty state missing"
    assert GATE_EMPTY_MSG in md, "Human-Review Gate empty state missing"
    # and NO verdict is invented for a review that does not exist
    assert "HUMAN-REVIEW GATE: BLOCKED" not in md
    assert "HUMAN-REVIEW GATE: PASSED" not in md


def test_empty_state_render_writes_no_audit(monkeypatch):
    import src.audit as audit_mod
    calls: list[dict] = []
    monkeypatch.setattr(audit_mod, "log_event", lambda **kw: calls.append(kw) or {})
    md = _open(NO_REVIEW_ALERT)
    assert REVIEW_MISSING_MSG in md              # the empty states really rendered
    assert calls == [], f"empty-state render wrote audit events: {calls}"


def test_alert001_review_present_path_intact_ai_fail_and_human_outcome():
    # ALERT001 HAS a review (REV003, reviewer:mchen, edited). It must take the
    # review-present branch, not the new empty-state else: AI claim FAIL is shown
    # together with the recorded human outcome.
    md = _open("ALERT001")
    assert REVIEW_MISSING_MSG not in md                       # not the empty-state branch
    assert ">FAIL<" in md                                     # AI prior_sar_history claim FAILs (Hero A)
    assert "reviewer:mchen" in md                             # human review rendered
    assert "did not accept the AI draft as-is" in md          # recorded human outcome


def test_alert007_review_present_path_intact_prior_sar_pass_and_gate_blocked():
    # ALERT007 HAS a review (REV001, reviewer:jdoe, incomplete "accepted"). Review-
    # present branch: prior-SAR claim PASS + human-review gate BLOCKED (same rule).
    md = _open("ALERT007")
    assert REVIEW_MISSING_MSG not in md
    assert ">PASS<" in md                                     # prior_sar_history PASS (contrast case)
    assert "reviewer:jdoe" in md
    assert "HUMAN-REVIEW GATE: BLOCKED" in md
