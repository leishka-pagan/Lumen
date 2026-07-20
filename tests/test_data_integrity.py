"""Data-integrity sanity tests.

These confirm the synthetic dataset loads, the closed vocabulary holds, the
verifier gate behaves, the audit log appends, and, most importantly, that the
planted hero cases are real: the contradiction in HERO CASE A and the missing
fields in HERO CASE B are actually present in the data, not just commented.

Run:  python -m pytest
(If the CSVs are missing, run python scripts/generate_data.py first.)
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT))  # make `src` importable regardless of how pytest is launched

from src import pipeline, schema  # noqa: E402
from src.audit import log_event  # noqa: E402
from src.verifier import HIGH_RISK_COUNTRIES, KNOWN_CLAIM_TYPES, verify_claim  # noqa: E402

REF_DATE = date(2026, 5, 29)


def load(table: str) -> pd.DataFrame:
    """Load a table, keeping empty cells as empty strings (not NaN)."""
    return pd.read_csv(DATA / f"{table}.csv", keep_default_na=False, dtype=str)


# --------------------------------------------------------------------------
# Basic loading and volumes
# --------------------------------------------------------------------------

def test_all_tables_have_a_csv():
    for table in schema.TABLES:
        assert (DATA / f"{table}.csv").exists(), f"missing CSV for {table}"


def test_volumes_in_target_ranges():
    assert len(load("customers")) == 30
    assert 150 <= len(load("transactions")) <= 300
    assert 30 <= len(load("alerts")) <= 50
    assert len(load("ai_outputs")) == 93        # 39 captured alerts, 1..3 claims each


def test_sample_rows_validate_against_pydantic_models():
    cust = load("customers").iloc[0].to_dict()
    schema.Customer.model_validate(cust)
    txn = load("transactions").iloc[0].to_dict()
    schema.Transaction.model_validate(txn)


# --------------------------------------------------------------------------
# Closed claim-type vocabulary
# --------------------------------------------------------------------------

def test_every_ai_claim_type_is_in_closed_vocabulary():
    types = set(load("ai_outputs")["claim_type"])
    assert types <= KNOWN_CLAIM_TYPES, f"unknown claim types present: {types - KNOWN_CLAIM_TYPES}"


def test_closed_vocabulary_has_exactly_nine_types():
    assert len(KNOWN_CLAIM_TYPES) == 9


# --------------------------------------------------------------------------
# HERO CASE A: the catch. The captured AI asserts exposure the records do not
# support, and the deterministic verifier contradicts it.
#
# In the live-capture dataset the catch lands on ALERT002: the model asserted
# high_risk_country for CUST0002, who has no high-risk counterparty at all.
# --------------------------------------------------------------------------

def test_hero_case_a_contradiction_is_real():
    ai = load("ai_outputs")
    alerts = load("alerts")
    txns = load("transactions")

    claim = ai[(ai.claim_type == "high_risk_country") & (ai.alert_id == "ALERT002")]
    assert len(claim) == 1, "HERO CASE A claim (high_risk_country on ALERT002) not found"
    assert claim.iloc[0].asserted_value == "true"

    customer_id = alerts[alerts.alert_id == "ALERT002"].iloc[0].customer_id
    assert customer_id == "CUST0002"

    # The records contradict the assertion: no transaction leaves for a high-risk country.
    countries = set(txns[txns.customer_id == customer_id].counterparty_country)
    assert not (countries & set(HIGH_RISK_COUNTRIES)), (
        "HERO CASE A is not a contradiction: CUST0002 has a high-risk counterparty"
    )


def test_hero_case_a_contradiction_is_caught_by_the_verifier():
    """The catch must be DERIVED by the verifier, not merely present in the data."""
    ai = load("ai_outputs")
    source = pipeline._load_source_tables()
    row = ai[(ai.claim_type == "high_risk_country") & (ai.alert_id == "ALERT002")].iloc[0].to_dict()
    row["evidence_refs"] = json.loads(row["evidence_refs"])
    result = verify_claim(row, source)
    assert result.status == "FAIL", f"verifier did not catch the contradiction: {result.reason}"


def test_hero_case_a_has_a_true_positive_contrast():
    """The contrast: a prior-SAR assertion the records DO support (CUST0007, count 2)."""
    prior = load("prior_cases")
    ai = load("ai_outputs")
    source = pipeline._load_source_tables()

    sar_count = int(prior[prior.customer_id == "CUST0007"].iloc[0].prior_sar_count)
    assert sar_count == 2, "contrast case CUST0007 should have prior_sar_count = 2"

    contrast = ai[(ai.alert_id == "ALERT007") & (ai.claim_type == "prior_sar_history")]
    assert len(contrast) == 1, "contrast claim (prior_sar_history on ALERT007) not found"
    row = contrast.iloc[0].to_dict()
    assert row["asserted_value"] == "true"
    row["evidence_refs"] = json.loads(row["evidence_refs"])
    assert verify_claim(row, source).status == "PASS"


# --------------------------------------------------------------------------
# HERO CASE B: the rubber stamp. Required review fields are missing.
# --------------------------------------------------------------------------

def test_hero_case_b_review_is_missing_required_fields():
    hr = load("human_reviews")
    rubber = hr[hr.review_id == "REV001"]
    assert len(rubber) == 1, "HERO CASE B review REV001 not found"
    row = rubber.iloc[0]
    assert row.draft_disposition == "accepted"
    assert row.decision_reason == "", "decision_reason should be MISSING for HERO CASE B"
    assert row.final_note == "", "final_note should be MISSING for HERO CASE B"
    assert row.evidence_reviewed.lower() == "false"


def test_hero_case_b_has_a_complete_review_contrast():
    hr = load("human_reviews")
    complete = hr[(hr.decision_reason != "") & (hr.final_note != "")]
    assert len(complete) >= 1, "expected at least one fully completed review as contrast"


# --------------------------------------------------------------------------
# Typology scenarios are backed by real underlying data
# --------------------------------------------------------------------------

def test_expected_activity_mismatch_scenario():
    cust = load("customers")
    txns = load("transactions")
    student = cust[cust.customer_id == "CUST0002"].iloc[0]
    assert float(student.expected_monthly_volume) == 2000.0
    big = txns[(txns.customer_id == "CUST0002") & (txns.amount.astype(float) >= 40000)]
    assert len(big) >= 1, "student should have a large inflow far above expected volume"


def test_rapid_movement_scenario():
    txns = load("transactions")
    t = txns[txns.customer_id == "CUST0003"].copy()
    t["day"] = t.timestamp.str.slice(0, 10)
    same_day = t[t.day == "2026-05-22"]
    assert len(same_day) == 4, "expected 4 same-day transactions for rapid_movement"
    assert same_day.amount.astype(float).nunique() == 1, "rapid_movement txns should share one amount"


def test_structuring_scenario():
    txns = load("transactions")
    t = txns[txns.customer_id == "CUST0004"].copy()
    amounts = t.amount.astype(float)
    under = amounts[(amounts >= 9000) & (amounts < 10000)]
    assert len(under) == 4, "expected 4 sub-10000 deposits for structuring"


def test_high_risk_country_scenario():
    txns = load("transactions")
    hr = txns[(txns.customer_id == "CUST0005") & (txns.counterparty_country == "IR")]
    assert len(hr) >= 1, "dormant business should have a transfer to a high-risk country"


def test_stale_kyc_scenario():
    cust = load("customers")
    kyc = load("kyc_profile_status")
    last = date.fromisoformat(cust[cust.customer_id == "CUST0006"].iloc[0].kyc_last_updated)
    assert (REF_DATE - last).days > 4 * 365, "CUST0006 KYC should be older than 4 years"
    flag = kyc[kyc.customer_id == "CUST0006"].iloc[0].current_within_12mo
    assert flag.lower() == "false", "stale KYC profile should not be current within 12 months"


# --------------------------------------------------------------------------
# Verifier dispatch gate (stub logic, real gate)
# --------------------------------------------------------------------------

def test_verifier_rejects_unknown_claim_type_without_crashing():
    result = verify_claim({"claim_type": "totally_made_up", "evidence_refs": ["x"]})
    assert result.status == "NEEDS_REVIEW"
    assert "Unknown claim_type" in result.reason


def test_verifier_flags_missing_evidence_refs():
    result = verify_claim({"claim_type": "prior_sar_history", "evidence_refs": []})
    assert result.status == "NEEDS_REVIEW"
    assert "evidence_refs" in result.reason


def test_verifier_known_claim_dispatches_to_stub():
    result = verify_claim({"claim_type": "prior_sar_history", "evidence_refs": ["prior_cases.customer_id=CUST0001"]})
    assert result.status == "NEEDS_REVIEW"
    assert result.claim_type == "prior_sar_history"


def test_ai_output_evidence_refs_parse_as_json():
    ai = load("ai_outputs")
    for raw in ai.evidence_refs:
        parsed = json.loads(raw)
        assert isinstance(parsed, list)


# --------------------------------------------------------------------------
# Audit log: append-only writes
# --------------------------------------------------------------------------

def test_audit_log_appends():
    workdir = Path(tempfile.mkdtemp(prefix="aml_audit_test_"))
    try:
        log_path = workdir / "audit_log.csv"
        log_event("tester", "first_action", alert_id="ALERT001", details={"k": 1}, log_path=log_path)
        log_event("tester", "second_action", alert_id="ALERT001", details={"k": 2}, log_path=log_path)

        df = pd.read_csv(log_path)
        assert list(df.columns) == ["log_id", "timestamp", "actor", "action", "alert_id", "details_json"]
        assert len(df) == 2, "second write should append, not overwrite"
        assert df.iloc[0].action == "first_action"
        assert df.iloc[1].action == "second_action"
        assert json.loads(df.iloc[1].details_json) == {"k": 2}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------
# Synthetic customer display names (no generic "Customer NN" placeholders)
# --------------------------------------------------------------------------

PLACEHOLDER_RE = re.compile(r"^Customer\s+\d+$")

# The designed hero/scenario customers. Their display names must never change.
HERO_CUSTOMER_NAMES = {
    "CUST0001": "Dana Whitfield", "CUST0002": "Marcus Reed", "CUST0003": "Priya Nair",
    "CUST0004": "Tomas Herrera", "CUST0005": "Eastgate Trading LLC", "CUST0006": "Helen Voss",
    "CUST0007": "Roland Beck", "CUST0008": "Grace Lin",
}


def test_no_generic_placeholder_customer_names():
    """The customer table must carry real display names, not 'Customer NN' fillers."""
    cust = load("customers")
    placeholders = [(r.customer_id, r["name"]) for _, r in cust.iterrows()
                    if PLACEHOLDER_RE.match(r["name"])]
    assert not placeholders, f"generic placeholder customer names remain: {placeholders}"


def test_customer_display_names_are_unique():
    names = list(load("customers")["name"])
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, f"duplicate customer display names: {dupes}"


def test_designed_hero_customer_names_unchanged():
    cust = load("customers")
    for cid, expected in HERO_CUSTOMER_NAMES.items():
        got = cust[cust.customer_id == cid].iloc[0]["name"]
        assert got == expected, f"{cid} display name changed: {got!r} != {expected!r}"


def test_generator_reproduces_committed_customer_names():
    """The committed customers.csv must match the generator, so regenerating the
    dataset will not silently revert the display names (build_dataset does no I/O)."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import generate_data  # noqa: E402
    gen = generate_data.build_dataset()["customers"].reset_index(drop=True)
    cur = load("customers")
    assert list(gen["customer_id"]) == list(cur["customer_id"]), "customer id order diverged"
    assert list(gen["name"]) == list(cur["name"]), "generator display names diverge from committed CSV"
