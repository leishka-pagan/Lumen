"""Mutually exclusive dialog routing.

Streamlit raises "Only one dialog is allowed to be opened at the same time" if two
@st.dialog functions are invoked in one rerun. All four modals (Reset Demo,
Approve, Reject, Case File) are therefore invoked from a single if/elif/elif
coordinator (priority: Reset Demo -> Override decision -> Case File), and each
open-action clears the other modals' state first. These tests prove no run can
open two dialogs, and that opening a modal mutates nothing.

Isolation: the override CSV + audit log are redirected to temp files via env vars
(the `runtime` fixture); committed CSVs are never mutated.
"""

from __future__ import annotations

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


def _run(**state):
    at = AppTest.from_file(APP, default_timeout=120)
    for k, v in state.items():
        at.session_state[k] = v
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def _btn(at, key):
    return next(b for b in at.button if b.key == key)


def _keys(at):
    return {b.key for b in at.button if b.key}


def _dialogs(at):
    """Which modal(s) are currently rendered, detected by their unique buttons."""
    k = _keys(at)
    return {
        "case": "close_case_dialog" in k,
        "approve": any(x.startswith("override_submit_confirm_") and x.endswith("_approved") for x in k),
        "reject": any(x.startswith("override_submit_confirm_") and x.endswith("_rejected") for x in k),
        "reset": "demo_reset_go" in k,
    }


APPROVE = {"change_id": "CHG-SEED-001", "decision": "approved"}   # CHG-SEED-001 -> ALERT002
REJECT = {"change_id": "CHG-SEED-001", "decision": "rejected"}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    ov = tmp_path / "pending_overrides.csv"
    lg = tmp_path / "audit_log.csv"
    shutil.copy(DATA / "pending_overrides.csv", ov)
    monkeypatch.setenv("LUMEN_OVERRIDES_CSV", str(ov))
    monkeypatch.setenv("LUMEN_AUDIT_LOG", str(lg))
    return {"pending": ov, "audit": lg}


# 1–4: each dialog opens alone, without exception, and is the only one rendered
def test_case_file_alone_opens(runtime):
    assert _dialogs(_run(open_case="ALERT002")) == {"case": True, "approve": False, "reject": False, "reset": False}


def test_approve_alone_opens(runtime):
    d = _dialogs(_run(view_as="Manager", pending_override=dict(APPROVE)))
    assert d == {"case": False, "approve": True, "reject": False, "reset": False}


def test_reject_alone_opens(runtime):
    d = _dialogs(_run(view_as="Manager", pending_override=dict(REJECT)))
    assert d == {"case": False, "approve": False, "reject": True, "reset": False}


def test_reset_alone_opens(runtime):
    assert _dialogs(_run(demo_reset_confirm=True)) == {"case": False, "approve": False, "reject": False, "reset": True}


# 5–7: stale state combinations never open two dialogs (priority wins)
def test_stale_case_plus_approve_renders_only_approve(runtime):
    d = _dialogs(_run(view_as="Manager", open_case="ALERT001", pending_override=dict(APPROVE)))
    assert d == {"case": False, "approve": True, "reject": False, "reset": False}


def test_stale_case_plus_reject_renders_only_reject(runtime):
    d = _dialogs(_run(view_as="Manager", open_case="ALERT001", pending_override=dict(REJECT)))
    assert d == {"case": False, "approve": False, "reject": True, "reset": False}


def test_stale_case_and_override_plus_reset_renders_only_reset(runtime):
    d = _dialogs(_run(view_as="Manager", open_case="ALERT001",
                      pending_override=dict(APPROVE), demo_reset_confirm=True))
    assert d == {"case": False, "approve": False, "reject": False, "reset": True}


# 8: opening a Case File from Override Requests clears override-dialog state
def test_open_case_from_override_requests_clears_override_state(runtime):
    at = _run(view_as="Manager", manager_review_view="override_requests", pending_override=dict(REJECT))
    assert _dialogs(at)["reject"] is True                       # stale reject modal is showing
    _btn(at, "oc_CHG-SEED-001").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert at.session_state["pending_override"] is None         # override state cleared on open
    d = _dialogs(at)
    assert d["case"] is True and d["approve"] is False and d["reject"] is False


# 9: opening Approve/Reject clears Case File state
def test_open_reject_clears_case_file_state(runtime):
    at = _run(view_as="Manager", manager_review_view="override_requests", open_case="ALERT002")
    assert _dialogs(at)["case"] is True                         # stale Case File is showing
    _btn(at, "rej_CHG-SEED-001").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert at.session_state["open_case"] is None                # case state cleared on open
    assert at.session_state["selected_alert"] is None
    d = _dialogs(at)
    assert d["reject"] is True and d["case"] is False


# 10: merely opening any dialog writes no audit event
def test_no_dialog_opening_writes_audit(runtime):
    for state in [dict(open_case="ALERT002"),
                  dict(view_as="Manager", pending_override=dict(APPROVE)),
                  dict(view_as="Manager", pending_override=dict(REJECT)),
                  dict(demo_reset_confirm=True)]:
        _run(**state)
        assert not runtime["audit"].exists()                   # nothing written by opening a modal


# 11: cancelling a decision dialog mutates neither the CSV nor the audit log
def test_cancel_is_mutation_free(runtime):
    at = _run(view_as="Manager", manager_review_view="override_requests", pending_override=dict(APPROVE))
    _btn(at, "override_submit_cancel_CHG-SEED-001_approved").click().run()
    assert at.session_state["pending_override"] is None         # dialog closed
    df = pd.read_csv(runtime["pending"], dtype=str, keep_default_na=False)
    assert (df["status"] == "pending").all()                   # no decision persisted
    assert not runtime["audit"].exists()
