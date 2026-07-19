"""Manager override decision workflow — form-based required-rationale dialog.

The rationale + both actions live in one st.form, so the typed value is submitted
WITH the click (fixing the real-browser bug where a disabled= button never saw the
typed value). Validation happens on submit: a rationale is valid iff non-empty after
trimming (<=500 chars enforced by the textarea). The confirm button is never disabled.

Isolation: the app's override CSV + audit log are redirected to temp files via env
vars (temp_data fixture) or passed explicitly, so no committed CSV is ever mutated.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
from src import lifecycle_store  # noqa: E402
from src.case_lifecycle import (  # noqa: E402
    DispositionSource, LifecycleInvariantError, OverrideStatus, QueueStatus,
    derive_queue_status,
)

APP = str(ROOT / "app.py")


def _committed():
    return pd.read_csv(DATA / "pending_overrides.csv", dtype=str, keep_default_na=False)


def _committed_lifecycle():
    return pd.read_csv(DATA / "case_lifecycle.csv", dtype=str, keep_default_na=False)


def _lifecycle_rec(path, alert_id):
    return next(r for r in lifecycle_store.load_lifecycle(path) if r.alert_id == alert_id)


def _btn(at, key):
    return next(b for b in at.button if b.key == key)


def _md(at):
    return " ".join(m.value for m in at.markdown)


def _override_view(**state):
    at = AppTest.from_file(APP, default_timeout=120)
    at.session_state["view_as"] = "Manager"
    at.session_state["manager_review_view"] = "override_requests"
    for k, v in state.items():
        at.session_state[k] = v
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def _open(at, change_id, decision):
    key = f"apr_{change_id}" if decision == "approved" else f"rej_{change_id}"
    _btn(at, key).click().run()
    return at


def _submit(at, change_id, decision, rationale=None):
    """Type an optional rationale, then click the form's confirm submit button."""
    if rationale is not None:
        at.text_area[0].set_value(rationale)
    _btn(at, f"override_submit_confirm_{change_id}_{decision}").click()
    at.run()


def _required_error(at):
    return any("Manager decision rationale is required." in e.value for e in at.error)


@pytest.fixture
def temp_data(tmp_path, monkeypatch):
    ov = tmp_path / "pending_overrides.csv"
    lg = tmp_path / "audit_log.csv"
    lc = tmp_path / "case_lifecycle.csv"
    shutil.copy(DATA / "pending_overrides.csv", ov)
    shutil.copy(DATA / "case_lifecycle.csv", lc)                 # lifecycle synced on decision
    monkeypatch.setenv("LUMEN_OVERRIDES_CSV", str(ov))
    monkeypatch.setenv("LUMEN_AUDIT_LOG", str(lg))
    monkeypatch.setenv("LUMEN_CASE_LIFECYCLE_CSV", str(lc))
    return {"overrides": ov, "audit": lg, "lifecycle": lc}


# committed baseline
def test_committed_baseline_three_pending_empty_review_note():
    df = _committed()
    pend = df[df["status"] == "pending"]
    assert len(pend) == 3
    assert list(pend["change_id"]) == ["CHG-SEED-001", "CHG-SEED-002", "CHG-SEED-003"]
    assert (df["review_note"] == "").all()


# proof 1
def test_confirm_button_renders_enabled_before_rationale(temp_data):
    at = _override_view()
    _open(at, "CHG-SEED-001", "approved")
    conf = _btn(at, "override_submit_confirm_CHG-SEED-001_approved")
    assert conf.disabled is False                                  # always enabled
    assert (pd.read_csv(temp_data["overrides"], dtype=str, keep_default_na=False)["status"] == "pending").all()


# proof 2
def test_empty_rationale_submission_shows_required_error(temp_data):
    at = _override_view()
    _open(at, "CHG-SEED-001", "approved")
    _submit(at, "CHG-SEED-001", "approved")                       # no rationale
    assert _required_error(at)
    assert at.session_state["pending_override"] is not None       # dialog stays open


# proof 3
def test_whitespace_rationale_submission_shows_required_error(temp_data):
    at = _override_view()
    _open(at, "CHG-SEED-001", "approved")
    _submit(at, "CHG-SEED-001", "approved", "     ")
    assert _required_error(at)
    assert at.session_state["pending_override"] is not None


# proof 4
def test_invalid_submission_makes_no_csv_or_audit_change(temp_data):
    at = _override_view()
    _open(at, "CHG-SEED-001", "approved")
    _submit(at, "CHG-SEED-001", "approved", "   ")               # whitespace -> invalid
    assert (pd.read_csv(temp_data["overrides"], dtype=str, keep_default_na=False)["status"] == "pending").all()
    assert not temp_data["audit"].exists()


# proof 5
def test_nonempty_rationale_successfully_approves(temp_data):
    at = _override_view()
    _open(at, "CHG-SEED-001", "approved")
    _submit(at, "CHG-SEED-001", "approved", "Supporting evidence justifies the requested downgrade.")
    assert not at.exception
    row = pd.read_csv(temp_data["overrides"], dtype=str, keep_default_na=False).set_index("change_id").loc["CHG-SEED-001"]
    assert row["status"] == "approved"
    assert row["reviewed_by"] != "" and row["reviewed_at"] != ""
    assert row["review_note"] == "Supporting evidence justifies the requested downgrade."
    assert (_committed()["status"] == "pending").all()            # committed untouched


# proof 6
def test_nonempty_rationale_successfully_rejects(temp_data):
    at = _override_view()
    _open(at, "CHG-SEED-002", "rejected")
    _submit(at, "CHG-SEED-002", "rejected", "Escalation is not warranted at this time.")
    assert not at.exception
    row = pd.read_csv(temp_data["overrides"], dtype=str, keep_default_na=False).set_index("change_id").loc["CHG-SEED-002"]
    assert row["status"] == "rejected"
    assert row["reviewed_by"] != "" and row["reviewed_at"] != ""
    assert row["review_note"] == "Escalation is not warranted at this time."


# proof 7
def test_rationale_persists_in_override_history(temp_data):
    at = _override_view()
    _open(at, "CHG-SEED-002", "rejected")
    _submit(at, "CHG-SEED-002", "rejected", "Escalation not warranted; enhanced monitoring instead.")
    md = _md(at)
    assert "Override History" in md and "Decision Rationale" in md
    assert "CHG-SEED-002" in md
    assert "Escalation not warranted; enhanced monitoring instead." in md


# proof 8
def test_cancel_makes_no_mutation(temp_data):
    at = _override_view()
    _open(at, "CHG-SEED-001", "approved")
    at.text_area[0].set_value("Typed, but cancelling.")
    _btn(at, "override_submit_cancel_CHG-SEED-001_approved").click()
    at.run()
    assert at.session_state["pending_override"] is None           # dialog closed
    assert (pd.read_csv(temp_data["overrides"], dtype=str, keep_default_na=False)["status"] == "pending").all()
    assert not temp_data["audit"].exists()


# proof 9
def test_override_requests_remains_selected_after_success(temp_data):
    at = _override_view()
    _open(at, "CHG-SEED-001", "approved")
    _submit(at, "CHG-SEED-001", "approved", "Downgrade justified after full evidence review.")
    assert not at.exception
    assert at.session_state["manager_review_view"] == "override_requests"


# supporting: persistence + audit unit coverage (record_override_decision, temp paths)
def test_record_decision_writes_one_audit_with_rationale_and_change(tmp_path):
    ov = tmp_path / "po.csv"; lg = tmp_path / "audit.csv"; lc = tmp_path / "case_lifecycle.csv"
    shutil.copy(DATA / "pending_overrides.csv", ov)
    shutil.copy(DATA / "case_lifecycle.csv", lc)
    app.record_override_decision("CHG-SEED-001", "approved", reviewer="M. Chen",
                                 rationale="Verified; downgrade approved.",
                                 actor="ui:EMP-006", overrides_path=ov, log_path=lg,
                                 lifecycle_path=lc)
    al = pd.read_csv(lg, dtype=str, keep_default_na=False)
    assert len(al) == 1 and al.iloc[0]["action"] == "override_review"
    d = json.loads(al.iloc[0]["details_json"])
    assert d["change_id"] == "CHG-SEED-001" and d["decision"] == "approved"
    assert d["rationale"] == "Verified; downgrade approved."
    assert d["old_value"] == "high" and d["new_value"] == "med"


# ── Lifecycle synchronization through record_override_decision (temp paths) ───
def test_decision_syncs_lifecycle_pending_and_audit_together(tmp_path):
    ov = tmp_path / "po.csv"; lg = tmp_path / "audit.csv"; lc = tmp_path / "case_lifecycle.csv"
    shutil.copy(DATA / "pending_overrides.csv", ov)
    shutil.copy(DATA / "case_lifecycle.csv", lc)
    committed_lc_before = _committed_lifecycle()
    app.record_override_decision("CHG-SEED-002", "approved", reviewer="M. Chen",
                                 rationale="Escalation justified.", actor="ui:EMP-006",
                                 overrides_path=ov, log_path=lg, lifecycle_path=lc)
    # pending updated
    po = pd.read_csv(ov, dtype=str, keep_default_na=False).set_index("change_id").loc["CHG-SEED-002"]
    assert po["status"] == "approved"
    # audit written
    assert pd.read_csv(lg, dtype=str, keep_default_na=False).iloc[0]["action"] == "override_review"
    # lifecycle synced: manager-override disposition, derives CLOSED
    rec = _lifecycle_rec(lc, "ALERT005")
    assert rec.override_status is OverrideStatus.APPROVED
    assert rec.final_action == "escalate"
    assert rec.disposition_source is DispositionSource.MANAGER_OVERRIDE
    assert rec.disposition_reference == "CHG-SEED-002"
    assert derive_queue_status(rec) is QueueStatus.CLOSED
    # committed lifecycle untouched
    assert _committed_lifecycle().equals(committed_lc_before)


def test_non_disposition_decision_preserves_lifecycle_provenance(tmp_path):
    ov = tmp_path / "po.csv"; lg = tmp_path / "audit.csv"; lc = tmp_path / "case_lifecycle.csv"
    shutil.copy(DATA / "pending_overrides.csv", ov)
    shutil.copy(DATA / "case_lifecycle.csv", lc)
    app.record_override_decision("CHG-SEED-003", "approved", reviewer="M. Chen",
                                 rationale="Risk rating downgrade confirmed.", actor="ui:EMP-006",
                                 overrides_path=ov, log_path=lg, lifecycle_path=lc)
    rec = _lifecycle_rec(lc, "ALERT001")                 # risk_rating override (non-disposition)
    assert rec.override_status is OverrideStatus.APPROVED
    assert rec.final_action == "monitor"                 # human-review action preserved
    assert rec.disposition_source is DispositionSource.HUMAN_REVIEW
    assert rec.disposition_reference == "REV003"
    assert derive_queue_status(rec) is QueueStatus.CLOSED


def test_lifecycle_validation_failure_writes_nothing_anywhere(tmp_path):
    # Force an invalid update: relabel CHG-SEED-003 (ALERT001, REQUIRED/COMPLETE) as a
    # 'disposition' override. Approving it would move provenance to MANAGER_OVERRIDE on a
    # COMPLETE record -> invariant failure. NOTHING may change.
    ov = tmp_path / "po.csv"; lg = tmp_path / "audit.csv"; lc = tmp_path / "case_lifecycle.csv"
    shutil.copy(DATA / "pending_overrides.csv", ov)
    shutil.copy(DATA / "case_lifecycle.csv", lc)
    po = pd.read_csv(ov, dtype=str, keep_default_na=False)
    po.loc[po["change_id"] == "CHG-SEED-003", "field_changed"] = "disposition"
    po.loc[po["change_id"] == "CHG-SEED-003", "new_value"] = "escalate"
    po.to_csv(ov, index=False)
    pending_before, lc_before = ov.read_bytes(), lc.read_bytes()
    with pytest.raises(LifecycleInvariantError):
        app.record_override_decision("CHG-SEED-003", "approved", reviewer="M. Chen",
                                     rationale="Should fail.", actor="ui:EMP-006",
                                     overrides_path=ov, log_path=lg, lifecycle_path=lc)
    assert ov.read_bytes() == pending_before             # pending unchanged
    assert lc.read_bytes() == lc_before                  # lifecycle unchanged
    assert not lg.exists()                               # no audit event


def test_committed_lifecycle_and_overrides_never_mutated_by_decisions(temp_data):
    lc_before = _committed_lifecycle()
    at = _override_view()
    _open(at, "CHG-SEED-001", "approved")
    _submit(at, "CHG-SEED-001", "approved", "Downgrade justified after full evidence review.")
    assert not at.exception
    assert (_committed()["status"] == "pending").all()   # committed overrides untouched
    assert _committed_lifecycle().equals(lc_before)       # committed lifecycle untouched


# supporting: existing behavior unchanged
def test_existing_open_case_file_routing_correct(temp_data):
    at = _override_view()
    _btn(at, "oc_CHG-SEED-003").click().run()
    assert not at.exception
    assert "Dana Whitfield —" in _md(at)             # CHG-SEED-003 -> ALERT001 case


def test_human_review_oversight_unchanged():
    at = AppTest.from_file(APP, default_timeout=120)
    at.session_state["view_as"] = "Manager"           # default subview = human_reviews
    at.run()
    assert not at.exception
    md = _md(at)
    assert "Human Review Oversight" in md
    assert "ALERT007 · Roland Beck" in md
    assert "ALERT001 · Dana Whitfield" in md
    assert "ALERT004 · Tomas Herrera" in md
    assert "Pending Override Requests" not in md
