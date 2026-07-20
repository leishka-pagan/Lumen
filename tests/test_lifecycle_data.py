"""Tests for the persisted canonical lifecycle dataset and its generator.

Proves the committed data/ai_outputs.csv (with truthful provenance) and
data/case_lifecycle.csv are canonical and honest:
  * exact schema; 39 unique lifecycle rows; 7 processed + 32 not_processed;
  * every row reconstructs as a valid CaseLifecycle;
  * the seven derived queue outcomes are exact;
  * verification statuses were DERIVED from the real verifier (not hardcoded);
  * synthetic-fixture provenance never claims a model id;
  * lifecycle draft references match ai_outputs provenance;
  * ALERT001/004 record final_action (not draft_disposition); ALERT007 is blocked
    with no disposition; no CLOSED case lacks a final action + provenance;
  * runtime and demo_baseline copies are byte-identical.

And proves the targeted generator (scripts.generate_data.generate_lifecycle_dataset)
writes only the two artifacts, makes no network/API call, and is deterministic.
"""

from __future__ import annotations

import dataclasses
import filecmp
import os
import socket
import sys
import tempfile
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
BASELINE = DATA / "demo_baseline"
sys.path.insert(0, str(ROOT))

from scripts.generate_data import generate_lifecycle_dataset  # noqa: E402
from src import pipeline, verifier  # noqa: E402
from src.case_lifecycle import (  # noqa: E402
    AIDraftSource, AIVerificationStatus, CaseLifecycle, DispositionSource,
    HumanReviewDecision, OverrideStatus, ProcessingStatus, QueueStatus,
    ReviewGateStatus, ReviewRoutingStatus, derive_queue_status,
)
from src.lifecycle_projector import summarize_ai_verification  # noqa: E402

# Every alert is processed from a real captured live draft. HERO_ALERTS are the seven
# designed demo cases; REMAINING_ALERTS were formerly decorative NOT_PROCESSED rows.
HERO_ALERTS = [f"ALERT{i:03d}" for i in range(1, 8)]
REMAINING_ALERTS = [f"ALERT{i:03d}" for i in range(8, 40)]
PROCESSED_ALERTS = HERO_ALERTS + REMAINING_ALERTS
LIVE_MODEL_ID = "claude-haiku-4-5-20251001"

LIFECYCLE_COLUMNS = [f.name for f in dataclasses.fields(CaseLifecycle)]
AI_OUTPUT_COLUMNS = [
    "output_id", "alert_id", "claim_id", "claim_type", "asserted_value",
    "evidence_refs", "generated_at",
    "draft_source", "draft_reference", "model_id", "processing_run_id",
]
_ENUM_FIELDS = {
    "processing_status": ProcessingStatus, "ai_draft_source": AIDraftSource,
    "ai_verification": AIVerificationStatus, "review_routing": ReviewRoutingStatus,
    "review_gate": ReviewGateStatus, "human_review_decision": HumanReviewDecision,
    "override_status": OverrideStatus, "disposition_source": DispositionSource,
}
# The seven designed outcomes are unchanged by the live-capture promotion; the formerly
# decorative alerts now land in AWAITING_REVIEW (routing REQUIRED, no review on file).
EXPECTED_QUEUES = {
    "ALERT001": QueueStatus.AWAITING_MANAGER, "ALERT002": QueueStatus.AWAITING_MANAGER,
    "ALERT005": QueueStatus.AWAITING_MANAGER, "ALERT003": QueueStatus.CLOSED,
    "ALERT004": QueueStatus.CLOSED, "ALERT006": QueueStatus.AWAITING_REVIEW,
    "ALERT007": QueueStatus.BLOCKED,
    **{a: QueueStatus.AWAITING_REVIEW for a in REMAINING_ALERTS},
}


def _load(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, keep_default_na=False, dtype=str)


def _lifecycle() -> pd.DataFrame:
    return _load(DATA / "case_lifecycle.csv")


def _ai_outputs() -> pd.DataFrame:
    return _load(DATA / "ai_outputs.csv")


def _by_alert(df: pd.DataFrame) -> dict[str, dict]:
    return {r["alert_id"]: r.to_dict() for _, r in df.iterrows()}


def _reconstruct(row: dict) -> CaseLifecycle:
    """Rebuild a CaseLifecycle from a serialized CSV row (empty -> None, enum value
    string -> member). Construction runs __post_init__, so this also validates."""
    kwargs = {}
    for name in LIFECYCLE_COLUMNS:
        value = row[name]
        if name in _ENUM_FIELDS:
            kwargs[name] = _ENUM_FIELDS[name](value)
        else:
            kwargs[name] = value if value != "" else None
    return CaseLifecycle(**kwargs)


# ── Schema & counts ──────────────────────────────────────────────────────────
def test_lifecycle_schema_is_exact_dataclass_order():
    assert list(_lifecycle().columns) == LIFECYCLE_COLUMNS
    assert len(LIFECYCLE_COLUMNS) == 19
    assert "queue_status" not in LIFECYCLE_COLUMNS  # derived, never persisted


def test_ai_outputs_schema_adds_provenance_columns():
    assert list(_ai_outputs().columns) == AI_OUTPUT_COLUMNS


def test_exactly_39_unique_rows():
    lc = _lifecycle()
    assert len(lc) == 39
    assert lc["alert_id"].nunique() == 39
    assert sorted(lc["alert_id"]) == sorted(PROCESSED_ALERTS)


def test_all_39_rows_are_processed():
    lc = _lifecycle()
    processed = set(lc[lc["processing_status"] == "processed"]["alert_id"])
    assert processed == set(PROCESSED_ALERTS)
    assert len(lc) == len(processed)                        # no other status remains
    assert "not_processed" not in set(lc["processing_status"])


# ── Reconstruction & derived queues ──────────────────────────────────────────
def test_every_row_reconstructs_as_valid_lifecycle():
    for _, row in _lifecycle().iterrows():
        rec = _reconstruct(row.to_dict())  # __post_init__ enforces every invariant
        assert isinstance(rec, CaseLifecycle)


def test_seven_derived_queue_outcomes_are_exact():
    for alert_id, row in _by_alert(_lifecycle()).items():
        assert derive_queue_status(_reconstruct(row)) is EXPECTED_QUEUES[alert_id], alert_id


def test_no_row_is_decorative_or_unevaluated():
    """Nothing is left NOT_PROCESSED: every alert carries a real, evaluated draft."""
    for alert_id, row in _by_alert(_lifecycle()).items():
        assert row["processing_status"] == "processed", alert_id
        assert row["ai_draft_source"] == "captured_live", alert_id
        assert row["ai_verification"] in ("pass", "mixed", "fail"), alert_id
        assert row["review_routing"] in ("required", "not_required"), alert_id
        assert row["review_gate"] != "not_evaluated", alert_id
        for required in ("processing_run_id", "processed_at", "ai_draft_reference",
                         "model_id", "routing_policy_id"):
            assert row[required] != "", (alert_id, required)
        assert row["error_code"] == "", alert_id


# ── Verification derived from the REAL verifier (not hardcoded) ───────────────
def test_verification_statuses_are_derived_from_real_verifier():
    source = pipeline._load_source_tables()
    persisted = _by_alert(_lifecycle())
    for alert_id in PROCESSED_ALERTS:
        claims = pipeline._load_seeded_claims(alert_id)
        results = [verifier.verify_claim(c, source) for c in claims]
        expected = summarize_ai_verification(results).value
        assert persisted[alert_id]["ai_verification"] == expected, alert_id
    # And the flagship outcomes specifically: ALERT002 is the caught contradiction
    # (high_risk_country asserted, no high-risk counterparty) and ALERT033 fails outright.
    assert persisted["ALERT002"]["ai_verification"] == "mixed"
    assert persisted["ALERT033"]["ai_verification"] == "fail"
    # The whole-dataset distribution is derived, never forced.
    counts = _lifecycle()["ai_verification"].value_counts().to_dict()
    assert counts == {"mixed": 26, "pass": 12, "fail": 1}


# ── Provenance honesty ───────────────────────────────────────────────────────
def test_captured_live_provenance_declares_its_model():
    ai = _ai_outputs()
    assert set(ai["draft_source"]) == {"captured_live"}
    assert set(ai["model_id"]) == {LIVE_MODEL_ID}
    assert "" not in set(ai["processing_run_id"])
    lc = _lifecycle()
    assert set(lc["ai_draft_source"]) == {"captured_live"}
    assert set(lc["model_id"]) == {LIVE_MODEL_ID}
    assert "" not in set(lc["processing_run_id"])
    assert "synthetic_fixture" not in set(ai["draft_source"])
    assert "FIXTURE-SEED-V1" not in set(ai["processing_run_id"])


def test_lifecycle_draft_references_match_ai_outputs_provenance():
    ai = _ai_outputs()
    lc = _by_alert(_lifecycle())
    for alert_id in PROCESSED_ALERTS:
        expected = f"{lc[alert_id]['processing_run_id']}:{alert_id}"
        refs = set(ai[ai["alert_id"] == alert_id]["draft_reference"])
        assert refs == {expected}, (alert_id, refs)
        assert lc[alert_id]["ai_draft_reference"] == expected
        # The reference names the capture run and the alert — never a filesystem path.
        assert ":" in expected and "\\" not in expected and "/" not in expected


# ── Disposition truthfulness (final_action, never draft_disposition) ─────────
def test_alert001_and_004_record_final_action_not_draft_disposition():
    hr = _load(DATA / "human_reviews.csv")
    lc = _by_alert(_lifecycle())
    for alert_id, review_id, expected_action in [
        ("ALERT001", "REV003", "monitor"), ("ALERT004", "REV002", "escalate"),
    ]:
        draft_disposition = hr[hr["review_id"] == review_id].iloc[0]["draft_disposition"]
        row = lc[alert_id]
        assert row["review_gate"] == "complete"
        assert row["final_action"] == expected_action
        assert row["final_action"] != draft_disposition        # NOT the review decision
        assert row["human_review_decision"] == draft_disposition
        assert row["disposition_source"] == "human_review"
        assert row["disposition_reference"] == review_id        # provenance = review, not draft


def test_alert007_is_blocked_with_no_disposition():
    row = _by_alert(_lifecycle())["ALERT007"]
    assert row["review_gate"] == "blocked"
    assert row["human_review_id"] == "REV001"
    assert row["final_action"] == ""
    assert row["disposition_source"] == "none"
    assert row["disposition_reference"] == ""


def test_no_closed_case_lacks_final_action_and_provenance():
    closed_found = False
    for row in _by_alert(_lifecycle()).values():
        if derive_queue_status(_reconstruct(row)) is QueueStatus.CLOSED:
            closed_found = True
            assert row["final_action"] != ""
            assert row["disposition_source"] != "none"
            assert row["disposition_reference"] != ""
    assert closed_found  # the assertion above is not vacuous


# ── Baseline == runtime ──────────────────────────────────────────────────────
def test_runtime_and_baseline_are_byte_identical():
    for name in ("ai_outputs.csv", "case_lifecycle.csv"):
        assert (BASELINE / name).exists(), f"missing baseline {name}"
        assert filecmp.cmp(DATA / name, BASELINE / name, shallow=False), name


# ── Generator behavior: isolation, purity, determinism ───────────────────────
def test_targeted_generation_writes_only_two_files(tmp_path):
    generate_lifecycle_dataset(DATA, tmp_path)
    assert sorted(os.listdir(tmp_path)) == ["ai_outputs.csv", "case_lifecycle.csv"]


def test_generation_makes_no_network_call(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("lifecycle generation attempted network access")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    generate_lifecycle_dataset(DATA, tmp_path)  # must complete with no socket use
    assert (tmp_path / "case_lifecycle.csv").exists()


def test_generation_is_deterministic(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    generate_lifecycle_dataset(DATA, a)
    generate_lifecycle_dataset(DATA, b)
    for name in ("ai_outputs.csv", "case_lifecycle.csv"):
        assert (a / name).read_bytes() == (b / name).read_bytes(), name


def test_generated_temp_matches_committed_runtime(tmp_path):
    # The committed runtime files must be exactly what the generator produces now.
    generate_lifecycle_dataset(DATA, tmp_path)
    for name in ("ai_outputs.csv", "case_lifecycle.csv"):
        assert (tmp_path / name).read_bytes() == (DATA / name).read_bytes(), name
