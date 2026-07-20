"""Case File — OVERRIDE REQUEST context panel.

The Case File shows the analyst's pending/decided override request for the open
alert, DERIVED LIVE from the runtime pending_overrides.csv. It is a governance
record DISTINCT from a Human Review: it never fabricates a HumanReview and never
changes the honest "no formal disposition review has been submitted" empty state.

Isolation: the app's override CSV + audit log are redirected to temp files via env
vars (the `runtime` fixture); committed CSVs are never mutated.
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


def _md(at):
    return " ".join(m.value for m in at.markdown)


def _ovreq(at):
    """Just the OVERRIDE REQUEST panel markdown (the single element carrying it)."""
    return " ".join(m.value for m in at.markdown if "ovreq-panel" in m.value)


def _run(**state):
    at = AppTest.from_file(APP, default_timeout=120)
    for k, v in state.items():
        at.session_state[k] = v
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def _btn(at, key):
    return next(b for b in at.button if b.key == key)


def _set_status(path, change_id, status, reviewed_by, reviewed_at, review_note):
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    m = df["change_id"] == change_id
    df.loc[m, "status"] = status
    df.loc[m, "reviewed_by"] = reviewed_by
    df.loc[m, "reviewed_at"] = reviewed_at
    df.loc[m, "review_note"] = review_note
    df.to_csv(path, index=False)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    """Temp runtime CSVs (pending = a copy of the committed baseline); env-redirected
    so the app reads them. Nothing here touches a committed CSV."""
    ov = tmp_path / "pending_overrides.csv"
    lg = tmp_path / "audit_log.csv"
    shutil.copy(DATA / "pending_overrides.csv", ov)
    monkeypatch.setenv("LUMEN_OVERRIDES_CSV", str(ov))
    monkeypatch.setenv("LUMEN_AUDIT_LOG", str(lg))
    return {"pending": ov, "audit": lg}


# 1 — ALERT002: pending override context AND honest "review still outstanding"
def test_alert002_shows_pending_override_and_no_human_review(runtime):
    md = _md(_run(open_case="ALERT002"))
    assert "OVERRIDE REQUEST" in md
    assert "PENDING MANAGER DECISION" in md
    assert "severity" in md
    assert "High → Medium" in md
    assert "Jordan Avery" in md and "EMP-204" in md
    assert "scholarship grant" in md
    # ALERT002 is MIXED -> routing REQUIRED with the gate still PENDING: the Case File must
    # say the disposition is withheld, and must never fabricate a review that is not on file.
    assert "No final disposition is accepted until a complete human review is submitted." in md
    assert "Human review was not required under deterministic routing policy" not in md
    assert "Human Review" not in _ovreq(_run(open_case="ALERT002"))  # not mislabeled


# 2 — ALERT005: pending override context AND honest "no formal disposition review"
def test_alert005_shows_pending_override_and_no_human_review(runtime):
    md = _md(_run(open_case="ALERT005"))
    assert "OVERRIDE REQUEST" in md
    assert "PENDING MANAGER DECISION" in md
    assert "disposition" in md
    assert "Open → Escalate" in md
    assert "Priya Raman" in md and "EMP-217" in md
    assert "high-risk jurisdiction" in md
    # Lifecycle NOT_REQUIRED: honest "no review required" state
    assert "Human review was not required under deterministic routing policy POL-REVIEW-ROUTING-V1." in md


# 3 — ALERT001: a completed Human Review AND a separate pending override coexist
def test_alert001_shows_both_human_review_and_pending_override(runtime):
    md = _md(_run(open_case="ALERT001"))
    assert "OVERRIDE REQUEST" in md and "PENDING MANAGER DECISION" in md
    assert "Residual risk assessed as medium" in md     # the override reason
    assert "reviewer:mchen" in md                       # the completed human review
    # ALERT001 is COMPLETE (a real review on file) — never a "no review required" state
    assert "Human review was not required under deterministic routing policy" not in md


# 4 — an APPROVED request keeps its panel, now green, with reviewer + rationale
def test_approved_override_shows_manager_rationale_and_reviewer(runtime):
    _set_status(runtime["pending"], "CHG-SEED-001", "approved",
                "M. Chen", "2026-06-01T10:00:00",
                "Downgrade justified after full evidence review.")
    ov = _ovreq(_run(open_case="ALERT002"))
    assert "APPROVED" in ov
    assert "ovreq-panel approved" in ov                 # green state
    assert "M. Chen" in ov
    assert "Downgrade justified after full evidence review." in ov
    assert "PENDING MANAGER DECISION" not in ov


# 5 — a REJECTED request keeps its panel, now red, with reviewer + rationale
def test_rejected_override_shows_manager_rationale_and_reviewer(runtime):
    _set_status(runtime["pending"], "CHG-SEED-002", "rejected",
                "M. Chen", "2026-06-02T11:00:00",
                "Escalation is not warranted at this time.")
    ov = _ovreq(_run(open_case="ALERT005"))
    assert "REJECTED" in ov
    assert "ovreq-panel rejected" in ov                 # red state
    assert "M. Chen" in ov
    assert "Escalation is not warranted at this time." in ov


# 6 — Reset Demo restores the pending presentation (panel derived from the CSV)
def test_reset_demo_restores_pending_presentation(runtime):
    _set_status(runtime["pending"], "CHG-SEED-001", "approved",
                "M. Chen", "2026-06-01T10:00:00", "Approved for the demo.")
    assert "APPROVED" in _ovreq(_run(open_case="ALERT002"))
    # drive the Reset Demo control (restores runtime CSVs from the committed baseline)
    at = _run()
    _btn(at, "demo_reset").click().run()
    _btn(at, "demo_reset_go").click().run()
    ov = _ovreq(_run(open_case="ALERT002"))
    assert "PENDING MANAGER DECISION" in ov
    assert "APPROVED" not in ov


# 7 — same Case File override context from Alert Queue and from Override Requests
def test_same_context_from_alert_queue_and_override_requests(runtime):
    # entry A: an Alert Queue selection sets open_case directly (app.py ~1559)
    ctx_a = _ovreq(_run(open_case="ALERT002"))
    # entry B: Manager ▸ Override Requests ▸ "Open Case File" (oc_<change_id>, ~2057)
    at_b = _run(view_as="Manager", manager_review_view="override_requests")
    _btn(at_b, "oc_CHG-SEED-001").click().run()
    ctx_b = _ovreq(at_b)
    assert ctx_a and ctx_b
    assert ctx_a == ctx_b                               # identical regardless of entry point
    assert "OVERRIDE REQUEST" in ctx_a and "High → Medium" in ctx_a


# 8 — merely opening a Case File writes no audit event
def test_opening_case_file_writes_no_audit_event(runtime):
    assert not runtime["audit"].exists()
    md = _md(_run(open_case="ALERT002"))
    assert "OVERRIDE REQUEST" in md                     # the case actually opened
    assert not runtime["audit"].exists()                # opening wrote nothing to the audit log
