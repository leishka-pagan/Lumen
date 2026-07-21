"""The Case File summary rail follows the session disposition, so the rail and the
gate panel are two views of ONE evaluation and can never disagree on screen.

Three invariants:

  * default path — with no session disposition the rail is byte-identical to the
    lifecycle-only render committed on main;
  * agreement — with a session disposition the rail's REVIEW REQUIREMENTS and
    RECORDED DISPOSITION are derived from the same ``evaluate_review`` result the
    gate panel displays, via the same ``effective_review`` overlay;
  * AI DRAFT ACCURACY is never touched — a human disposition says nothing about
    whether the AI's claims matched the evidence.

Nothing is persisted: no CSV write, no audit event, no lifecycle mutation.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
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

BLOCKED_ALERT = "ALERT007"
COMPLETE_ALERTS = ["ALERT001", "ALERT004"]

COMPLETE_DISPOSITION = {
    "evidence_reviewed": True,
    "draft_disposition": "edited",
    "decision_reason": "Reviewed the drafted claims against source transactions.",
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


def _rail_html(md: str) -> str:
    """The rail markup plus anything appended immediately after it."""
    start = md.index('<div class="case-summary-rail">')
    tail = md[start:]
    end = tail.index("RECORDED DISPOSITION")
    end = tail.index("</div>", tail.index("</div>", end + 40) + 6) + 6
    return tail[:end + 120]


def _rail(md: str) -> dict:
    """label -> value for all three rail cards."""
    return dict(re.findall(
        r'case-summary-label">([A-Z ]+)</div><div class="case-summary-value">([^<]*)</div>',
        md))


def _pair(label: str, value: str) -> str:
    return f'case-summary-label">{label}</div><div class="case-summary-value">{value}</div>'


def _committed_hashes() -> dict:
    return {f.name: hashlib.sha256(f.read_bytes()).hexdigest()
            for f in sorted(DATA.rglob("*")) if f.is_file()}


def _lifecycle() -> dict:
    return {r.alert_id: r for r in load_lifecycle(DATA / "case_lifecycle.csv")}


# ── one overlay, not two ─────────────────────────────────────────────────────
def _dialog_body() -> str:
    """Source of show_case_dialog only (the form helper lives above it)."""
    start = _SRC.index("def show_case_dialog(")
    return _SRC[start:]


def test_rail_uses_the_same_effective_review_overlay():
    """Inside the Case File the overlay is resolved once and reused by both surfaces."""
    assert _SRC.count("def effective_review") == 1
    body = _dialog_body()
    assert body.count("effective_review(") == 1, "overlay resolved more than once"
    assert body.count("get_session_disposition(") == 1, "second session lookup path"
    # the gate panel consumes the hoisted value rather than recomputing it
    assert "rv = _eff_review" in body


def test_rail_and_gate_share_one_note_string():
    assert _SRC.count("SESSION_DISPOSITION_NOTE =") == 1
    assert _SRC.count("{SESSION_DISPOSITION_NOTE}") == 2      # gate foot + rail note
    assert app.SESSION_DISPOSITION_NOTE == (
        "Session-only demo disposition — not written to human_reviews.csv")


def test_src_tree_is_untouched():
    for name in ("review_gate.py", "verifier.py", "pipeline.py", "schema.py", "audit.py"):
        committed = subprocess.run(["git", "show", f"main:src/{name}"],
                                   cwd=ROOT, capture_output=True, check=True).stdout
        on_disk = (ROOT / "src" / name).read_bytes()
        assert on_disk.replace(b"\r\n", b"\n") == committed.replace(b"\r\n", b"\n"), name


# ── default path: byte-identical to main ─────────────────────────────────────
@pytest.mark.parametrize("alert_id", [BLOCKED_ALERT] + COMPLETE_ALERTS + ["ALERT002", "ALERT033"])
def test_rail_without_session_disposition_is_unchanged(alert_id):
    lc = _lifecycle()[alert_id]
    md = _case(alert_id)
    rail = _rail(md)
    assert rail["AI DRAFT ACCURACY"] == app.lifecycle_ai_verification_label(lc)
    assert rail["REVIEW REQUIREMENTS"] == app.lifecycle_review_requirements_label(lc)
    assert rail["RECORDED DISPOSITION"] == app.lifecycle_recorded_disposition_label(lc)
    assert app.SESSION_DISPOSITION_NOTE not in md
    assert "rail-session-note" not in md


@pytest.mark.parametrize("alert_id", [BLOCKED_ALERT] + COMPLETE_ALERTS)
def test_rail_markup_is_byte_identical_with_an_empty_store(alert_id):
    """An empty session_dispositions dict must change nothing at all."""
    assert _rail_html(_case(alert_id, {})) == _rail_html(_case(alert_id))


def test_a_disposition_on_another_alert_does_not_touch_this_rail():
    store = {BLOCKED_ALERT: dict(COMPLETE_DISPOSITION)}
    for other in COMPLETE_ALERTS:
        assert _rail_html(_case(other, store)) == _rail_html(_case(other))


# ── overlaid path: rail agrees with the gate ─────────────────────────────────
def test_complete_disposition_flips_both_rail_and_gate():
    md = _case(BLOCKED_ALERT, {BLOCKED_ALERT: dict(COMPLETE_DISPOSITION)})
    rail = _rail(md)
    assert rail["REVIEW REQUIREMENTS"] == "COMPLETE"
    assert rail["RECORDED DISPOSITION"] == "MONITOR"
    assert "REVIEW REQUIREMENTS: COMPLETE" in md          # gate panel
    assert "HUMAN-REVIEW GATE: BLOCKED" not in md
    assert "Final case action: <b>monitor</b>" in md


def test_rail_is_marked_session_only_when_overlaid():
    md = _case(BLOCKED_ALERT, {BLOCKED_ALERT: dict(COMPLETE_DISPOSITION)})
    assert "rail-session-note" in md
    assert md.count(app.SESSION_DISPOSITION_NOTE) == 2    # rail note + gate foot


@pytest.mark.parametrize("drop", list(app.SESSION_DISPOSITION_FIELDS))
def test_incomplete_disposition_keeps_rail_blocked_and_agrees_with_gate(drop):
    partial = dict(COMPLETE_DISPOSITION)
    partial[drop] = False if drop == "evidence_reviewed" else ""
    md = _case(BLOCKED_ALERT, {BLOCKED_ALERT: partial})
    rail = _rail(md)
    assert rail["REVIEW REQUIREMENTS"] == "BLOCKED"
    assert rail["RECORDED DISPOSITION"] == "NONE"         # no action on an open review
    assert "HUMAN-REVIEW GATE: BLOCKED" in md             # gate agrees
    assert "REVIEW REQUIREMENTS: COMPLETE" not in md


@pytest.mark.parametrize("action, expected", [
    ("monitor", "MONITOR"), ("escalate", "ESCALATE"), ("close", "CLOSE"),
])
def test_every_final_action_reaches_the_rail(action, expected):
    disp = dict(COMPLETE_DISPOSITION, final_action=action)
    md = _case(BLOCKED_ALERT, {BLOCKED_ALERT: disp})
    assert _rail(md)["RECORDED DISPOSITION"] == expected
    assert f"Final case action: <b>{action}</b>" in md    # same value on the gate


def test_rail_never_shows_a_disposition_the_gate_withholds():
    """Fail closed: an incomplete review must not surface a final action anywhere."""
    partial = dict(COMPLETE_DISPOSITION, decision_reason="")
    md = _case(BLOCKED_ALERT, {BLOCKED_ALERT: partial})
    assert _pair("RECORDED DISPOSITION", "MONITOR") not in md
    assert _pair("RECORDED DISPOSITION", "NONE") in md


# ── the rail labels come from the shared rule ────────────────────────────────
def test_session_rail_labels_track_evaluate_review():
    complete = evaluate_review(COMPLETE_DISPOSITION, enforce=True)
    assert app.session_rail_labels(COMPLETE_DISPOSITION, complete) == ("COMPLETE", "MONITOR")
    partial = dict(COMPLETE_DISPOSITION, final_note="")
    assert app.session_rail_labels(partial, evaluate_review(partial, enforce=True)) \
        == ("BLOCKED", "NONE")


def test_incomplete_stays_blocked_on_the_rail_even_with_enforcement_off():
    """Switching enforcement off stops the gate blocking; it does not make an
    incomplete review complete, and the rail must not claim it is."""
    partial = dict(COMPLETE_DISPOSITION, final_note="")
    unenforced = evaluate_review(partial, enforce=False)
    assert not unenforced.blocked and unenforced.missing        # the rule's own view
    assert app.session_rail_labels(partial, unenforced) == ("BLOCKED", "NONE")


# ── AI DRAFT ACCURACY is out of scope for a human disposition ────────────────
@pytest.mark.parametrize("disp", [
    dict(COMPLETE_DISPOSITION),
    dict(COMPLETE_DISPOSITION, draft_disposition="rejected"),
    dict(COMPLETE_DISPOSITION, final_action="close"),
    dict(COMPLETE_DISPOSITION, evidence_reviewed=False),
])
def test_ai_draft_accuracy_is_never_altered_by_a_disposition(disp):
    lc = _lifecycle()[BLOCKED_ALERT]
    expected = app.lifecycle_ai_verification_label(lc)
    assert expected == "VERIFIED"
    md = _case(BLOCKED_ALERT, {BLOCKED_ALERT: disp})
    assert _rail(md)["AI DRAFT ACCURACY"] == expected
    assert _pair("AI DRAFT ACCURACY", "VERIFIED") in md


def test_claim_verdicts_are_untouched_by_a_disposition():
    md = _case(BLOCKED_ALERT, {BLOCKED_ALERT: dict(COMPLETE_DISPOSITION)})
    assert re.findall(r'hero-verdict">([A-Z ]+)</span>', md) == \
        ["SUPPORTED", "SUPPORTED", "SUPPORTED"]


# ── already-COMPLETE cases keep their lifecycle rail ─────────────────────────
@pytest.mark.parametrize("alert_id, req, disp", [
    ("ALERT001", "COMPLETE", "MONITOR"),
    ("ALERT004", "COMPLETE", "ESCALATE"),
])
def test_complete_cases_rails_match_main(alert_id, req, disp):
    md = _case(alert_id)
    assert _pair("REVIEW REQUIREMENTS", req) in md
    assert _pair("RECORDED DISPOSITION", disp) in md
    assert "rail-session-note" not in md


# ── nothing is persisted ─────────────────────────────────────────────────────
def test_rail_overlay_writes_no_file_and_no_audit(monkeypatch):
    import src.audit as audit_mod
    calls: list = []
    monkeypatch.setattr(audit_mod, "log_event", lambda **kw: calls.append(kw) or {})
    before = _committed_hashes()
    md = _case(BLOCKED_ALERT, {BLOCKED_ALERT: dict(COMPLETE_DISPOSITION)})
    assert _rail(md)["RECORDED DISPOSITION"] == "MONITOR"      # it really rendered
    assert calls == [], f"the rail overlay wrote audit events: {calls}"
    assert _committed_hashes() == before, "a committed data file changed"


def test_lifecycle_still_reads_blocked_behind_the_overlay():
    _case(BLOCKED_ALERT, {BLOCKED_ALERT: dict(COMPLETE_DISPOSITION)})
    lc = _lifecycle()[BLOCKED_ALERT]
    assert lc.review_gate is ReviewGateStatus.BLOCKED
    assert lc.final_action is None
