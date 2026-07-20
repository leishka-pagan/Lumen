"""Manager Review split into two subviews by one local segmented control:

- "human_reviews" (default): Human Review Oversight — a review-only summary rail,
  the Requires Attention card (ALERT007), and the Completed Reviews grid
  (ALERT001, ALERT004). No override sections here.
- "override_requests": the existing Pending Override Requests cards and Override
  History, unchanged. No human-review cards here.

Switching preserves role state and writes no audit event. These tests exercise the
real Streamlit render (AppTest) and the segmented control.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT))

import app  # noqa: E402  (module-level helpers: lifecycle_*_label)
from src.lifecycle_store import load_lifecycle  # noqa: E402

APP = str(ROOT / "app.py")


def _committed_hashes() -> dict:
    return {n: hashlib.sha256((DATA / n).read_bytes()).hexdigest()
            for n in ("case_lifecycle.csv", "human_reviews.csv",
                      "pending_overrides.csv", "audit_log.csv", "ai_outputs.csv")}


def _manager(**state) -> AppTest:
    at = AppTest.from_file(APP, default_timeout=120)
    at.session_state["view_as"] = "Manager"
    for k, v in state.items():
        at.session_state[k] = v
    at.run()
    assert not at.exception, f"Manager Review raised: {[str(e.value) for e in at.exception]}"
    return at


def _override_view() -> AppTest:
    at = _manager()
    at.segmented_control(key="manager_review_view").set_value("override_requests").run()
    assert not at.exception, f"Override view raised: {[str(e.value) for e in at.exception]}"
    return at


def _md(at: AppTest) -> str:
    return " ".join(m.value for m in at.markdown)


def _card(at: AppTest, needle: str) -> str:
    return next(m.value for m in at.markdown if needle in m.value)


def _btn(at: AppTest, key: str):
    return next(b for b in at.button if b.key == key)


def _metric(label: str, value: str) -> str:
    return f'hro-metric-lbl">{label}</div><div class="hro-metric-val">{value}</div>'


# 1
def test_manager_review_defaults_to_human_reviews():
    at = _manager()
    assert at.session_state["manager_review_view"] == "human_reviews"
    md = _md(at)
    assert "Human Review Oversight" in md
    assert "Pending Override Requests" not in md


# 2
def test_default_view_summary_counts_1_2_3():
    md = _md(_manager())
    assert _metric("Requires Attention", "1") in md
    assert _metric("Completed Reviews", "2") in md
    assert _metric("Total Human Reviews", "3") in md
    assert "Pending Overrides" not in md          # the review-only rail replaced this metric


# 3
def test_default_view_shows_all_three_reviews():
    md = _md(_manager())
    assert "ALERT007 · Roland Beck" in md
    assert "ALERT001 · Dana Whitfield" in md
    assert "ALERT004 · Tomas Herrera" in md


def test_alert007_requires_attention_badges_and_missing_fields():
    card = _card(_manager(), "ALERT007 · Roland Beck")
    assert "AI VERIFICATION: PASS" in card
    assert "REVIEW REQUIREMENTS: BLOCKED" in card
    assert "RECORDED DISPOSITION: NONE" in card
    assert "reviewer:jdoe" in card
    for field in ("evidence_reviewed", "decision_reason", "final_note", "final_action"):
        assert field in card, f"missing requirement {field} not shown"


def test_completed_review_badges_alert001_and_alert004():
    at = _manager()
    c1 = _card(at, "ALERT001 · Dana Whitfield")
    # RECORDED DISPOSITION is lifecycle.final_action (MONITOR), never the review decision (EDITED)
    assert "AI VERIFICATION: PASS" in c1 and "REVIEW REQUIREMENTS: COMPLETE" in c1 and "RECORDED DISPOSITION: MONITOR" in c1
    assert "RECORDED DISPOSITION: EDITED" not in c1
    assert "Review ID: REV003" in c1 and "Final Action" in c1
    c4 = _card(at, "ALERT004 · Tomas Herrera")
    assert "AI VERIFICATION: PASS" in c4 and "REVIEW REQUIREMENTS: COMPLETE" in c4 and "RECORDED DISPOSITION: ESCALATE" in c4
    assert "RECORDED DISPOSITION: EDITED" not in c4
    assert "Review ID: REV002" in c4 and "Final Action" in c4


# 4
def test_default_view_hides_override_sections():
    at = _manager()
    md = _md(at)
    assert "Pending Override Requests" not in md
    assert "Override History" not in md
    keys = {b.key for b in at.button if b.key}
    assert not any(k.startswith(("oc_", "apr_", "rej_")) for k in keys)


# 5 + 6
def test_human_review_open_case_file_routes_correctly():
    for alert, customer in [("ALERT007", "Roland Beck"),
                            ("ALERT001", "Dana Whitfield"),
                            ("ALERT004", "Tomas Herrera")]:
        at = _manager()
        _btn(at, f"hro_{alert}").click().run()
        assert not at.exception, [str(e.value) for e in at.exception]
        md = _md(at)
        assert "case-summary-rail" in md, f"{alert}: Case File did not open"
        assert f"{customer} —" in md, f"{alert}: opened the wrong case"


# 7
def test_override_view_shows_override_cards_and_history():
    at = _override_view()
    md = _md(at)
    keys = {b.key for b in at.button if b.key}
    assert "Pending Override Requests" in md
    assert "Override History" in md and "<th>Request ID</th>" in md
    assert sum(1 for k in keys if k.startswith("apr_")) == 3   # three override cards
    assert any(k.startswith("oc_") for k in keys)
    assert any(k.startswith("rej_") for k in keys)


# 8
def test_override_view_hides_human_review_cards():
    md = _md(_override_view())
    assert "Human Review Oversight" not in md
    assert "ALERT007 · Roland Beck" not in md
    assert "ALERT001 · Dana Whitfield" not in md
    assert "ALERT004 · Tomas Herrera" not in md


# 9
def test_existing_override_open_case_callback_unchanged():
    at = _override_view()
    _btn(at, "oc_CHG-SEED-003").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert "Dana Whitfield —" in _md(at)            # CHG-SEED-003 -> ALERT001 case, callback intact


# 10
def test_switching_views_preserves_role_and_writes_no_audit(monkeypatch):
    import src.audit as audit_mod
    calls: list[dict] = []
    monkeypatch.setattr(audit_mod, "log_event", lambda **kw: calls.append(kw) or {})
    at = _manager()                                                       # human view
    at.segmented_control(key="manager_review_view").set_value("override_requests").run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert at.session_state["view_as"] == "Manager"                      # role preserved
    at.segmented_control(key="manager_review_view").set_value("human_reviews").run()
    _btn(at, "hro_ALERT007").click().run()                                # open a review case
    assert at.session_state["view_as"] == "Manager"
    assert calls == [], f"switching/opening wrote audit events: {calls}"


# 11
def test_alert004_remains_pass_complete_escalate_and_100_readiness():
    at = _manager()
    c4 = _card(at, "ALERT004 · Tomas Herrera")
    assert "AI VERIFICATION: PASS" in c4 and "REVIEW REQUIREMENTS: COMPLETE" in c4 and "RECORDED DISPOSITION: ESCALATE" in c4
    assert "RECORDED DISPOSITION: EDITED" not in c4
    _btn(at, "hro_ALERT004").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    md = _md(at)
    # "Case Readiness" was renamed to "Evidence Completeness" (percentage unchanged).
    assert "Tomas Herrera —" in md and "Evidence Completeness" in md and "100%" in md
    assert "Missing source evidence:" not in md


# 12 — every oversight badge is derived from the canonical lifecycle, not alerts.status
def test_hro_badges_derive_from_lifecycle_not_alerts_status():
    idx = {r.alert_id: r for r in load_lifecycle(DATA / "case_lifecycle.csv")}
    at = _manager()
    for alert, cust in (("ALERT001", "Dana Whitfield"), ("ALERT004", "Tomas Herrera"),
                        ("ALERT007", "Roland Beck")):
        card = _card(at, f"{alert} · {cust}")
        lc = idx[alert]
        assert f"AI VERIFICATION: {app.lifecycle_ai_verification_label(lc)}" in card
        assert f"REVIEW REQUIREMENTS: {app.lifecycle_review_requirements_label(lc)}" in card
        assert f"RECORDED DISPOSITION: {app.lifecycle_recorded_disposition_label(lc)}" in card
    # ALERT007 alerts.status is 'closed' and ALERT001/004 are 'in_review' — none of those
    # legacy statuses may surface as an oversight badge value.
    md = _md(at)
    for legacy in ("RECORDED DISPOSITION: OPEN", "RECORDED DISPOSITION: IN_REVIEW",
                   "RECORDED DISPOSITION: CLOSED", "RECORDED DISPOSITION: EDITED",
                   "RECORDED DISPOSITION: ACCEPTED"):
        assert legacy not in md


# 13 — opening each oversight Case File is read-only (no audit, no CSV mutation)
def test_opening_each_oversight_case_file_is_read_only(monkeypatch):
    import src.audit as audit_mod
    calls: list = []
    monkeypatch.setattr(audit_mod, "log_event", lambda **kw: calls.append(kw) or {})
    before = _committed_hashes()
    for alert in ("ALERT007", "ALERT001", "ALERT004"):
        at = _manager()
        _btn(at, f"hro_{alert}").click().run()
        assert not at.exception, [str(e.value) for e in at.exception]
        assert "case-summary-rail" in _md(at)
    assert calls == [], f"opening an oversight Case File wrote audit events: {calls}"
    assert _committed_hashes() == before
