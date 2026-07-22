"""Analyst disposition coverage for alerts with no completed stored review.

The disposition form used to render only where the STORED review gate was BLOCKED
(ALERT007). The primary investigation workflow — an alert with no HumanReview row at
all, such as ALERT008 or ALERT011 — had no Analyst UI. The form now renders whenever
the current role is Analyst and the alert has no COMPLETE stored review, which covers:

  * no stored review at all (ALERT008, ALERT011);
  * a stored-but-incomplete review (ALERT007);
  * an alert already completed through a session disposition, kept editable so the
    analyst can correct and re-submit it.

A COMPLETE stored review (ALERT001, ALERT004) stays authoritative and renders no form,
and the Manager role never receives edit controls.

Everything remains session-only: no CSV row, no audit event, no override record, no
lifecycle mutation. Submissions here are driven through the REAL form widgets and the
real submit button, not by writing session state directly.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
from src.lifecycle_store import load_lifecycle  # noqa: E402
from src.review_gate import evaluate_review  # noqa: E402

APP = str(ROOT / "app.py")
_SRC = (ROOT / "app.py").read_text(encoding="utf-8")

NO_REVIEW = "ALERT008"          # no HumanReview row
OTHER_NO_REVIEW = "ALERT011"    # no HumanReview row
STORED_INCOMPLETE = "ALERT007"  # REV001, every required field empty
STORED_COMPLETE = ["ALERT001", "ALERT004"]

# Exact wording ALERT008 / ALERT011 render today, preserved byte-for-byte.
PENDING_HR_TEXT = "Human review is required and awaiting submission."
PENDING_GATE_TITLE = "HUMAN-REVIEW GATE: PENDING"
PENDING_GATE_BODY = ("No final disposition is accepted until a complete human review "
                     "is submitted.")

FORM_LABELS = {"Evidence reviewed", "AI draft accepted / edited / rejected",
               "Decision reason", "Final analyst note", "Final action",
               "Record disposition"}

FIRST = {"draft": "edited", "reason": "First rationale: claims checked against source.",
         "note": "First note: monitoring continued.", "action": "escalate"}
SECOND = {"draft": "rejected", "reason": "Corrected rationale: volume claim contradicted.",
          "note": "Corrected note: escalating for SAR consideration.", "action": "close"}


# ── helpers ──────────────────────────────────────────────────────────────────
def _fresh(role: str = "Analyst") -> AppTest:
    at = AppTest.from_file(APP, default_timeout=120)
    at.session_state["view_as"] = role
    return at


def _open(at: AppTest, alert_id: str) -> AppTest:
    """Open a Case File. The dialog coordinator consumes open_case each run, so it is
    re-armed on every call — the same thing the real app's rerun does."""
    at.session_state["open_case"] = alert_id
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def _md(at: AppTest) -> str:
    return " ".join(m.value for m in at.markdown)


def _case(alert_id: str, role: str = "Analyst") -> str:
    return _md(_open(_fresh(role), alert_id))


def _has_form(at: AppTest) -> bool:
    return any(b.label == "Record disposition" for b in at.button)


def _form_widgets(at: AppTest, alert_id: str) -> set[str]:
    """Every control inside this alert's form, scoped by form_id so unrelated page
    widgets (Risk Settings) are never counted."""
    fid = f"disposition_form_{alert_id}"
    got = []
    for group in (at.checkbox, at.selectbox, at.text_area, at.button):
        got += [w.label for w in group if getattr(w, "form_id", "") == fid]
    assert len(got) == 6, f"expected exactly 6 controls, got {got}"
    return set(got)


def _submit(at: AppTest, alert_id: str, values: dict) -> AppTest:
    """Fill and submit the REAL form, then re-open the Case File."""
    at.checkbox(key=f"disp_evidence_{alert_id}").set_value(True)
    at.selectbox(key=f"disp_draft_{alert_id}").select(values["draft"])
    at.text_area(key=f"disp_reason_{alert_id}").set_value(values["reason"])
    at.text_area(key=f"disp_note_{alert_id}").set_value(values["note"])
    at.selectbox(key=f"disp_action_{alert_id}").select(values["action"])
    next(b for b in at.button if b.label == "Record disposition").click()
    return _open(at, alert_id)


def _prefill(at: AppTest, alert_id: str) -> dict:
    return {
        "evidence": at.checkbox(key=f"disp_evidence_{alert_id}").value,
        "draft": at.selectbox(key=f"disp_draft_{alert_id}").value,
        "reason": at.text_area(key=f"disp_reason_{alert_id}").value,
        "note": at.text_area(key=f"disp_note_{alert_id}").value,
        "action": at.selectbox(key=f"disp_action_{alert_id}").value,
    }


def _pair(label: str, value: str) -> str:
    return f'case-summary-label">{label}</div><div class="case-summary-value">{value}</div>'


def _rail(md: str) -> dict:
    import re
    return dict(re.findall(
        r'case-summary-label">([A-Z ]+)</div><div class="case-summary-value">([^<]*)</div>', md))


def _tracked_data_hashes() -> dict:
    rel = subprocess.run(["git", "ls-files", "data"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout.split()
    return {r: hashlib.sha256((ROOT / r).read_bytes()).hexdigest() for r in sorted(rel)}


def _overrides() -> pd.DataFrame:
    return pd.read_csv(DATA / "pending_overrides.csv", dtype=str).fillna("")


# ═══ 1 — Analyst, ALERT008, no stored review, before submission ══════════════
def test_alert008_has_no_stored_review_row():
    hr = pd.read_csv(DATA / "human_reviews.csv", dtype=str).fillna("")
    assert len(hr[hr["alert_id"] == NO_REVIEW]) == 0


def test_unreviewed_alert_shows_the_form_for_an_analyst():
    at = _open(_fresh(), NO_REVIEW)
    assert _has_form(at)
    assert _form_widgets(at, NO_REVIEW) == FORM_LABELS


def test_unreviewed_alert_keeps_its_no_review_wording_before_submission():
    md = _case(NO_REVIEW)
    assert PENDING_HR_TEXT in md
    assert PENDING_GATE_TITLE in md
    assert PENDING_GATE_BODY in md
    assert "gate-panel gate-empty" in md


def test_unreviewed_alert_rail_is_unchanged_before_submission():
    lc = {r.alert_id: r for r in load_lifecycle(DATA / "case_lifecycle.csv")}[NO_REVIEW]
    rail = _rail(_case(NO_REVIEW))
    assert rail["AI DRAFT ACCURACY"] == app.lifecycle_ai_verification_label(lc)
    assert rail["REVIEW REQUIREMENTS"] == app.lifecycle_review_requirements_label(lc) == "PENDING"
    assert rail["RECORDED DISPOSITION"] == app.lifecycle_recorded_disposition_label(lc) == "NONE"
    assert app.SESSION_DISPOSITION_NOTE not in _case(NO_REVIEW)


def test_rendering_creates_no_session_disposition_or_effective_review():
    at = _open(_fresh(), NO_REVIEW)
    assert at.session_state["session_dispositions"] == {}
    assert app.effective_review(NO_REVIEW, None) is None      # absence stays absence


def test_absence_is_not_converted_into_blocked():
    md = _case(NO_REVIEW)
    assert "HUMAN-REVIEW GATE: BLOCKED" not in md
    assert _pair("REVIEW REQUIREMENTS", "BLOCKED") not in md


def test_opening_writes_no_audit_override_or_data(monkeypatch):
    import src.audit as audit_mod
    calls: list = []
    monkeypatch.setattr(audit_mod, "log_event", lambda **kw: calls.append(kw) or {})
    before, ov = _tracked_data_hashes(), _overrides()
    _case(NO_REVIEW)
    assert calls == []
    assert _tracked_data_hashes() == before
    assert _overrides().equals(ov)


# ═══ 2 — complete ALERT008 session submission ════════════════════════════════
def test_complete_submission_drives_gate_rail_and_details():
    at = _submit(_open(_fresh(), NO_REVIEW), NO_REVIEW, FIRST)
    md = _md(at)
    stored = at.session_state["session_dispositions"][NO_REVIEW]
    assert not evaluate_review(stored, enforce=True).missing      # effective review complete
    assert "REVIEW REQUIREMENTS: COMPLETE" in md
    assert _pair("RECORDED DISPOSITION", "ESCALATE") in md
    assert _rail(md)["REVIEW REQUIREMENTS"] == "COMPLETE"
    assert app.SESSION_DISPOSITION_NOTE in md
    assert FIRST["reason"] in md and FIRST["note"] in md
    assert PENDING_GATE_TITLE not in md


def test_complete_submission_persists_nothing(monkeypatch):
    import src.audit as audit_mod
    calls: list = []
    monkeypatch.setattr(audit_mod, "log_event", lambda **kw: calls.append(kw) or {})
    before, ov = _tracked_data_hashes(), _overrides()
    hr_before = (DATA / "human_reviews.csv").read_bytes()
    at = _submit(_open(_fresh(), NO_REVIEW), NO_REVIEW, FIRST)
    assert "REVIEW REQUIREMENTS: COMPLETE" in _md(at)          # it really happened
    assert calls == [], f"audit events written: {calls}"
    assert (DATA / "human_reviews.csv").read_bytes() == hr_before
    assert _overrides().equals(ov)
    assert _tracked_data_hashes() == before


def test_session_review_carries_no_persistent_review_id():
    at = _submit(_open(_fresh(), NO_REVIEW), NO_REVIEW, FIRST)
    stored = at.session_state["session_dispositions"][NO_REVIEW]
    assert "review_id" not in stored
    assert evaluate_review(stored, enforce=True).review_id is None


# ═══ 3 — form remains editable after session completion ══════════════════════
def test_form_remains_visible_and_prefilled_after_completion():
    at = _submit(_open(_fresh(), NO_REVIEW), NO_REVIEW, FIRST)
    assert _has_form(at), "session-complete must stay editable"
    assert _form_widgets(at, NO_REVIEW) == FORM_LABELS
    assert _prefill(at, NO_REVIEW) == {
        "evidence": True, "draft": FIRST["draft"], "reason": FIRST["reason"],
        "note": FIRST["note"], "action": FIRST["action"]}


def test_session_complete_is_not_treated_as_stored_complete():
    """The distinction that keeps the form editable."""
    assert "_stored_complete = (_stored_review is not None" in _SRC
    assert "and not _stored_complete)" in _SRC
    hr = pd.read_csv(DATA / "human_reviews.csv", dtype=str).fillna("")
    assert len(hr[hr["alert_id"] == NO_REVIEW]) == 0           # still no stored row
    at = _submit(_open(_fresh(), NO_REVIEW), NO_REVIEW, FIRST)
    assert _has_form(at)


# ═══ 4 — re-submission overwrites, through the real UI ═══════════════════════
def test_resubmission_replaces_the_prior_disposition():
    at = _submit(_open(_fresh(), NO_REVIEW), NO_REVIEW, FIRST)
    at = _submit(at, NO_REVIEW, SECOND)               # correct without reloading
    md = _md(at)

    store = at.session_state["session_dispositions"]
    assert list(store) == [NO_REVIEW], "more than one alert keyed"
    assert len(store) == 1
    entry = store[NO_REVIEW]
    assert entry["decision_reason"] == SECOND["reason"]
    assert entry["final_note"] == SECOND["note"]
    assert entry["draft_disposition"] == SECOND["draft"]
    assert entry["final_action"] == SECOND["action"]

    # only the second set is visible anywhere
    assert SECOND["reason"] in md and SECOND["note"] in md
    assert FIRST["reason"] not in md, "stale rationale still rendered"
    assert FIRST["note"] not in md, "stale note still rendered"
    assert _pair("RECORDED DISPOSITION", "CLOSE") in md
    assert _pair("RECORDED DISPOSITION", "ESCALATE") not in md
    assert f"Review decision recorded: <b>{SECOND['draft']}</b>" in md
    assert _prefill(at, NO_REVIEW)["reason"] == SECOND["reason"]
    assert _has_form(at)


def test_resubmission_creates_exactly_one_entry():
    at = _submit(_open(_fresh(), NO_REVIEW), NO_REVIEW, FIRST)
    at = _submit(at, NO_REVIEW, SECOND)
    at = _submit(at, NO_REVIEW, FIRST)
    assert len(at.session_state["session_dispositions"]) == 1
    assert len(at.session_state["session_dispositions"][NO_REVIEW]) == 6


# ═══ 5 — alert isolation ═════════════════════════════════════════════════════
def test_submitting_one_alert_leaves_another_untouched():
    at = _submit(_open(_fresh(), NO_REVIEW), NO_REVIEW, FIRST)
    at = _open(at, OTHER_NO_REVIEW)
    md = _md(at)

    assert OTHER_NO_REVIEW not in at.session_state["session_dispositions"]
    assert list(at.session_state["session_dispositions"]) == [NO_REVIEW]
    assert PENDING_HR_TEXT in md and PENDING_GATE_TITLE in md
    assert _rail(md)["REVIEW REQUIREMENTS"] == "PENDING"
    assert _rail(md)["RECORDED DISPOSITION"] == "NONE"
    assert app.SESSION_DISPOSITION_NOTE not in md
    for leaked in (FIRST["reason"], FIRST["note"]):
        assert leaked not in md, "values leaked across Case Files"
    assert _prefill(at, OTHER_NO_REVIEW) == {
        "evidence": False, "draft": None, "reason": "", "note": "", "action": None}


def test_editing_one_alert_never_alters_another_alerts_widgets():
    at = _submit(_open(_fresh(), NO_REVIEW), NO_REVIEW, FIRST)
    at = _open(at, OTHER_NO_REVIEW)
    at = _submit(at, OTHER_NO_REVIEW, SECOND)
    store = at.session_state["session_dispositions"]
    assert store[NO_REVIEW]["decision_reason"] == FIRST["reason"]
    assert store[OTHER_NO_REVIEW]["decision_reason"] == SECOND["reason"]
    assert len(store) == 2


# ═══ 6 — Manager restrictions ════════════════════════════════════════════════
@pytest.mark.parametrize("alert_id", [NO_REVIEW, STORED_INCOMPLETE])
def test_manager_never_sees_the_form_before_submission(alert_id):
    at = _open(_fresh("Manager"), alert_id)
    assert not _has_form(at)
    assert "Record disposition" not in _md(at)


def test_manager_sees_no_form_but_can_read_the_session_review():
    at = _submit(_open(_fresh(), NO_REVIEW), NO_REVIEW, FIRST)
    at.session_state["view_as"] = "Manager"
    at = _open(at, NO_REVIEW)
    md = _md(at)
    assert not _has_form(at), "manager received edit controls"
    assert "Record disposition" not in md
    # ...but the overlay is readable
    assert "REVIEW REQUIREMENTS: COMPLETE" in md
    assert FIRST["reason"] in md and FIRST["note"] in md
    assert _pair("RECORDED DISPOSITION", "ESCALATE") in md
    assert app.SESSION_DISPOSITION_NOTE in md


def test_role_switching_preserves_the_disposition_and_restores_the_form():
    at = _submit(_open(_fresh(), NO_REVIEW), NO_REVIEW, FIRST)
    at.session_state["view_as"] = "Manager"
    at = _open(at, NO_REVIEW)
    assert at.session_state["session_dispositions"][NO_REVIEW]["decision_reason"] == FIRST["reason"]
    at.session_state["view_as"] = "Analyst"
    at = _open(at, NO_REVIEW)
    assert _has_form(at)
    assert at.session_state["session_dispositions"][NO_REVIEW]["decision_reason"] == FIRST["reason"]


# ═══ 7 — existing alert behavior ═════════════════════════════════════════════
def test_alert007_analyst_behaviour_is_unchanged():
    at = _open(_fresh(), STORED_INCOMPLETE)
    md = _md(at)
    assert "HUMAN-REVIEW GATE: BLOCKED" in md
    assert "reviewer:jdoe" in md
    assert _has_form(at)
    assert _form_widgets(at, STORED_INCOMPLETE) == FORM_LABELS


def test_alert007_session_completion_remains_editable():
    at = _submit(_open(_fresh(), STORED_INCOMPLETE), STORED_INCOMPLETE, FIRST)
    md = _md(at)
    assert "REVIEW REQUIREMENTS: COMPLETE" in md
    assert _has_form(at)
    assert _prefill(at, STORED_INCOMPLETE)["reason"] == FIRST["reason"]


@pytest.mark.parametrize("alert_id", STORED_COMPLETE)
def test_stored_complete_alerts_show_no_form(alert_id):
    at = _open(_fresh(), alert_id)
    assert not _has_form(at)
    assert "Record disposition" not in _md(at)


@pytest.mark.parametrize("alert_id, req, disp", [("ALERT001", "COMPLETE", "MONITOR"),
                                                 ("ALERT004", "COMPLETE", "ESCALATE")])
def test_stored_complete_rail_and_gate_unchanged(alert_id, req, disp):
    md = _case(alert_id)
    assert _pair("REVIEW REQUIREMENTS", req) in md
    assert _pair("RECORDED DISPOSITION", disp) in md
    assert "REVIEW REQUIREMENTS: COMPLETE" in md
    assert app.SESSION_DISPOSITION_NOTE not in md
    assert "rail-session-note" not in md


def test_stored_complete_review_values_are_untouched():
    hr = pd.read_csv(DATA / "human_reviews.csv", dtype=str).fillna("")
    for alert_id, reviewer in (("ALERT001", "reviewer:mchen"), ("ALERT004", "reviewer:asmith")):
        row = hr[hr["alert_id"] == alert_id].iloc[0]
        assert row["reviewer"] == reviewer
        assert not evaluate_review(row.to_dict(), enforce=True).missing
        assert reviewer in _case(alert_id)


# ═══ 8 — separation from Override Requests ═══════════════════════════════════
def test_escalation_creates_no_override_record(monkeypatch):
    calls: list = []
    for fn in ("save_override", "update_override_status", "record_override_decision"):
        monkeypatch.setattr(app, fn, lambda *a, _n=fn, **k: calls.append(_n))
    ov_before = _overrides()
    at = _submit(_open(_fresh(), NO_REVIEW), NO_REVIEW, FIRST)   # final action = escalate
    assert _pair("RECORDED DISPOSITION", "ESCALATE") in _md(at)
    assert calls == [], f"override persistence invoked: {calls}"
    assert _overrides().equals(ov_before)


def test_session_disposition_is_not_an_override_identifier():
    at = _submit(_open(_fresh(), NO_REVIEW), NO_REVIEW, FIRST)
    entry = at.session_state["session_dispositions"][NO_REVIEW]
    blob = " ".join(str(v) for v in entry.values())
    assert "CHG-" not in blob and "OVR-" not in blob
    assert set(entry) == set(app.SESSION_DISPOSITION_FIELDS) | {"reviewer"}


def test_session_disposition_does_not_render_in_override_requests():
    at = _submit(_open(_fresh(), NO_REVIEW), NO_REVIEW, FIRST)
    at.session_state["view_as"] = "Manager"
    at.session_state["open_case"] = None
    at.run()
    override_panels = [m.value for m in at.markdown if "ovreq" in m.value]
    for panel in override_panels:
        assert FIRST["reason"] not in panel
        assert FIRST["note"] not in panel
        assert "session:" not in panel


def test_form_handler_touches_no_override_or_persistence_call():
    body = _SRC[_SRC.index("def render_disposition_form"):_SRC.index('@st.dialog("Case File"')]
    for smell in ("save_override", "record_override_decision", "update_override_status",
                  "to_csv", "log_event", "write_lifecycle", "OVERRIDES_CSV"):
        assert smell not in body, f"disposition form reaches {smell}"


# ═══ 9 — open and close without submitting ═══════════════════════════════════
def test_open_then_close_without_submitting_mutates_nothing(monkeypatch):
    import src.audit as audit_mod
    calls: list = []
    monkeypatch.setattr(audit_mod, "log_event", lambda **kw: calls.append(kw) or {})
    before, ov = _tracked_data_hashes(), _overrides()

    at = _open(_fresh(), NO_REVIEW)
    next(b for b in at.button if b.label == "Close").click()
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]

    assert at.session_state["session_dispositions"] == {}
    assert at.session_state["open_case"] is None
    assert calls == []
    assert _overrides().equals(ov)
    assert _tracked_data_hashes() == before


def test_src_tree_is_untouched():
    for name in ("review_gate.py", "verifier.py", "pipeline.py", "schema.py", "audit.py"):
        committed = subprocess.run(["git", "show", f"84c5b6b:src/{name}"],
                                   cwd=ROOT, capture_output=True, check=True).stdout
        on_disk = (ROOT / "src" / name).read_bytes()
        assert on_disk.replace(b"\r\n", b"\n") == committed.replace(b"\r\n", b"\n"), name
