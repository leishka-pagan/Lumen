"""The disposition form is gated on routing: it appears only where a human review is
REQUIRED.

Coverage used to be "Analyst AND no complete stored review", which also matched the two
NOT_REQUIRED alerts (ALERT003, ALERT005). NOT_REQUIRED means the deterministic routing
policy already authorized a system disposition — offering a review form there would
contradict the routing decision the gate exists to enforce, and ALERT005 is on screen
during the presentation.

The render condition is now the conjunction of three independent facts:

    role is Analyst  AND  review_routing is REQUIRED  AND  no COMPLETE stored review

Alerts that were already covered (ALERT007 stored-but-incomplete, ALERT008 / ALERT011
with no stored row) are untouched, and ALERT001 / ALERT004 still show nothing.
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
from src.case_lifecycle import ReviewGateStatus, ReviewRoutingStatus  # noqa: E402
from src.lifecycle_store import load_lifecycle  # noqa: E402
from src.review_gate import evaluate_review  # noqa: E402

APP = str(ROOT / "app.py")
_SRC = (ROOT / "app.py").read_text(encoding="utf-8")

NOT_REQUIRED = ["ALERT003", "ALERT005"]
NO_STORED_REVIEW = ["ALERT008", "ALERT011"]
STORED_INCOMPLETE = "ALERT007"
STORED_COMPLETE = ["ALERT001", "ALERT004"]

# Exact wording a NOT_REQUIRED alert renders, unchanged from main.
NOT_REQUIRED_HR = ("Human review was not required under deterministic routing policy "
                   "POL-REVIEW-ROUTING-V1.")
NOT_REQUIRED_GATE_TITLE = "HUMAN-REVIEW GATE: NOT APPLICABLE"
NOT_REQUIRED_GATE_BODY = "The routing policy authorized the recorded system disposition: monitor."

COMPLETE_DISPOSITION = {
    "evidence_reviewed": True,
    "draft_disposition": "edited",
    "decision_reason": "Session rationale recorded for an already-authorized case.",
    "final_note": "Session note recorded.",
    "final_action": "monitor",
    "reviewer": "session:EMP-003",
}


# ── helpers ──────────────────────────────────────────────────────────────────
def _run(role: str = "Analyst", **state):
    at = AppTest.from_file(APP, default_timeout=120)
    at.session_state["view_as"] = role
    for k, v in state.items():
        at.session_state[k] = v
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def _open(alert_id: str, role: str = "Analyst", dispositions=None):
    state = {"open_case": alert_id}
    if dispositions is not None:
        state["session_dispositions"] = dispositions
    return _run(role, **state)


def _md(at) -> str:
    return " ".join(m.value for m in at.markdown)


def _has_form(at) -> bool:
    return any(b.label == "Record disposition" for b in at.button)


def _rail(md: str) -> dict:
    import re
    return dict(re.findall(
        r'case-summary-label">([A-Z ]+)</div><div class="case-summary-value">([^<]*)</div>', md))


def _pair(label: str, value: str) -> str:
    return f'case-summary-label">{label}</div><div class="case-summary-value">{value}</div>'


def _norm(md: str) -> str:
    """Scrub the per-render volatiles in the app header (session id, wall-clock minute)
    so two renders can be byte-compared without a clock race."""
    import re
    md = re.sub(r"SESSION-\d{8}-\d{6}", "SESSION-X", md)
    return re.sub(r"\d{2}-[A-Za-z]{3}-\d{4} \d{2}:\d{2} UTC", "TIMESTAMP", md)


def _lifecycle() -> dict:
    return {r.alert_id: r for r in load_lifecycle(DATA / "case_lifecycle.csv")}


def _tracked_data_hashes() -> dict:
    rel = subprocess.run(["git", "ls-files", "data"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout.split()
    return {r: hashlib.sha256((ROOT / r).read_bytes()).hexdigest() for r in sorted(rel)}


def _main_render(alert_id: str) -> str:
    """The Case File markdown produced by app.py as committed on main."""
    committed = subprocess.run(["git", "show", "main:app.py"], cwd=ROOT,
                               capture_output=True, check=True).stdout
    tmp = ROOT / "_main_app_snapshot.py"
    tmp.write_bytes(committed)
    try:
        at = AppTest.from_file(str(tmp), default_timeout=120)
        at.session_state["view_as"] = "Analyst"
        at.session_state["open_case"] = alert_id
        at.run()
        assert not at.exception, [str(e.value) for e in at.exception]
        return " ".join(m.value for m in at.markdown)
    finally:
        tmp.unlink(missing_ok=True)


# ── the routing vocabulary this gating relies on ─────────────────────────────
def test_routing_enum_members_are_the_expected_three():
    assert [m.name for m in ReviewRoutingStatus] == ["UNDETERMINED", "REQUIRED", "NOT_REQUIRED"]
    assert ReviewRoutingStatus.REQUIRED.value == "required"
    assert ReviewRoutingStatus.NOT_REQUIRED.value == "not_required"


def test_exactly_two_alerts_are_not_required():
    lc = _lifecycle()
    not_req = sorted(a for a, r in lc.items()
                     if r.review_routing is ReviewRoutingStatus.NOT_REQUIRED)
    assert not_req == NOT_REQUIRED
    assert len(lc) == 39
    assert sum(1 for r in lc.values()
               if r.review_routing is ReviewRoutingStatus.REQUIRED) == 37
    assert sum(1 for r in lc.values()
               if r.review_routing is ReviewRoutingStatus.UNDETERMINED) == 0


def test_render_condition_is_a_conjunction_not_a_replacement():
    """Routing was ADDED to the existing checks, not substituted for them."""
    assert "_review_required = lc.review_routing is ReviewRoutingStatus.REQUIRED" in _SRC
    assert ('_show_disposition_form = (st.session_state.get("view_as") == "Analyst"\n'
            "                              and _review_required\n"
            "                              and not _stored_complete)") in _SRC


# ── NOT_REQUIRED alerts: no form, wording identical to main ──────────────────
@pytest.mark.parametrize("alert_id", NOT_REQUIRED)
def test_not_required_alert_shows_no_form(alert_id):
    at = _open(alert_id)
    assert _lifecycle()[alert_id].review_routing is ReviewRoutingStatus.NOT_REQUIRED
    assert not _has_form(at)
    assert "Record disposition" not in _md(at)


@pytest.mark.parametrize("alert_id", NOT_REQUIRED)
def test_not_required_alert_keeps_its_panel_and_gate_wording(alert_id):
    md = _open(alert_id)
    md = _md(md) if not isinstance(md, str) else md
    assert NOT_REQUIRED_HR in md
    assert NOT_REQUIRED_GATE_TITLE in md
    assert NOT_REQUIRED_GATE_BODY in md
    assert "gate-panel gate-empty" in md


@pytest.mark.parametrize("alert_id", NOT_REQUIRED)
def test_not_required_alert_rail_matches_the_lifecycle(alert_id):
    lc = _lifecycle()[alert_id]
    rail = _rail(_md(_open(alert_id)))
    assert rail["AI DRAFT ACCURACY"] == app.lifecycle_ai_verification_label(lc)
    assert rail["REVIEW REQUIREMENTS"] == app.lifecycle_review_requirements_label(lc) == "NOT REQUIRED"
    assert rail["RECORDED DISPOSITION"] == app.lifecycle_recorded_disposition_label(lc) == "MONITOR"


@pytest.mark.parametrize("alert_id", NOT_REQUIRED)
def test_not_required_alert_renders_exactly_as_on_main(alert_id):
    """The strongest form of 'unchanged': byte-compare against main's own render."""
    assert _norm(_md(_open(alert_id))) == _norm(_main_render(alert_id))


# ── alerts that were already covered stay covered ────────────────────────────
@pytest.mark.parametrize("alert_id", NO_STORED_REVIEW + [STORED_INCOMPLETE])
def test_required_alerts_still_show_the_form(alert_id):
    lc = _lifecycle()[alert_id]
    assert lc.review_routing is ReviewRoutingStatus.REQUIRED
    at = _open(alert_id)
    assert _has_form(at)
    fid = f"disposition_form_{alert_id}"
    controls = []
    for group in (at.checkbox, at.selectbox, at.text_area, at.button):
        controls += [w.label for w in group if getattr(w, "form_id", "") == fid]
    assert set(controls) == {"Evidence reviewed", "AI draft accepted / edited / rejected",
                             "Decision reason", "Final analyst note", "Final action",
                             "Record disposition"}
    assert len(controls) == 6


def test_alert007_gate_still_reads_blocked():
    md = _md(_open(STORED_INCOMPLETE))
    assert "HUMAN-REVIEW GATE: BLOCKED" in md
    assert "reviewer:jdoe" in md
    assert _pair("REVIEW REQUIREMENTS", "BLOCKED") in md


@pytest.mark.parametrize("alert_id", NO_STORED_REVIEW)
def test_unreviewed_required_alerts_keep_pending_wording(alert_id):
    md = _md(_open(alert_id))
    assert "Human review is required and awaiting submission." in md
    assert "HUMAN-REVIEW GATE: PENDING" in md
    assert _pair("REVIEW REQUIREMENTS", "PENDING") in md


@pytest.mark.parametrize("alert_id", NO_STORED_REVIEW + [STORED_INCOMPLETE])
def test_required_alerts_render_exactly_as_before_this_change(alert_id):
    """Unaffected: identical to the branch parent fb331e1."""
    committed = subprocess.run(["git", "show", "fb331e1:app.py"], cwd=ROOT,
                               capture_output=True, check=True).stdout
    tmp = ROOT / "_parent_app_snapshot.py"
    tmp.write_bytes(committed)
    try:
        at = AppTest.from_file(str(tmp), default_timeout=120)
        at.session_state["view_as"] = "Analyst"
        at.session_state["open_case"] = alert_id
        at.run()
        assert not at.exception, [str(e.value) for e in at.exception]
        before = " ".join(m.value for m in at.markdown)
    finally:
        tmp.unlink(missing_ok=True)
    assert _norm(_md(_open(alert_id))) == _norm(before)


# ── stored-complete alerts unchanged ─────────────────────────────────────────
@pytest.mark.parametrize("alert_id, disp", [("ALERT001", "MONITOR"), ("ALERT004", "ESCALATE")])
def test_stored_complete_alerts_still_show_no_form(alert_id, disp):
    at = _open(alert_id)
    assert not _has_form(at)
    md = _md(at)
    assert _pair("REVIEW REQUIREMENTS", "COMPLETE") in md
    assert _pair("RECORDED DISPOSITION", disp) in md
    assert "REVIEW REQUIREMENTS: COMPLETE" in md


# ── role restriction is still in force ───────────────────────────────────────
@pytest.mark.parametrize("alert_id", NO_STORED_REVIEW + [STORED_INCOMPLETE] + NOT_REQUIRED)
def test_manager_never_sees_the_form(alert_id):
    assert not _has_form(_open(alert_id, role="Manager"))


# ── session state on a NOT_REQUIRED alert is never stranded ──────────────────
def test_session_disposition_on_a_not_required_alert_still_reads_correctly():
    """The form is gone, but a disposition already in session state must still drive
    the overlay rather than being silently ignored."""
    alert_id = NOT_REQUIRED[0]
    md = _md(_open(alert_id, dispositions={alert_id: dict(COMPLETE_DISPOSITION)}))
    assert "REVIEW REQUIREMENTS: COMPLETE" in md
    assert _pair("REVIEW REQUIREMENTS", "COMPLETE") in md
    assert _pair("RECORDED DISPOSITION", "MONITOR") in md
    assert app.SESSION_DISPOSITION_NOTE in md
    assert COMPLETE_DISPOSITION["decision_reason"] in md
    assert COMPLETE_DISPOSITION["final_note"] in md
    assert NOT_REQUIRED_GATE_TITLE not in md          # overlay took over the empty state


def test_stranded_session_disposition_still_offers_no_form():
    alert_id = NOT_REQUIRED[0]
    at = _open(alert_id, dispositions={alert_id: dict(COMPLETE_DISPOSITION)})
    assert not _has_form(at)
    assert at.session_state["session_dispositions"][alert_id]["final_action"] == "monitor"


def test_effective_review_overlay_is_unchanged_for_not_required():
    """The overlay helper itself is routing-agnostic — only form VISIBILITY is gated."""
    alert_id = NOT_REQUIRED[0]
    app.st.session_state["session_dispositions"] = {alert_id: dict(COMPLETE_DISPOSITION)}
    merged = app.effective_review(alert_id, None)
    assert not evaluate_review(merged, enforce=True).missing
    app.st.session_state["session_dispositions"] = {}


# ── nothing is persisted ─────────────────────────────────────────────────────
def test_gating_change_writes_nothing(monkeypatch):
    import src.audit as audit_mod
    calls: list = []
    monkeypatch.setattr(audit_mod, "log_event", lambda **kw: calls.append(kw) or {})
    before = _tracked_data_hashes()
    for alert_id in NOT_REQUIRED + NO_STORED_REVIEW + [STORED_INCOMPLETE] + STORED_COMPLETE:
        _open(alert_id)
    assert calls == []
    assert _tracked_data_hashes() == before


def test_lifecycle_routing_values_are_untouched():
    lc = _lifecycle()
    for alert_id in NOT_REQUIRED:
        assert lc[alert_id].review_routing is ReviewRoutingStatus.NOT_REQUIRED
        assert lc[alert_id].review_gate is ReviewGateStatus.NOT_APPLICABLE
        assert lc[alert_id].final_action == "monitor"


def test_src_tree_is_untouched():
    for name in ("review_gate.py", "verifier.py", "pipeline.py", "schema.py",
                 "audit.py", "case_lifecycle.py"):
        committed = subprocess.run(["git", "show", f"main:src/{name}"], cwd=ROOT,
                                   capture_output=True, check=True).stdout
        on_disk = (ROOT / "src" / name).read_bytes()
        assert on_disk.replace(b"\r\n", b"\n") == committed.replace(b"\r\n", b"\n"), name
