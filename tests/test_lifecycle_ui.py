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

import dataclasses  # noqa: E402

import app  # noqa: E402
from src.case_lifecycle import AIDraftSource, derive_queue_status  # noqa: E402
from src.lifecycle_store import load_lifecycle, write_lifecycle  # noqa: E402

APP = str(ROOT / "app.py")

ALL_ALERTS = [f"ALERT{i:03d}" for i in range(1, 40)]
# Every alert is processed from a real captured live draft.
PROCESSED = list(ALL_ALERTS)
HERO = [f"ALERT{i:03d}" for i in range(1, 8)]
REMAINING = [f"ALERT{i:03d}" for i in range(8, 40)]

# alert_id -> (AI VERIFICATION, REVIEW REQUIREMENTS, RECORDED DISPOSITION, Queue label)
SEVEN = {
    "ALERT001": ("PASS",  "COMPLETE",     "MONITOR",  "Awaiting Manager"),
    "ALERT002": ("MIXED", "PENDING",      "NONE",     "Awaiting Manager"),
    "ALERT003": ("PASS",  "NOT REQUIRED", "MONITOR",  "Closed"),
    "ALERT004": ("PASS",  "COMPLETE",     "ESCALATE", "Closed"),
    "ALERT005": ("PASS",  "NOT REQUIRED", "MONITOR",  "Awaiting Manager"),
    "ALERT006": ("PASS",  "PENDING",      "NONE",     "Awaiting Review"),
    "ALERT007": ("PASS",  "BLOCKED",      "NONE",     "Blocked"),
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
    assert expected == {"Awaiting Manager": 3, "Awaiting Review": 33,
                        "Blocked": 1, "Closed": 2}
    assert "Not Processed" not in expected          # nothing decorative remains
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
def test_alert022_is_processed_from_a_live_capture(runtime):
    """Formerly decorative: ALERT022 now carries a real captured draft and a real verdict."""
    md = _md(_run(open_case="ALERT022"))
    assert _pair("AI VERIFICATION", "MIXED") in md
    assert _pair("REVIEW REQUIREMENTS", "PENDING") in md
    assert _pair("RECORDED DISPOSITION", "NONE") in md
    # the retired NOT_PROCESSED empty states must be gone
    assert _pair("AI VERIFICATION", "NOT EVALUATED") not in md
    assert "has not been run through the LUMEN processing workflow" not in md
    assert "HUMAN-REVIEW GATE: NOT EVALUATED" not in md


def test_formerly_decorative_alert_hides_technical_provenance(runtime):
    md = _md(_run(open_case="ALERT022"))
    assert "DRAFT PROVENANCE" not in md
    assert "claude-haiku-4-5-20251001" not in md
    assert "DEMO-CAPTURE-REMAINING-V1" not in md
    assert "SYNTHETIC FIXTURE" not in md
    assert "Draft Claim" in md
    assert "Source Evidence" in md
    assert "Verdict" in md


# ── ALERT002/005 NOT REQUIRED + override panel ───────────────────────────────
# ALERT005 is the NOT_REQUIRED case that still carries a pending override (ALERT002 is
# now MIXED, so routing requires review and it can hold no system disposition).
@pytest.mark.parametrize("alert_id", ["ALERT005"])
def test_not_required_with_pending_override_panel(alert_id, runtime):
    md = _md(_run(open_case=alert_id))
    assert ("Human review was not required under deterministic routing policy "
            "POL-REVIEW-ROUTING-V1.") in md
    assert "HUMAN-REVIEW GATE: NOT APPLICABLE" in md
    assert "The routing policy authorized the recorded system disposition: monitor." in md
    assert "A manager override request is pending above." in md      # pending override note
    assert "OVERRIDE REQUEST" in md                                  # the override panel itself


# ── Draft provenance is truthful ─────────────────────────────────────────────
def test_captured_live_case_hides_technical_provenance(runtime):
    md = _md(_run(open_case="ALERT001"))
    assert "DRAFT PROVENANCE" not in md
    assert "claude-haiku-4-5-20251001" not in md
    assert "LIVE-CAPTURE-V1" not in md
    assert "CAPTURED LIVE" not in md
    assert "SYNTHETIC FIXTURE" not in md
    assert "AI Draft Verification" in md and "AI Claim Verification" not in md
    assert "Draft Claim" in md              # claim label renamed (CSS uppercases to DRAFT CLAIM)
    assert "Source Evidence" in md
    assert "Verdict" in md


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
    # approve ALERT005's disposition override -> lifecycle ALERT005 -> CLOSED
    app.record_override_decision(
        "CHG-SEED-002", "approved", reviewer="M. Chen", rationale="ok",
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
    assert ("All required review fields are present. Review decision recorded: "
            "edited. Final case action: monitor.") in vis
    assert "did not accept the AI draft as-is" not in vis        # old wording gone


def test_alert007_accepted_is_a_review_decision_never_a_disposition(runtime):
    md = _md(_run(open_case="ALERT007"))
    vis = _visible(md)
    assert _pair("RECORDED DISPOSITION", "NONE") in md
    # populated card labels 'accepted' as a review decision
    assert 'review-lbl">Review Decision</span><span class="review-val">accepted</span>' in md
    # BLOCKED gate body preserved verbatim
    assert ("Incomplete review detected. This stored review fails the same enforcement "
            "rule used by the decision pipeline. No final disposition is accepted.") in vis
    # 'accepted' is ONLY ever a review decision — never called a recorded/final disposition
    assert "disposition is 'accepted'" not in vis               # never conflated
    assert _pair("RECORDED DISPOSITION", "ACCEPTED") not in md
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


# ── Alert Inventory DRAFT column (only from lifecycle.ai_draft_source) ────────
FIXTURE_MARK = ">&#9679; FIXTURE</span>"
LIVEAI_MARK = ">&#9679; LIVE AI</span>"
EMDASH_MARK = 'color:#ccc;font-size:12px;">&#8212;</span>'   # the no-draft em dash span


def test_draft_column_header_renamed_from_ai(runtime):
    table = _queue_table(_run())
    assert ">DRAFT</th>" in table
    assert ">AI</th>" not in table


def test_baseline_draft_counts_39_liveai_0_fixture_0_dash(runtime):
    table = _queue_table(_run())
    assert table.count(LIVEAI_MARK) == 39        # every alert has a real captured draft
    assert table.count(FIXTURE_MARK) == 0        # no synthetic fixture remains
    assert table.count(EMDASH_MARK) == 0         # no alert is draft-less


def test_draft_never_renders_standalone_ai_label(runtime):
    table = _queue_table(_run())
    assert "&#9679; AI</span>" not in table      # the old "● AI" marker is gone entirely
    assert table.count(LIVEAI_MARK) == 39


def test_draft_column_matches_lifecycle_source_for_all_39():
    source = app.load_source_tables(app.mtimes_key())
    index = {r.alert_id: r for r in load_lifecycle(DATA / "case_lifecycle.csv")}
    for _, arow in source["alerts"].iterrows():
        row = app.build_queue_row(arow, source, index)
        assert row["draft"] == app.lifecycle_draft_source_label(index[arow["alert_id"]])


def test_ai_outputs_presence_cannot_change_draft():
    # The DRAFT label reads ONLY the lifecycle record. Removing every ai_outputs row for
    # ALERT008 must not change its label away from LIVE AI.
    source = app.load_source_tables(app.mtimes_key())
    index = {r.alert_id: r for r in load_lifecycle(DATA / "case_lifecycle.csv")}
    stripped = dict(source)
    stripped["ai_outputs"] = source["ai_outputs"][source["ai_outputs"]["alert_id"] != "ALERT008"]
    arow = source["alerts"][source["alerts"]["alert_id"] == "ALERT008"].iloc[0]
    assert app.build_queue_row(arow, stripped, index)["draft"] == "LIVE AI"


def test_alerts_status_cannot_change_draft():
    source = app.load_source_tables(app.mtimes_key())
    index = {r.alert_id: r for r in load_lifecycle(DATA / "case_lifecycle.csv")}
    base = source["alerts"][source["alerts"]["alert_id"] == "ALERT001"].iloc[0]
    labels = set()
    for legacy in ("open", "in_review", "closed", "totally-made-up"):
        arow = base.copy()
        arow["status"] = legacy
        labels.add(app.build_queue_row(arow, source, index)["draft"])
    assert labels == {"LIVE AI"}          # lifecycle captured_live wins regardless


def test_synthetic_fixture_without_model_id_displays_fixture(tmp_path, monkeypatch):
    # The inverse mapping still holds: a VALID synthetic_fixture record (no model id)
    # renders FIXTURE while every captured_live record renders LIVE AI.
    lc = tmp_path / "case_lifecycle.csv"
    recs = load_lifecycle(DATA / "case_lifecycle.csv")
    committed_before = (DATA / "case_lifecycle.csv").read_bytes()
    for i, r in enumerate(recs):
        if r.alert_id == "ALERT001":
            recs[i] = dataclasses.replace(
                r, ai_draft_source=AIDraftSource.SYNTHETIC_FIXTURE,
                ai_draft_reference="FIXTURE-BUNDLE-ALERT001", model_id=None)
    write_lifecycle(recs, lc)
    monkeypatch.setenv("LUMEN_CASE_LIFECYCLE_CSV", str(lc))
    table = _queue_table(_run())
    assert table.count(FIXTURE_MARK) == 1         # ALERT001 -> FIXTURE (ai_outputs unchanged)
    assert table.count(LIVEAI_MARK) == 38         # the remaining captured-live drafts
    assert table.count(EMDASH_MARK) == 0
    assert (DATA / "case_lifecycle.csv").read_bytes() == committed_before   # committed untouched


def test_invalid_lifecycle_hides_draft_column_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("LUMEN_CASE_LIFECYCLE_CSV", str(tmp_path / "missing.csv"))
    at = _run()
    assert any("lifecycle" in e.value.lower() for e in at.error)
    assert ">DRAFT</th>" not in _md(at)            # queue (and DRAFT column) never renders


def test_draft_column_render_is_read_only(runtime, monkeypatch):
    committed = _committed_hashes()
    before = {k: runtime[k].read_bytes() for k in runtime}
    import src.audit as audit_mod
    calls: list = []
    monkeypatch.setattr(audit_mod, "log_event", lambda **kw: calls.append(kw) or {})
    table = _queue_table(_run())
    assert table.count(LIVEAI_MARK) == 39
    assert calls == []
    for k in runtime:
        assert runtime[k].read_bytes() == before[k]
    assert _committed_hashes() == committed


# ── "Case Readiness" -> "Evidence Completeness" relabel (percentage unchanged) ─
# Representative percentages (calculation is byte-for-byte unchanged).
EVIDENCE_PCT = {"ALERT004": 100, "ALERT002": 60, "ALERT006": 50, "ALERT007": 93, "ALERT022": 94}


def _evidence_pair(pct: int) -> str:
    return f'field-lbl">Evidence Completeness</span><span class="field-val">{pct}%</span>'


def test_alert_inventory_heading_is_evidence_completeness(runtime):
    table = _queue_table(_run())
    assert ">EVIDENCE COMPLETENESS</th>" in table
    assert "Case Readiness" not in table
    assert ">Case Readiness</th>" not in table


def test_case_file_uses_evidence_completeness_label_and_same_percent(runtime):
    for alert_id, pct in EVIDENCE_PCT.items():
        md = _md(_run(open_case=alert_id))
        assert _evidence_pair(pct) in md, f"{alert_id} expected Evidence Completeness {pct}%"
        assert "Case Readiness" not in md


def test_warning_uses_missing_source_evidence(runtime):
    # ALERT006 has 50% evidence completeness -> a missing-evidence warning is shown.
    md = _md(_run(open_case="ALERT006"))
    assert "Missing source evidence:" in md
    assert "Missing evidence:" not in md            # old prefix retired


def test_all_retired_case_readiness_strings_absent(runtime):
    # Full default render (all tab panels render in AppTest, incl. the Risk Settings
    # section retitled "Evidence Completeness Gates") plus a Case File.
    md_full = _md(_run())
    assert "Case Readiness" not in md_full
    assert "Evidence Completeness Gates" in md_full  # Risk Settings section retitled
    md_case = _md(_run(open_case="ALERT004"))
    assert "Case Readiness" not in md_case
    assert "CASE READINESS" not in md_full and "CASE READINESS" not in md_case


def test_alert022_94pct_evidence_is_independent_of_disposition(runtime):
    md = _md(_run(open_case="ALERT022"))
    assert _evidence_pair(94) in md                              # Evidence Completeness 94%
    assert _pair("AI VERIFICATION", "MIXED") in md
    assert _pair("REVIEW REQUIREMENTS", "PENDING") in md
    assert _pair("RECORDED DISPOSITION", "NONE") in md           # evidence != disposition


def test_evidence_completeness_cannot_change_queue_status():
    # The queue status is lifecycle-derived; changing evidence availability (hence the
    # completeness %) leaves the status untouched.
    source = app.load_source_tables(app.mtimes_key())
    index = {r.alert_id: r for r in load_lifecycle(DATA / "case_lifecycle.csv")}
    arow = source["alerts"][source["alerts"]["alert_id"] == "ALERT003"].iloc[0]
    base = app.build_queue_row(arow, source, index)
    ev = source["evidence_items"].copy()
    ev.loc[ev["alert_id"] == "ALERT003", "available"] = "false"   # force 0% completeness
    faked = dict(source); faked["evidence_items"] = ev
    changed = app.build_queue_row(arow, faked, index)
    assert changed["readiness"] != base["readiness"]             # completeness really changed
    assert changed["status"] == base["status"]                   # queue status unchanged


def test_evidence_completeness_render_is_read_only(runtime, monkeypatch):
    committed = _committed_hashes()
    before = {k: runtime[k].read_bytes() for k in runtime}
    import src.audit as audit_mod
    calls: list = []
    monkeypatch.setattr(audit_mod, "log_event", lambda **kw: calls.append(kw) or {})
    _queue_table(_run())
    for alert_id in ("ALERT004", "ALERT006", "ALERT022"):
        at = _run(open_case=alert_id)
        assert not at.exception, [str(e.value) for e in at.exception]
    assert calls == []
    for k in runtime:
        assert runtime[k].read_bytes() == before[k]
    assert _committed_hashes() == committed


# ── Display-only override request identifiers ────────────────────────────────
# OVR-001/002/003 are shown; the lifecycle record and every widget key keep the
# internal CHG-SEED-* id. Reset Demo continues to key off the internal id.
def _override_requests(**state):
    return _run(view_as="Manager", manager_review_view="override_requests", **state)


def test_lifecycle_records_still_carry_internal_override_ids(runtime):
    index = {r.alert_id: r for r in load_lifecycle(runtime["lifecycle"])}
    for alert_id, internal in (("ALERT001", "CHG-SEED-003"),
                               ("ALERT002", "CHG-SEED-001"),
                               ("ALERT005", "CHG-SEED-002")):
        assert index[alert_id].override_request_id == internal
        assert app.override_display_id(internal) != internal      # it IS remapped for display


def test_override_request_surfaces_show_display_ids(runtime):
    at = _override_requests()
    cards = " ".join(m.value for m in at.markdown if "ov-card-id" in m.value)
    history = " ".join(m.value for m in at.markdown if "<th>Request ID</th>" in m.value)
    for internal, shown in (("CHG-SEED-001", "OVR-001"),
                            ("CHG-SEED-002", "OVR-002"),
                            ("CHG-SEED-003", "OVR-003")):
        assert shown in cards and internal not in cards
        assert shown in history and internal not in history


def test_display_mapping_does_not_touch_widget_keys_or_lifecycle(runtime):
    at = _override_requests()
    keys = {b.key for b in at.button if b.key}
    assert {"apr_CHG-SEED-001", "rej_CHG-SEED-001", "oc_CHG-SEED-001"} <= keys
    assert not any("OVR-" in k for k in keys)
    # the runtime lifecycle CSV is untouched by rendering
    assert "OVR-" not in runtime["lifecycle"].read_text(encoding="utf-8")


def test_open_case_file_button_still_routes_by_internal_id(runtime):
    at = _override_requests()
    _btn = next(b for b in at.button if b.key == "oc_CHG-SEED-003")
    _btn.click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert "Dana Whitfield —" in _md(at)        # CHG-SEED-003 -> ALERT001, unchanged routing


def test_display_mapping_mutates_no_committed_file(runtime):
    committed = _committed_hashes()
    _override_requests()
    _run(open_case="ALERT002")
    assert _committed_hashes() == committed


# ── BaseWeb dropdown styling (portal selectors) ──────────────────────────────
# These assert the CSS SOURCE in app.py: the dead menu selectors are gone, only
# stable portal selectors are used, and every required state value is present.
import re as _re

_APP_SRC = (ROOT / "app.py").read_text(encoding="utf-8")


def _dropdown_css():
    """The dropdown-related CSS lines only (control + popover menu), excluding
    comments, so selector/emotion assertions never trip on prose."""
    lines = []
    for ln in _APP_SRC.splitlines():
        s = ln.strip()
        if s.startswith("/*") or s.startswith("*") or not s:
            continue
        if ('data-baseweb="popover"' in ln or 'data-baseweb="select"' in ln
                or 'svg[data-baseweb="icon"]' in ln
                or 'input[role="combobox"]' in ln
                or 'li[role="option"]' in ln):
            lines.append(ln)
    return "\n".join(lines)


def test_dropdown_css_uses_no_generated_emotion_classes():
    css = _dropdown_css()
    assert css, "no dropdown CSS found"
    assert "st-emotion-cache" not in css
    assert not _re.search(r"\.st-[a-z0-9]{2,3}\b", css)     # no st-c2 / st-bb / st-dp etc.


def test_dead_dropdown_selectors_removed_from_active_rules():
    # No ACTIVE rule (a line ending in a `{...}` declaration) may use the dead selectors.
    for ln in _APP_SRC.splitlines():
        if "{" in ln and "}" in ln and not ln.strip().startswith(("/*", "*")):
            assert 'ul[data-baseweb="menu"]' not in ln, ln
            assert 'ul[role="listbox"]' not in ln, ln
            assert '[role="listbox"]' not in ln, ln


def test_stable_popover_and_option_selectors_present():
    for sel in ('div[data-baseweb="popover"]',
                'div[data-baseweb="popover"] ul',
                'div[data-baseweb="popover"] li[role="option"]',
                'div[data-baseweb="popover"] li[role="option"]>div',
                'li[role="option"][aria-selected="true"]',
                'li[role="option"][aria-disabled="true"]',
                'div[data-testid="stSelectbox"] div[data-baseweb="select"]>div',
                'div[data-testid="stSelectbox"] input[role="combobox"]',
                'div[data-testid="stSelectbox"] svg[data-baseweb="icon"]'):
        assert sel in _APP_SRC, f"missing stable selector: {sel}"


def test_collapsed_control_state_values():
    css = _APP_SRC
    # normal control box
    assert _re.search(r'div\[data-testid="stSelectbox"\] div\[data-baseweb="select"\]>div\{[^}]*'
                      r'background:#FFFFFF[^}]*border:1px solid #98A2B3[^}]*border-radius:8px[^}]*'
                      r'box-shadow:none[^}]*color:#17202A', css)
    # placeholder + arrow
    assert _re.search(r'input\[role="combobox"\]::placeholder\{[^}]*color:#667085', css)
    assert _re.search(r'svg\[data-baseweb="icon"\]\{fill:#475569', css)
    # hover + focus ring (no layout change: box-shadow, outline none)
    assert _re.search(r'div\[data-baseweb="select"\]:hover>div\{[^}]*border-color:#66788A[^}]*cursor:pointer', css)
    assert _re.search(r'div\[data-baseweb="select"\]:focus-within>div\{[^}]*border:1px solid #475569[^}]*'
                      r'box-shadow:0 0 0 3px #DCE3EA[^}]*outline:none', css)


def test_popover_state_values():
    css = _APP_SRC
    assert _re.search(r'div\[data-baseweb="popover"\]\{[^}]*background:#ffffff[^}]*'
                      r'border:1px solid #98A2B3[^}]*border-radius:8px[^}]*'
                      r'box-shadow:0 8px 24px rgba\(17,24,39,\.18\)[^}]*padding:0', css)


def test_option_state_values():
    css = _APP_SRC
    # normal
    assert _re.search(r'li\[role="option"\]>div\{[^}]*background:transparent[^}]*color:#17202A[^}]*'
                      r'font-size:13px[^}]*font-weight:400[^}]*padding-left:12px[^}]*'
                      r'padding-right:12px[^}]*border-radius:5px', css)
    # hover
    assert _re.search(r'li\[role="option"\]:hover>div\{[^}]*background:#EEF1F4[^}]*color:#334155[^}]*font-weight:600', css)
    # keyboard focus + inset ring
    assert _re.search(r'li\[role="option"\]:focus-visible>div\{[^}]*background:#EEF1F4[^}]*color:#334155[^}]*'
                      r'font-weight:600[^}]*box-shadow:inset 0 0 0 2px #475569', css)
    # selected + selected-on-hover
    assert _re.search(r'li\[role="option"\]\[aria-selected="true"\]>div\{[^}]*background:#CBD5E1[^}]*color:#334155[^}]*font-weight:700', css)
    assert _re.search(r'li\[role="option"\]\[aria-selected="true"\]:hover>div\{[^}]*background:#A8B6C8[^}]*font-weight:700', css)
    # disabled
    assert _re.search(r'li\[role="option"\]\[aria-disabled="true"\]\{[^}]*cursor:not-allowed[^}]*opacity:\.75', css)
    assert _re.search(r'li\[role="option"\]\[aria-disabled="true"\]>div\{[^}]*background:#F4F6F8[^}]*color:#667085', css)


def test_mobile_popover_constraint_present_and_transform_untouched():
    css = _APP_SRC
    assert _re.search(r'@media \(max-width:600px\)\{[^@]*'
                      r'div\[data-baseweb="popover"\]\{max-width:calc\(100vw - 32px\)', css)
    # never override transform / left / top on the POPOVER CONTAINER rule itself
    # (that carries the dynamic vertical placement); descendant rules are exempt.
    for m in _re.finditer(r'div\[data-baseweb="popover"\]\{([^}]*)\}', css):
        body = m.group(1)
        assert "transform:" not in body, body
        assert not _re.search(r'(?<![\w-])left:', body), body     # not padding-left/margin-left
        assert not _re.search(r'(?<![\w-])top:', body), body


def test_option_height_and_position_not_overridden():
    """Virtualization must be preserved: no height/position/top/width on the option li."""
    css = _APP_SRC
    for ln in css.splitlines():
        m = _re.match(r'\s*div\[data-baseweb="popover"\] li\[role="option"\](?:\[[^\]]+\])?\{(.*)\}', ln)
        if m:                                        # a rule on the li itself (not >div)
            body = m.group(1)
            assert "height:" not in body, ln
            assert "position:" not in body, ln
            assert _re.search(r'\btop:', body) is None, ln
