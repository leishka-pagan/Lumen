"""Lifecycle-driven Alert Queue + Case File — the visible architecture integration.

Proves the running UI derives queue status and every Case File rail/state from the
canonical case_lifecycle.csv (via src.lifecycle_store.load_lifecycle +
src.case_lifecycle.derive_queue_status), never from alerts.status; that it fails
closed on bad lifecycle data; that opening a Case File mutates nothing; and that an
approval and Reset Demo are reflected after a rerun.

Isolation: interaction tests redirect the lifecycle/override/audit (and ai_outputs)
runtime CSVs to temp files via env vars. Committed runtime CSVs are never mutated.
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
BASELINE = DATA / "demo_baseline"
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
from src.case_lifecycle import derive_queue_status  # noqa: E402
from src.lifecycle_store import load_lifecycle  # noqa: E402

APP = str(ROOT / "app.py")

ALL_ALERTS = [f"ALERT{i:03d}" for i in range(1, 40)]
PROCESSED = [f"ALERT{i:03d}" for i in range(1, 8)]

# alert_id -> (AI VERIFICATION, REVIEW REQUIREMENTS, RECORDED DISPOSITION, Queue label)
SEVEN = {
    "ALERT001": ("FAIL",  "COMPLETE",     "MONITOR",  "Awaiting Manager"),
    "ALERT002": ("PASS",  "NOT REQUIRED", "MONITOR",  "Awaiting Manager"),
    "ALERT003": ("PASS",  "NOT REQUIRED", "MONITOR",  "Closed"),
    "ALERT004": ("PASS",  "COMPLETE",     "ESCALATE", "Closed"),
    "ALERT005": ("PASS",  "NOT REQUIRED", "MONITOR",  "Awaiting Manager"),
    "ALERT006": ("PASS",  "PENDING",      "NONE",     "Awaiting Review"),
    "ALERT007": ("MIXED", "BLOCKED",      "NONE",     "Blocked"),
}


# ── helpers ──────────────────────────────────────────────────────────────────
def _run(**state):
    at = AppTest.from_file(APP, default_timeout=120)
    for k, v in state.items():
        at.session_state[k] = v
    at.run()
    return at


def _md(at) -> str:
    return " ".join(m.value for m in at.markdown)


def _queue_table(at) -> str:
    return next(m.value for m in at.markdown if "lv-table" in m.value)


def _badge_count(table: str, label: str) -> int:
    return table.count(f">{label}</span>")


def _pair(label: str, value: str) -> str:
    return f'case-summary-label">{label}</div><div class="case-summary-value">{value}</div>'


def _committed_hashes() -> dict:
    import hashlib
    files = ["case_lifecycle.csv", "pending_overrides.csv", "audit_log.csv", "ai_outputs.csv"]
    return {n: hashlib.sha256((DATA / n).read_bytes()).hexdigest() for n in files}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    """All four runtime CSVs redirected to temp copies of the committed files."""
    paths = {}
    for name, key in (("case_lifecycle.csv", "lifecycle"), ("pending_overrides.csv", "overrides"),
                      ("audit_log.csv", "audit"), ("ai_outputs.csv", "ai_outputs")):
        dst = tmp_path / name
        shutil.copy(DATA / name, dst)
        paths[key] = dst
    monkeypatch.setenv("LUMEN_CASE_LIFECYCLE_CSV", str(paths["lifecycle"]))
    monkeypatch.setenv("LUMEN_OVERRIDES_CSV", str(paths["overrides"]))
    monkeypatch.setenv("LUMEN_AUDIT_LOG", str(paths["audit"]))
    monkeypatch.setenv("LUMEN_AI_OUTPUTS_CSV", str(paths["ai_outputs"]))
    return paths


# ── Queue status is derived from the lifecycle, never alerts.status ──────────
def test_build_queue_row_status_comes_from_lifecycle_for_all_39():
    source = app.load_source_tables(app.mtimes_key())
    index = {r.alert_id: r for r in load_lifecycle(DATA / "case_lifecycle.csv")}
    for _, arow in source["alerts"].iterrows():
        aid = arow["alert_id"]
        row = app.build_queue_row(arow, source, index)
        assert row["status"] == app.QUEUE_STATUS_LABELS[derive_queue_status(index[aid])]


def test_alerts_status_cannot_affect_displayed_queue_status():
    # Same lifecycle record, different (legacy) alert_row["status"] values -> identical
    # displayed status. alerts.status is inert.
    source = app.load_source_tables(app.mtimes_key())
    index = {r.alert_id: r for r in load_lifecycle(DATA / "case_lifecycle.csv")}
    base = source["alerts"][source["alerts"]["alert_id"] == "ALERT003"].iloc[0]
    labels = set()
    for legacy in ("open", "in_review", "closed", "totally-made-up"):
        arow = base.copy()
        arow["status"] = legacy
        labels.add(app.build_queue_row(arow, source, index)["status"])
    assert labels == {"Closed"}          # lifecycle CLOSED wins regardless of alerts.status


def test_queue_renamed_and_status_counts_from_lifecycle(runtime):
    at = _run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert 'Alert Inventory <span class="section-count">(39)</span>' in _md(at)
    assert "Active Alerts" not in _md(at)
    table = _queue_table(at)
    index = {r.alert_id: r for r in load_lifecycle(runtime["lifecycle"])}
    expected = {}
    for r in index.values():
        lbl = app.QUEUE_STATUS_LABELS[derive_queue_status(r)]
        expected[lbl] = expected.get(lbl, 0) + 1
    assert expected == {"Not Processed": 32, "Awaiting Manager": 3,
                        "Awaiting Review": 1, "Blocked": 1, "Closed": 2}
    for lbl, n in expected.items():
        assert _badge_count(table, lbl) == n, f"{lbl}: rendered != lifecycle count"
    # legacy status labels never appear as badges
    for legacy in ("Pending Review", "In Progress"):
        assert _badge_count(table, legacy) == 0


# ── The seven canonical cases: rails + queue ─────────────────────────────────
@pytest.mark.parametrize("alert_id", list(SEVEN))
def test_seven_case_rails(alert_id, runtime):
    ai, req, disp, _q = SEVEN[alert_id]
    md = _md(_run(open_case=alert_id))
    assert _pair("AI VERIFICATION", ai) in md
    assert _pair("REVIEW REQUIREMENTS", req) in md
    assert _pair("RECORDED DISPOSITION", disp) in md


def test_seven_case_queue_labels(runtime):
    table = _queue_table(_run())
    for alert_id, (_ai, _req, _disp, q) in SEVEN.items():
        assert _badge_count(table, q) >= 1, f"{alert_id} queue label {q} missing"


def test_alert007_is_blocked_never_closed(runtime):
    md = _md(_run(open_case="ALERT007"))
    assert _pair("REVIEW REQUIREMENTS", "BLOCKED") in md
    assert _pair("RECORDED DISPOSITION", "NONE") in md
    index = {r.alert_id: r for r in load_lifecycle(runtime["lifecycle"])}
    assert app.QUEUE_STATUS_LABELS[derive_queue_status(index["ALERT007"])] == "Blocked"


def test_alert001_004_recorded_disposition_is_action_not_review_word(runtime):
    for alert_id, action in (("ALERT001", "MONITOR"), ("ALERT004", "ESCALATE")):
        md = _md(_run(open_case=alert_id))
        assert _pair("RECORDED DISPOSITION", action) in md
        for word in ("EDITED", "ACCEPTED", "REJECTED"):
            assert _pair("RECORDED DISPOSITION", word) not in md


# ── Ordinary NOT_PROCESSED alert (ALERT022) ──────────────────────────────────
def test_alert022_not_processed_case_file(runtime):
    md = _md(_run(open_case="ALERT022"))
    assert _pair("AI VERIFICATION", "NOT EVALUATED") in md
    assert _pair("REVIEW REQUIREMENTS", "NOT PROCESSED") in md
    assert _pair("RECORDED DISPOSITION", "NONE") in md
    assert ("This synthetic inventory alert has not been run through the LUMEN "
            "processing workflow. No AI draft or verification result exists.") in md
    assert ("Not evaluated. This alert has not been processed or routed for human "
            "review.") in md
    assert "HUMAN-REVIEW GATE: NOT EVALUATED" in md
    assert ("No review gate was evaluated because this alert has not been "
            "processed.") in md


def test_not_processed_shows_no_fixture_provenance(runtime):
    md = _md(_run(open_case="ALERT022"))
    assert "DRAFT PROVENANCE" not in md


# ── ALERT002/005 NOT REQUIRED + override panel ───────────────────────────────
@pytest.mark.parametrize("alert_id", ["ALERT002", "ALERT005"])
def test_not_required_with_pending_override_panel(alert_id, runtime):
    md = _md(_run(open_case=alert_id))
    assert ("Human review was not required under deterministic routing policy "
            "POL-REVIEW-ROUTING-V1.") in md
    assert "HUMAN-REVIEW GATE: NOT APPLICABLE" in md
    assert "The routing policy authorized the recorded system disposition: monitor." in md
    assert "A manager override request is pending above." in md      # pending override note
    assert "OVERRIDE REQUEST" in md                                  # the override panel itself


# ── Draft provenance is truthful ─────────────────────────────────────────────
def test_fixture_provenance_is_truthful(runtime):
    md = _md(_run(open_case="ALERT001"))
    assert ("DRAFT PROVENANCE: SYNTHETIC FIXTURE · RUN FIXTURE-SEED-V1 · "
            "MODEL: NONE") in md
    assert "AI Draft Verification" in md and "AI Claim Verification" not in md
    assert "Draft Claim" in md              # claim label renamed (CSS uppercases to DRAFT CLAIM)


# ── Fail closed on bad/missing lifecycle; never fall back to alerts.status ───
def test_missing_lifecycle_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("LUMEN_CASE_LIFECYCLE_CSV", str(tmp_path / "does_not_exist.csv"))
    at = _run()
    assert any("lifecycle" in e.value.lower() for e in at.error)
    assert "Alert Inventory" not in _md(at)        # queue never renders on failure


def test_duplicate_lifecycle_fails_closed(tmp_path, monkeypatch):
    lc = tmp_path / "case_lifecycle.csv"
    df = pd.read_csv(DATA / "case_lifecycle.csv", dtype=str, keep_default_na=False)
    df.loc[len(df)] = df.iloc[0]                    # duplicate ALERT001
    df.to_csv(lc, index=False)
    monkeypatch.setenv("LUMEN_CASE_LIFECYCLE_CSV", str(lc))
    at = _run()
    assert at.error, "duplicate alert_id must fail closed with an explicit error"
    assert "Alert Inventory" not in _md(at)


def test_lifecycle_missing_an_alert_fails_closed(tmp_path, monkeypatch):
    lc = tmp_path / "case_lifecycle.csv"
    df = pd.read_csv(DATA / "case_lifecycle.csv", dtype=str, keep_default_na=False)
    df = df[df["alert_id"] != "ALERT022"]           # drop one alert's record
    df.to_csv(lc, index=False)
    monkeypatch.setenv("LUMEN_CASE_LIFECYCLE_CSV", str(lc))
    at = _run()
    assert any("ALERT022" in e.value for e in at.error)
    assert "Alert Inventory" not in _md(at)


# ── Opening a Case File mutates nothing ──────────────────────────────────────
def test_opening_case_file_writes_no_lifecycle_csv_or_audit(runtime, monkeypatch):
    committed_before = _committed_hashes()
    before = {k: runtime[k].read_bytes() for k in runtime}
    import src.audit as audit_mod
    calls: list = []
    monkeypatch.setattr(audit_mod, "log_event", lambda **kw: calls.append(kw) or {})
    for alert_id in ("ALERT001", "ALERT002", "ALERT007", "ALERT022"):
        at = _run(open_case=alert_id)
        assert not at.exception, [str(e.value) for e in at.exception]
    assert calls == [], f"Case File render wrote audit events: {calls}"
    for k in runtime:
        assert runtime[k].read_bytes() == before[k], f"temp {k} mutated by opening a case"
    assert _committed_hashes() == committed_before   # committed CSVs untouched


# ── Approval + Reset Demo reflected after a rerun ────────────────────────────
def test_approval_reflected_in_queue_after_rerun(runtime):
    before = _queue_table(_run())
    assert _badge_count(before, "Awaiting Manager") == 3
    assert _badge_count(before, "Closed") == 2
    # approve ALERT002's severity override -> lifecycle ALERT002 -> CLOSED
    app.record_override_decision(
        "CHG-SEED-001", "approved", reviewer="M. Chen", rationale="ok",
        actor="ui:EMP-006", overrides_path=runtime["overrides"],
        log_path=runtime["audit"], lifecycle_path=runtime["lifecycle"])
    after = _queue_table(_run())
    assert _badge_count(after, "Awaiting Manager") == 2      # one fewer pending manager
    assert _badge_count(after, "Closed") == 3                # one more closed


def test_reset_demo_reflected_in_queue_after_rerun(runtime):
    from src.demo_reset import reset_override_demo
    app.record_override_decision(
        "CHG-SEED-002", "approved", reviewer="M. Chen", rationale="ok",
        actor="ui:EMP-006", overrides_path=runtime["overrides"],
        log_path=runtime["audit"], lifecycle_path=runtime["lifecycle"])
    mutated = _queue_table(_run())
    assert _badge_count(mutated, "Awaiting Manager") == 2
    # Reset restores the temp runtime from the committed baselines
    reset_override_demo(runtime["overrides"], runtime["audit"],
                        BASELINE / "pending_overrides.csv", BASELINE / "audit_log.csv",
                        lifecycle_path=runtime["lifecycle"],
                        baseline_lifecycle_path=BASELINE / "case_lifecycle.csv",
                        ai_outputs_path=runtime["ai_outputs"],
                        baseline_ai_outputs_path=BASELINE / "ai_outputs.csv")
    restored = _queue_table(_run())
    assert _badge_count(restored, "Awaiting Manager") == 3   # back to the baseline
    assert _badge_count(restored, "Closed") == 2


# ── Review DECISION vs final case DISPOSITION wording ────────────────────────
def _visible(md: str) -> str:
    """Strip HTML tags to get the visible dialog text (matches browser innerText)."""
    import re
    return re.sub(r"<[^>]+>", "", md)


def test_alert004_review_decision_edited_final_action_escalate(runtime):
    md = _md(_run(open_case="ALERT004"))
    vis = _visible(md)
    assert _pair("RECORDED DISPOSITION", "ESCALATE") in md
    # populated Human Review card: the field is a REVIEW DECISION (value edited)
    assert 'review-lbl">Review Decision</span><span class="review-val">edited</span>' in md
    assert 'review-lbl">Disposition</span>' not in md            # old label gone
    # COMPLETE gate body separates the review decision from the final case action
    assert ("All required review fields are present. Review decision recorded: "
            "edited. Final case action: escalate.") in vis
    assert "Final disposition accepted:" not in vis              # old conflation gone


def test_alert001_warning_distinguishes_edited_decision_from_monitor_action(runtime):
    md = _md(_run(open_case="ALERT001"))
    vis = _visible(md)
    assert _pair("RECORDED DISPOSITION", "MONITOR") in md
    assert ("recorded review decision 'edited' and final case action 'monitor'. "
            "The failed draft claim did not become the final disposition.") in vis
    assert ("All required review fields are present. Review decision recorded: "
            "edited. Final case action: monitor.") in vis
    assert "did not accept the AI draft as-is" not in vis        # old wording gone


def test_alert007_accepted_is_a_review_decision_never_a_disposition(runtime):
    md = _md(_run(open_case="ALERT007"))
    vis = _visible(md)
    assert _pair("RECORDED DISPOSITION", "NONE") in md
    # populated card labels 'accepted' as a review decision
    assert 'review-lbl">Review Decision</span><span class="review-val">accepted</span>' in md
    # blocked-with-failed-claim wording: decision is 'accepted', but no final disposition
    assert ("The stored human-review decision is 'accepted', but the review is "
            "incomplete and no final disposition is recorded.") in vis
    # BLOCKED gate body preserved verbatim
    assert ("Incomplete review detected. This stored review fails the same enforcement "
            "rule used by the decision pipeline. No final disposition is accepted.") in vis
    # 'accepted' is ONLY ever a review decision — never called a recorded/final disposition
    assert "human-review decision is 'accepted'" in vis         # positive: labeled a decision
    assert "disposition is 'accepted'" not in vis               # old quoted conflation gone
    assert "disposition: accepted" not in vis.lower()
    assert "Final disposition accepted:" not in vis
    # the review word must not appear as the RECORDED DISPOSITION rail value
    for word in ("ACCEPTED", "EDITED", "REJECTED"):
        assert _pair("RECORDED DISPOSITION", word) not in md


def test_case_file_wording_change_is_read_only(runtime, monkeypatch):
    committed = _committed_hashes()
    before = {k: runtime[k].read_bytes() for k in runtime}
    import src.audit as audit_mod
    calls: list = []
    monkeypatch.setattr(audit_mod, "log_event", lambda **kw: calls.append(kw) or {})
    for alert_id in ("ALERT001", "ALERT004", "ALERT007"):
        at = _run(open_case=alert_id)
        assert not at.exception, [str(e.value) for e in at.exception]
    assert calls == []
    for k in runtime:
        assert runtime[k].read_bytes() == before[k]
    assert _committed_hashes() == committed
