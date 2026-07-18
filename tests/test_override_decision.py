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

APP = str(ROOT / "app.py")


def _committed():
    return pd.read_csv(DATA / "pending_overrides.csv", dtype=str, keep_default_na=False)


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
    shutil.copy(DATA / "pending_overrides.csv", ov)
    monkeypatch.setenv("LUMEN_OVERRIDES_CSV", str(ov))
    monkeypatch.setenv("LUMEN_AUDIT_LOG", str(lg))
    return {"overrides": ov, "audit": lg}


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
    ov = tmp_path / "po.csv"; lg = tmp_path / "audit.csv"
    shutil.copy(DATA / "pending_overrides.csv", ov)
    app.record_override_decision("CHG-SEED-001", "approved", reviewer="M. Chen",
                                 rationale="Verified; downgrade approved.",
                                 actor="ui:EMP-006", overrides_path=ov, log_path=lg)
    al = pd.read_csv(lg, dtype=str, keep_default_na=False)
    assert len(al) == 1 and al.iloc[0]["action"] == "override_review"
    d = json.loads(al.iloc[0]["details_json"])
    assert d["change_id"] == "CHG-SEED-001" and d["decision"] == "approved"
    assert d["rationale"] == "Verified; downgrade approved."
    assert d["old_value"] == "high" and d["new_value"] == "med"


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
