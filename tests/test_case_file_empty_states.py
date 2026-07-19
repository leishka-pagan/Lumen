"""Pass A — Case File consistency: lifecycle-derived neutral states for alerts with
no human review, plus regression that the review-present path is undisturbed.

The Human Review + Human-Review Gate panels are now driven by the canonical lifecycle
(app.show_case_dialog). For a NOT_REQUIRED alert like ALERT002 the Human Review panel
reads "Human review was not required under deterministic routing policy
POL-REVIEW-ROUTING-V1." and the gate reads "HUMAN-REVIEW GATE: NOT APPLICABLE" with
"The routing policy authorized the recorded system disposition: monitor." — in the
exact positions those panels occupy when a review IS on file. It fabricates no review,
shows no PASS/FAIL/BLOCKED verdict, and writes no audit event.

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

NO_REVIEW_ALERT = "ALERT002"   # PASS + NOT_REQUIRED routing, no human_reviews row
# Lifecycle-derived neutral states for ALERT002 (NOT_REQUIRED, pending override).
REVIEW_MISSING_MSG = "Human review was not required under deterministic routing policy POL-REVIEW-ROUTING-V1."
GATE_EMPTY_MSG = "The routing policy authorized the recorded system disposition: monitor."


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


# ── Case outcome summary rail (three derived display values) ──────────────────

def _summary_pair(label: str, value: str) -> str:
    """The exact label->value adjacency the summary rail renders."""
    return f'case-summary-label">{label}</div><div class="case-summary-value">{value}</div>'


def test_case_outcome_summary_alert001_fail_complete_monitor():
    md = _open("ALERT001")
    assert _summary_pair("AI VERIFICATION", "FAIL") in md
    assert _summary_pair("REVIEW REQUIREMENTS", "COMPLETE") in md
    # Recorded disposition is the lifecycle final_action (MONITOR), never the review
    # decision word (EDITED).
    assert _summary_pair("RECORDED DISPOSITION", "MONITOR") in md
    assert _summary_pair("RECORDED DISPOSITION", "EDITED") not in md


def test_case_outcome_summary_alert007_mixed_blocked_none():
    md = _open("ALERT007")
    assert _summary_pair("AI VERIFICATION", "MIXED") in md
    assert _summary_pair("REVIEW REQUIREMENTS", "BLOCKED") in md
    assert _summary_pair("RECORDED DISPOSITION", "NONE") in md


def test_case_outcome_summary_alert002_pass_not_required_monitor():
    md = _open("ALERT002")
    assert _summary_pair("AI VERIFICATION", "PASS") in md
    assert _summary_pair("REVIEW REQUIREMENTS", "NOT REQUIRED") in md
    assert _summary_pair("RECORDED DISPOSITION", "MONITOR") in md


def test_gate_allowed_panel_is_blue_complete_not_green_passed():
    # A gate-allowed review shows the neutral blue "REVIEW REQUIREMENTS: COMPLETE"
    # panel — never the old green "HUMAN-REVIEW GATE: PASSED".
    md = _open("ALERT001")
    assert "gate-panel gate-complete" in md
    assert "REVIEW REQUIREMENTS: COMPLETE" in md
    assert "HUMAN-REVIEW GATE: PASSED" not in md
    assert "gate-passed" not in md
