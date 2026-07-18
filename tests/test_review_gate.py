"""Tests for the anti-rubber-stamp human-review gate (HERO CASE B).

Covers the pure shared gate (src/review_gate), its integration into the decision
pipeline (src/pipeline.process_alert Step 3), the audit behavior of an actual
pipeline execution, and the guarantee that rendering the Case File writes no audit
event. All pipeline audit writes are redirected to a temp path so no data/*.csv is
touched.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT))

from src.review_gate import ReviewGateResult, evaluate_review  # noqa: E402
from src.pipeline import process_alert  # noqa: E402
from src.verifier import verify_prior_sar_history  # noqa: E402

APP = str(ROOT / "app.py")

COMPLETE = {
    "review_id": "RTEST", "alert_id": "ALERTX", "reviewer": "reviewer:t",
    "evidence_reviewed": "True", "draft_disposition": "edited",
    "decision_reason": "Confirmed evidence; claim contradicted.", "final_note": "Monitor.",
    "final_action": "monitor",
}


def _load(name: str) -> pd.DataFrame:
    return pd.read_csv(DATA / f"{name}.csv", dtype=str, keep_default_na=False)


def _rev(review_id: str) -> dict:
    hr = _load("human_reviews")
    return hr[hr["review_id"] == review_id].iloc[0].to_dict()


# ── the pure gate ──────────────────────────────────────────────────────────

def test_1_complete_review_allowed():
    g = evaluate_review(COMPLETE, enforce=True)
    assert isinstance(g, ReviewGateResult)
    assert g.allowed and not g.blocked and g.missing == ()


def test_2_evidence_reviewed_false_blocked():
    g = evaluate_review({**COMPLETE, "evidence_reviewed": "False"}, enforce=True)
    assert g.blocked and "evidence_reviewed" in g.missing


def test_3_missing_decision_reason_blocked():
    g = evaluate_review({**COMPLETE, "decision_reason": ""}, enforce=True)
    assert g.blocked and "decision_reason" in g.missing


def test_4_missing_final_note_blocked():
    g = evaluate_review({**COMPLETE, "final_note": ""}, enforce=True)
    assert g.blocked and "final_note" in g.missing


def test_5_missing_final_action_blocked():
    g = evaluate_review({**COMPLETE, "final_action": ""}, enforce=True)
    assert g.blocked and "final_action" in g.missing


def test_6_invalid_disposition_blocked():
    g = evaluate_review({**COMPLETE, "draft_disposition": "maybe"}, enforce=True)
    assert g.blocked and "draft_disposition" in g.missing


def test_7_multiple_defects_reported_together():
    g = evaluate_review(_rev("REV001"), enforce=True)  # REV001: 4 fields wrong
    assert g.blocked
    assert set(g.missing) == {"evidence_reviewed", "decision_reason", "final_note", "final_action"}


def test_8_blocked_result_has_no_disposition():
    g = evaluate_review(_rev("REV001"), enforce=True)
    assert g.blocked and g.disposition is None


def test_9_valid_result_preserves_disposition():
    g = evaluate_review(_rev("REV003"), enforce=True)  # complete edited review
    assert g.allowed and g.disposition == "edited"


# ── pipeline integration + audit ───────────────────────────────────────────

def _actions(log_path: Path) -> list[str]:
    df = pd.read_csv(log_path, keep_default_na=False)
    return list(df["action"])


def test_10_and_11_pipeline_invokes_gate_and_blocks_with_one_audit_event(tmp_path):
    log = tmp_path / "audit.csv"
    result = process_alert("ALERT007", human_review=_rev("REV001"),
                           enforce_gate=True, log_path=log)
    gate = result["review_gate"]
    # 10: the pipeline evaluated the review through the shared gate and failed closed
    assert isinstance(gate, ReviewGateResult) and gate.blocked
    assert result["disposition"] is None
    # 11: exactly one structured block event
    assert _actions(log).count("review_blocked") == 1
    assert not (DATA / "audit_log.csv").samefile(log)  # sanity: not the real trail


def test_12_disabled_enforcement_in_pipeline_writes_one_bypass_event(tmp_path):
    log = tmp_path / "audit.csv"
    result = process_alert("ALERT007", human_review=_rev("REV001"),
                           enforce_gate=False, log_path=log)
    gate = result["review_gate"]
    assert not gate.blocked and gate.enforcement_enabled is False
    assert result["disposition"] == "accepted"        # bypass finalizes the stored disposition
    actions = _actions(log)
    assert actions.count("review_bypassed") == 1
    assert actions.count("review_blocked") == 0


def test_13_case_file_render_writes_no_audit(monkeypatch):
    import src.audit as audit_mod
    calls: list[dict] = []
    monkeypatch.setattr(audit_mod, "log_event", lambda **kw: calls.append(kw) or {})
    at = AppTest.from_file(APP, default_timeout=120)
    at.session_state["open_case"] = "ALERT007"   # renders the Case File + BLOCKED gate panel
    at.run()
    assert not at.exception, f"Case File render raised: {[str(e.value) for e in at.exception]}"
    assert calls == [], f"rendering the Case File wrote audit events: {calls}"


# ── independence + regression ──────────────────────────────────────────────

def test_14_alert007_ai_pass_and_gate_blocked_independently():
    source = {n: _load(n) for n in
              ("prior_cases", "alerts", "transactions", "customers",
               "kyc_profile_status", "evidence_items")}
    claim = {"claim_type": "prior_sar_history", "alert_id": "ALERT007", "evidence_refs": ["x"]}
    assert verify_prior_sar_history(claim, source).status == "PASS"      # AI verification: PASS
    gate = evaluate_review(_rev("REV001"), enforce=True)                 # human-review gate: BLOCKED
    assert gate.blocked and gate.disposition is None


def test_15_hero_a_remains_fail():
    source = {n: _load(n) for n in ("prior_cases", "alerts")}
    claim = {"claim_type": "prior_sar_history", "alert_id": "ALERT001", "evidence_refs": ["x"]}
    assert verify_prior_sar_history(claim, source).status == "FAIL"
