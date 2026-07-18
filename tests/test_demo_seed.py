"""Tests for the Week 5 demo seed data (pending overrides, audit history,
richer Hero Case A). All seeding flows through scripts/generate_data.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT))

from src.verifier import verify_prior_sar_history  # noqa: E402

# The action vocabulary the live app actually emits (src/audit.py callers in
# src/llm_drafter.py, src/pipeline.py, app.py). The seeded audit trail must be a
# subset: no phantom action names.
LIVE_AUDIT_ACTIONS = {
    "draft_started", "draft_completed", "claim_dropped", "draft_failed",
    "alert_processing_started", "alert_processing_finished", "claim_verified",
    "field_override", "override_review", "keyword_added", "keyword_removed",
    "risk_settings_saved", "draft_empty",
}

EXPECTED_OVERRIDE_COLUMNS = [
    "change_id", "alert_id", "field_changed", "old_value", "new_value",
    "changed_by_id", "changed_by_name", "changed_at", "reason",
    "status", "reviewed_by", "reviewed_at", "review_note",
]


def load(table: str) -> pd.DataFrame:
    return pd.read_csv(DATA / f"{table}.csv", keep_default_na=False, dtype=str)


# ---------------------------------------------------------------- Step 1

def test_pending_overrides_generated():
    assert (DATA / "pending_overrides.csv").exists()


def test_pending_overrides_columns_exact():
    df = load("pending_overrides")
    assert list(df.columns) == EXPECTED_OVERRIDE_COLUMNS


def test_pending_overrides_has_a_pending_row():
    df = load("pending_overrides")
    assert (df["status"] == "pending").sum() >= 1


def test_pending_overrides_alert_ids_exist():
    overrides = load("pending_overrides")
    valid_alerts = set(load("alerts")["alert_id"])
    for aid in overrides["alert_id"]:
        assert aid in valid_alerts, f"override references unknown alert {aid}"


def test_pending_rows_have_blank_reviewer_fields():
    df = load("pending_overrides")
    pending = df[df["status"] == "pending"]
    assert (pending["reviewed_by"] == "").all()
    assert (pending["reviewed_at"] == "").all()


# ---------------------------------------------------------------- Step 2

def test_audit_log_is_not_empty():
    df = load("audit_log")
    assert len(df) >= 1, "audit_log.csv should contain seeded history"


def test_audit_actions_use_only_live_vocabulary():
    df = load("audit_log")
    seeded = set(df["action"])
    phantom = seeded - LIVE_AUDIT_ACTIONS
    assert not phantom, f"seeded audit uses action names the app never emits: {phantom}"


def test_audit_log_columns_match_schema():
    df = load("audit_log")
    assert list(df.columns) == ["log_id", "timestamp", "actor", "action", "alert_id", "details_json"]


# ---------------------------------------------------------------- Step 3

def test_alert001_exists_and_maps_to_cust0001():
    alerts = load("alerts")
    row = alerts[alerts["alert_id"] == "ALERT001"]
    assert len(row) == 1
    assert row.iloc[0]["customer_id"] == "CUST0001"


def test_cust0001_prior_sar_count_still_zero():
    prior = load("prior_cases")
    row = prior[prior["customer_id"] == "CUST0001"]
    assert int(row.iloc[0]["prior_sar_count"]) == 0


def test_alert001_has_multiple_supporting_rows():
    txns = load("transactions")
    evidence = load("evidence_items")
    assert len(txns[txns["customer_id"] == "CUST0001"]) >= 2, "CUST0001 should have multiple transactions"
    assert len(evidence[evidence["alert_id"] == "ALERT001"]) >= 2, "ALERT001 should have multiple evidence items"


def test_hero_case_a_still_verifies_as_fail():
    source = {name: load(name) for name in
              ["customers", "transactions", "prior_cases", "kyc_profile_status", "evidence_items", "alerts"]}
    claim = {"claim_type": "prior_sar_history", "alert_id": "ALERT001", "evidence_refs": ["x"]}
    result = verify_prior_sar_history(claim, source)
    assert result.status == "FAIL", result.reason
