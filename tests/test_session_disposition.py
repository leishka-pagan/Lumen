"""Session-only human disposition — the demo affordance for Hero Moment 2.

A Case File whose STORED review is BLOCKED (ALERT007 / REV001) now offers a form.
Filling it records the values in ``st.session_state`` keyed by alert_id and the
gate panel flips to COMPLETE on screen. Nothing is persisted: no CSV write, no
audit event, no lifecycle mutation. The gate decision is delegated to the existing
``src.review_gate.evaluate_review`` — these tests pin that delegation rather than
re-deriving completeness.

A case whose stored review is already COMPLETE (ALERT001, ALERT004) is untouched:
no form, and byte-identical gate copy.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
from src.case_lifecycle import ReviewGateStatus  # noqa: E402
from src.lifecycle_store import load_lifecycle  # noqa: E402
from src.review_gate import evaluate_review  # noqa: E402

APP = str(ROOT / "app.py")
_SRC = (ROOT / "app.py").read_text(encoding="utf-8")

BLOCKED_ALERT = "ALERT007"          # stored REV001: every required field empty
COMPLETE_ALERTS = ["ALERT001", "ALERT004"]

COMPLETE_DISPOSITION = {
    "evidence_reviewed": True,
    "draft_disposition": "edited",
    "decision_reason": "Reviewed the three drafted claims against source transactions.",
    "final_note": "Enhanced monitoring continued; no SAR filed at this time.",
    "final_action": "monitor",
    "reviewer": "session:EMP-003",
}


# ── helpers ──────────────────────────────────────────────────────────────────
def _run(**state):
    at = AppTest.from_file(APP, default_timeout=120)
    for k, v in state.items():
        at.session_state[k] = v
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def _md(at) -> str:
    return " ".join(m.value for m in at.markdown)


def _case(alert_id, dispositions=None) -> str:
    state = {"open_case": alert_id}
    if dispositions is not None:
        state["session_dispositions"] = dispositions
    return _md(_run(**state))


def _committed_hashes() -> dict:
    return {f.name: hashlib.sha256(f.read_bytes()).hexdigest()
            for f in sorted(DATA.rglob("*")) if f.is_file()}


def _lifecycle() -> dict:
    return {r.alert_id: r for r in load_lifecycle(DATA / "case_lifecycle.csv")}


# ── the gate rule is delegated, never reimplemented ──────────────────────────
def test_gate_rule_is_imported_not_reimplemented():
    assert "from src.review_gate import evaluate_review" in _SRC
    # no local re-derivation of the five required fields
    for smell in ("def evaluate_review", "VALID_DISPOSITIONS ="):
        assert smell not in _SRC, f"gate rule appears duplicated in app.py: {smell}"


def test_review_gate_module_is_untouched():
    """The shared rule file must be byte-identical to main (compare raw bytes:
    decoding git's stdout through the console codepage corrupts non-ASCII)."""
    import subprocess
    committed = subprocess.run(
        ["git", "show", "3326a02:src/review_gate.py"],
        cwd=ROOT, capture_output=True, check=True).stdout
    on_disk = (ROOT / "src" / "review_gate.py").read_bytes()
    assert on_disk.replace(b"\r\n", b"\n") == committed.replace(b"\r\n", b"\n")


def test_effective_review_prefers_session_then_falls_back():
    stored = {"review_id": "REV001", "alert_id": BLOCKED_ALERT, "reviewer": "reviewer:jdoe",
              "evidence_reviewed": "False", "draft_disposition": "accepted",
              "decision_reason": "", "final_note": "", "final_action": ""}
    # no session disposition -> the stored row, unchanged object
    app.st.session_state["session_dispositions"] = {}
    assert app.effective_review(BLOCKED_ALERT, stored) is stored
    # with one -> the five gate fields come from the session
    app.st.session_state["session_dispositions"] = {BLOCKED_ALERT: dict(COMPLETE_DISPOSITION)}
    merged = app.effective_review(BLOCKED_ALERT, stored)
    for field in app.SESSION_DISPOSITION_FIELDS:
        assert merged[field] == COMPLETE_DISPOSITION[field]
    assert stored["final_action"] == "", "the stored row was mutated"
    app.st.session_state["session_dispositions"] = {}


def test_complete_session_disposition_satisfies_the_real_gate():
    """The shared rule — not app code — is what declares it complete."""
    result = evaluate_review(COMPLETE_DISPOSITION, enforce=True)
    assert result.allowed and not result.blocked and result.missing == ()


@pytest.mark.parametrize("drop, expected", [
    ("evidence_reviewed", "evidence_reviewed"),
    ("draft_disposition", "draft_disposition"),
    ("decision_reason", "decision_reason"),
    ("final_note", "final_note"),
    ("final_action", "final_action"),
])
def test_each_missing_field_blocks_and_is_named(drop, expected):
    partial = dict(COMPLETE_DISPOSITION)
    partial[drop] = False if drop == "evidence_reviewed" else ""
    result = evaluate_review(partial, enforce=True)
    assert result.blocked and expected in result.missing
    assert app.DISPOSITION_FIELD_LABELS[expected] in app.missing_disposition_labels(result)


# ── stored-blocked case: the form appears ────────────────────────────────────
def test_blocked_case_starts_blocked_and_offers_the_form():
    md = _case(BLOCKED_ALERT)
    assert "HUMAN-REVIEW GATE: BLOCKED" in md
    assert "Record disposition" in md
    assert "Session only" in md


def test_blocked_case_form_has_exactly_the_six_controls():
    """Exactly the six authorized controls, scoped to this alert's form — the page
    carries unrelated widgets (Risk Settings) that must not be counted or disturbed."""
    at = _run(open_case=BLOCKED_ALERT)
    form_id = f"disposition_form_{BLOCKED_ALERT}"

    def _in_form(widgets):
        return [w for w in widgets if getattr(w, "form_id", "") == form_id]

    boxes = _in_form(at.checkbox)
    assert [w.label for w in boxes] == ["Evidence reviewed"]
    assert [w.label for w in _in_form(at.selectbox)] == [
        "AI draft accepted / edited / rejected", "Final action"]
    assert [w.label for w in _in_form(at.text_area)] == [
        "Decision reason", "Final analyst note"]
    assert [w.label for w in _in_form(at.button)] == ["Record disposition"]
    # six controls in total, and nothing else inside the form
    assert sum(len(_in_form(g)) for g in
               (at.checkbox, at.selectbox, at.text_area, at.button,
                at.text_input, at.radio, at.multiselect)) == 6


def test_selects_offer_exactly_the_authorized_options():
    assert app.DISPOSITION_CHOICES == ("accepted", "edited", "rejected")
    assert app.FINAL_ACTION_CHOICES == ("monitor", "escalate", "close")


def test_selects_start_unselected_so_missing_fields_are_reachable():
    at = _run(open_case=BLOCKED_ALERT)
    form_id = f"disposition_form_{BLOCKED_ALERT}"
    picked = [s for s in at.selectbox if getattr(s, "form_id", "") == form_id]
    assert len(picked) == 2
    for s in picked:
        assert s.value is None, f"{s.label} should start empty"


# ── submitting complete values flips the gate ────────────────────────────────
def test_complete_session_disposition_flips_the_gate_to_complete():
    md = _case(BLOCKED_ALERT, {BLOCKED_ALERT: dict(COMPLETE_DISPOSITION)})
    assert "REVIEW REQUIREMENTS: COMPLETE" in md
    assert "HUMAN-REVIEW GATE: BLOCKED" not in md
    assert "gate-panel gate-complete" in md
    assert "Review decision recorded: <b>edited</b>" in md
    assert "Final case action: <b>monitor</b>" in md
    assert "Session-only demo disposition" in md      # provenance is stated on screen


def test_flipped_gate_does_not_claim_persistence():
    md = _case(BLOCKED_ALERT, {BLOCKED_ALERT: dict(COMPLETE_DISPOSITION)})
    assert "not written to human_reviews.csv" in md


def test_session_disposition_shows_in_the_human_review_card():
    md = _case(BLOCKED_ALERT, {BLOCKED_ALERT: dict(COMPLETE_DISPOSITION)})
    assert "session:EMP-003" in md
    assert COMPLETE_DISPOSITION["decision_reason"] in md


# ── submitting incomplete values keeps it blocked and names the gaps ─────────
@pytest.mark.parametrize("drop", list(app.SESSION_DISPOSITION_FIELDS))
def test_incomplete_session_disposition_stays_blocked_and_names_the_field(drop):
    partial = dict(COMPLETE_DISPOSITION)
    partial[drop] = False if drop == "evidence_reviewed" else ""
    md = _case(BLOCKED_ALERT, {BLOCKED_ALERT: partial})
    assert "HUMAN-REVIEW GATE: BLOCKED" in md
    assert "REVIEW REQUIREMENTS: COMPLETE" not in md
    assert "Disposition not recorded" in md
    assert app.DISPOSITION_FIELD_LABELS[drop] in md, f"{drop} not named on screen"


def test_empty_form_submission_names_every_missing_field():
    blank = {"evidence_reviewed": False, "draft_disposition": "", "decision_reason": "",
             "final_note": "", "final_action": "", "reviewer": "session:EMP-003"}
    md = _case(BLOCKED_ALERT, {BLOCKED_ALERT: blank})
    assert "HUMAN-REVIEW GATE: BLOCKED" in md
    for label in app.DISPOSITION_FIELD_LABELS.values():
        assert label in md, f"missing field not named: {label}"


# ── session scoping ──────────────────────────────────────────────────────────
def test_disposition_is_keyed_by_alert_and_does_not_leak():
    """A disposition on ALERT007 must not alter any other case."""
    store = {BLOCKED_ALERT: dict(COMPLETE_DISPOSITION)}
    for other in COMPLETE_ALERTS:
        md = _case(other, store)
        assert "Session-only demo disposition" not in md
        assert "session:EMP-003" not in md


def test_session_dispositions_defaults_to_empty():
    at = _run()
    assert at.session_state["session_dispositions"] == {}


# ── already-COMPLETE cases are untouched ─────────────────────────────────────
@pytest.mark.parametrize("alert_id", COMPLETE_ALERTS)
def test_complete_cases_show_no_form(alert_id):
    lc = _lifecycle()
    assert lc[alert_id].review_gate is ReviewGateStatus.COMPLETE
    at = _run(open_case=alert_id)
    assert not any(b.label == "Record disposition" for b in at.button)
    assert "Record disposition" not in _md(at)


@pytest.mark.parametrize("alert_id, decision, action", [
    ("ALERT001", "edited", "monitor"),
    ("ALERT004", "edited", "escalate"),
])
def test_complete_cases_keep_their_lifecycle_derived_copy(alert_id, decision, action):
    md = _case(alert_id)
    assert ("All required review fields are present. Review decision recorded: "
            f"<b>{decision}</b>. Final case action: <b>{action}</b>.") in md
    assert "Session-only demo disposition" not in md


# ── nothing is persisted ─────────────────────────────────────────────────────
def test_recording_a_disposition_writes_no_file_and_no_audit(monkeypatch):
    import src.audit as audit_mod
    calls: list = []
    monkeypatch.setattr(audit_mod, "log_event", lambda **kw: calls.append(kw) or {})
    before = _committed_hashes()
    md = _case(BLOCKED_ALERT, {BLOCKED_ALERT: dict(COMPLETE_DISPOSITION)})
    assert "REVIEW REQUIREMENTS: COMPLETE" in md          # it really rendered
    assert calls == [], f"a session disposition wrote audit events: {calls}"
    assert _committed_hashes() == before, "a committed data file changed"


def test_lifecycle_record_is_unchanged_by_a_session_disposition():
    """The flip is display-only: the canonical lifecycle still reads BLOCKED."""
    _case(BLOCKED_ALERT, {BLOCKED_ALERT: dict(COMPLETE_DISPOSITION)})
    lc = _lifecycle()
    assert lc[BLOCKED_ALERT].review_gate is ReviewGateStatus.BLOCKED
    assert lc[BLOCKED_ALERT].final_action is None


def test_form_code_performs_no_disk_write():
    """The form handler stores to session_state only."""
    start = _SRC.index("def render_disposition_form")
    end = _SRC.index("@st.dialog(\"Case File\"")
    body = _SRC[start:end]
    for smell in ("to_csv", "open(", "write_text", "log_event", "write_lifecycle"):
        assert smell not in body, f"disposition form performs I/O: {smell}"
    assert "st.session_state.session_dispositions[alert_id]" in body
