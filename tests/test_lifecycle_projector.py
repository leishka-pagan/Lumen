"""Tests for the pure lifecycle projector (src/lifecycle_projector).

The projector converts REAL subsystem outputs — src.verifier.VerificationResult objects,
a src.review_routing.ReviewRoutingDecision, a HumanReview row evaluated through the real
src.review_gate.evaluate_review, and override state — into one canonical CaseLifecycle.

Coverage:
  * summarize_ai_verification over PASS / FAIL / MIXED and its rejections.
  * Seven demo-path shapes (ALERT001..007) asserted end-to-end, including the derived
    queue status, using the real verifier and review-gate contracts.
  * A valid captured-live projection.
  * Adversarial inputs (empty/malformed results, routing incompatibility, contradictory
    review/override/provenance, raw NaN / malformed identifiers) all fail closed.
  * The load-bearing invariant that draft_disposition is NEVER used as final_action.
  * Purity: deterministic, frozen output, no file / network / audit access, no forbidden
    imports or bound names.
"""

from __future__ import annotations

import builtins
import dataclasses
import inspect
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.case_lifecycle import (  # noqa: E402
    AIDraftSource, AIVerificationStatus as AV, CaseLifecycle, DispositionSource,
    HumanReviewDecision, LifecycleInvariantError, OverrideStatus, QueueStatus,
    ReviewGateStatus, ReviewRoutingStatus, derive_queue_status,
)
from src.lifecycle_projector import (  # noqa: E402
    LifecycleProjectionError, project_processed_lifecycle, summarize_ai_verification,
)
from src.review_gate import evaluate_review  # noqa: E402
from src.review_routing import route_review  # noqa: E402
from src.verifier import VerificationResult  # noqa: E402

REJECT = (LifecycleProjectionError, LifecycleInvariantError)


# ── Real verifier results (the projector consumes these; it does not rerun verify) ──
PASS_RESULT = VerificationResult(status="PASS", reason="claim confirmed against source")
FAIL_RESULT = VerificationResult(status="FAIL", reason="claim contradicted by source")


def _complete_review(review_id, alert_id, draft_disposition, final_action):
    """A HumanReview row that the real gate accepts as COMPLETE."""
    return {
        "review_id": review_id, "alert_id": alert_id, "reviewer": "reviewer:analyst",
        "evidence_reviewed": "True", "draft_disposition": draft_disposition,
        "decision_reason": "documented rationale", "final_note": "documented final note",
        "final_action": final_action, "reviewed_at": "2026-01-02T00:00:00Z",
    }


def _project(**overrides):
    """Project with sensible synthetic-fixture defaults; override per test."""
    params = dict(
        alert_id="ALERT000", processing_run_id="RUN-1",
        processed_at="2026-01-01T00:00:00Z",
        ai_draft_source=AIDraftSource.SYNTHETIC_FIXTURE, ai_draft_reference="OUT001",
        model_id=None, verification_results=[PASS_RESULT],
        routing_decision=route_review(ai_verification=AV.PASS, policy_id="POL-D",
                                      auto_disposition_action="monitor"),
        human_review=None, override_status=OverrideStatus.NONE, override_request_id=None,
    )
    params.update(overrides)
    return project_processed_lifecycle(**params)


# ═══ summarize_ai_verification ═══════════════════════════════════════════════
def test_summarize_all_pass():
    assert summarize_ai_verification([PASS_RESULT, PASS_RESULT]) is AV.PASS


def test_summarize_all_fail():
    assert summarize_ai_verification([FAIL_RESULT]) is AV.FAIL


def test_summarize_mixed():
    assert summarize_ai_verification([PASS_RESULT, FAIL_RESULT]) is AV.MIXED
    assert summarize_ai_verification([FAIL_RESULT, PASS_RESULT]) is AV.MIXED


def test_summarize_requires_at_least_one_result():
    with pytest.raises(LifecycleProjectionError):
        summarize_ai_verification([])


def test_summarize_rejects_malformed_result_objects():
    for bad in ({"status": "PASS"}, "PASS", 1, None, ["PASS"], object()):
        with pytest.raises(LifecycleProjectionError):
            summarize_ai_verification([PASS_RESULT, bad])


def test_summarize_rejects_indeterminate_or_unknown_status():
    # NEEDS_REVIEW is a real verifier status the PASS/FAIL/MIXED model cannot represent.
    needs_review = VerificationResult(status="NEEDS_REVIEW", reason="schema gate")
    with pytest.raises(LifecycleProjectionError):
        summarize_ai_verification([needs_review])
    unknown = VerificationResult(status="WHATEVER", reason="x")
    with pytest.raises(LifecycleProjectionError):
        summarize_ai_verification([PASS_RESULT, unknown])


def test_summarize_consumes_results_not_claims():
    # It reads .status off finished results; it neither needs nor accepts a raw claim dict
    # (proving verification is not rerun here).
    with pytest.raises(LifecycleProjectionError):
        summarize_ai_verification([{"claim_type": "kyc_status", "value": "x"}])


# ═══ Seven demo-path shapes (real verifier + real review gate) ═══════════════
def test_alert001_fail_required_complete_human_review_closed():
    # FAIL verification -> REQUIRED; REV003 complete (edited, monitor); no override.
    rd = route_review(ai_verification=AV.FAIL, policy_id="POL-A1")
    review = _complete_review("REV003", "ALERT001", "edited", "monitor")
    lc = _project(alert_id="ALERT001", ai_draft_reference="OUT003",
                  verification_results=[FAIL_RESULT], routing_decision=rd,
                  human_review=review)
    assert lc.ai_verification is AV.FAIL
    assert lc.review_routing is ReviewRoutingStatus.REQUIRED
    assert lc.review_gate is ReviewGateStatus.COMPLETE
    assert lc.human_review_decision is HumanReviewDecision.EDITED
    assert lc.human_review_id == "REV003"
    assert lc.final_action == "monitor"
    assert lc.disposition_source is DispositionSource.HUMAN_REVIEW
    assert lc.disposition_reference == "REV003"
    assert derive_queue_status(lc) is QueueStatus.CLOSED


def test_alert002_pass_auto_not_required_with_pending_override_awaiting_manager():
    rd = route_review(ai_verification=AV.PASS, policy_id="POL-A2", auto_disposition_action="monitor")
    lc = _project(alert_id="ALERT002", ai_draft_reference="OUT002", routing_decision=rd,
                  override_status=OverrideStatus.PENDING, override_request_id="CHG-SEED-001")
    assert lc.review_routing is ReviewRoutingStatus.NOT_REQUIRED
    assert lc.review_gate is ReviewGateStatus.NOT_APPLICABLE
    assert lc.human_review_decision is HumanReviewDecision.NONE
    assert lc.human_review_id is None
    assert lc.final_action == "monitor"
    assert lc.disposition_source is DispositionSource.SYSTEM_POLICY
    assert lc.disposition_reference == "POL-A2"
    assert lc.override_status is OverrideStatus.PENDING
    assert derive_queue_status(lc) is QueueStatus.AWAITING_MANAGER  # pending override wins


def test_alert003_pass_auto_not_required_no_override_closed():
    rd = route_review(ai_verification=AV.PASS, policy_id="POL-A3", auto_disposition_action="monitor")
    lc = _project(alert_id="ALERT003", ai_draft_reference="OUT003b", routing_decision=rd)
    assert lc.review_routing is ReviewRoutingStatus.NOT_REQUIRED
    assert lc.review_gate is ReviewGateStatus.NOT_APPLICABLE
    assert lc.final_action == "monitor"
    assert lc.disposition_source is DispositionSource.SYSTEM_POLICY
    assert lc.disposition_reference == "POL-A3"
    assert derive_queue_status(lc) is QueueStatus.CLOSED


def test_alert004_pass_mandatory_required_complete_escalate_closed():
    rd = route_review(ai_verification=AV.PASS, policy_id="POL-A4",
                      mandatory_review_reasons=("RULE_REQUIRES_HUMAN_REVIEW",))
    review = _complete_review("REV002", "ALERT004", "edited", "escalate")
    lc = _project(alert_id="ALERT004", ai_draft_reference="OUT004", routing_decision=rd,
                  human_review=review)
    assert lc.review_routing is ReviewRoutingStatus.REQUIRED
    assert lc.review_gate is ReviewGateStatus.COMPLETE
    assert lc.human_review_decision is HumanReviewDecision.EDITED
    assert lc.human_review_id == "REV002"
    assert lc.final_action == "escalate"
    assert lc.disposition_source is DispositionSource.HUMAN_REVIEW
    assert lc.disposition_reference == "REV002"
    assert derive_queue_status(lc) is QueueStatus.CLOSED


def test_alert005_pass_auto_not_required_with_pending_override_awaiting_manager():
    rd = route_review(ai_verification=AV.PASS, policy_id="POL-A5", auto_disposition_action="monitor")
    lc = _project(alert_id="ALERT005", ai_draft_reference="OUT005", routing_decision=rd,
                  override_status=OverrideStatus.PENDING, override_request_id="CHG-SEED-002")
    assert lc.review_routing is ReviewRoutingStatus.NOT_REQUIRED
    assert lc.final_action == "monitor"
    assert derive_queue_status(lc) is QueueStatus.AWAITING_MANAGER


def test_alert006_pass_mandatory_required_no_review_awaiting_review():
    rd = route_review(ai_verification=AV.PASS, policy_id="POL-A6",
                      mandatory_review_reasons=("CRITICAL_EVIDENCE_MISSING",))
    lc = _project(alert_id="ALERT006", ai_draft_reference="OUT006", routing_decision=rd,
                  human_review=None)
    assert lc.review_routing is ReviewRoutingStatus.REQUIRED
    assert lc.review_gate is ReviewGateStatus.PENDING
    assert lc.human_review_decision is HumanReviewDecision.NONE
    assert lc.human_review_id is None
    assert lc.final_action is None
    assert lc.disposition_source is DispositionSource.NONE
    assert lc.disposition_reference is None
    assert derive_queue_status(lc) is QueueStatus.AWAITING_REVIEW


def test_alert007_mixed_required_incomplete_review_blocked():
    # ALERT007: one PASS + one FAIL claim -> MIXED; REV001 is a rubber stamp -> BLOCKED.
    rd = route_review(ai_verification=AV.MIXED, policy_id="POL-A7")
    review = {
        "review_id": "REV001", "alert_id": "ALERT007", "reviewer": "reviewer:analyst",
        "evidence_reviewed": "False", "draft_disposition": "accepted",
        "decision_reason": "", "final_note": "", "final_action": "",
        "reviewed_at": "2026-01-03T00:00:00Z",
    }
    # Sanity: the real gate blocks this review.
    assert evaluate_review(review).blocked is True
    lc = _project(alert_id="ALERT007", ai_draft_reference="OUT007",
                  verification_results=[PASS_RESULT, FAIL_RESULT], routing_decision=rd,
                  human_review=review)
    assert lc.ai_verification is AV.MIXED
    assert lc.review_gate is ReviewGateStatus.BLOCKED
    assert lc.human_review_id == "REV001"
    assert lc.final_action is None
    assert lc.disposition_source is DispositionSource.NONE
    assert lc.disposition_reference is None
    assert derive_queue_status(lc) is QueueStatus.BLOCKED


# ═══ Captured-live provenance is supported when coherent ═════════════════════
def test_captured_live_with_model_id_is_valid():
    rd = route_review(ai_verification=AV.PASS, policy_id="POL-L", auto_disposition_action="monitor")
    lc = _project(ai_draft_source=AIDraftSource.CAPTURED_LIVE, ai_draft_reference="OUT-ab12cd34",
                  model_id="claude-haiku-4-5-20251001", routing_decision=rd)
    assert lc.ai_draft_source is AIDraftSource.CAPTURED_LIVE
    assert lc.model_id == "claude-haiku-4-5-20251001"


def test_enum_string_inputs_are_normalized():
    # Legitimate scalar CSV values (enum strings) are converted explicitly.
    rd = route_review(ai_verification=AV.PASS, policy_id="POL-N", auto_disposition_action="monitor")
    lc = _project(ai_draft_source="synthetic_fixture", override_status="none",
                  routing_decision=rd)
    assert lc.ai_draft_source is AIDraftSource.SYNTHETIC_FIXTURE
    assert lc.override_status is OverrideStatus.NONE


# ═══ Adversarial: routing / review / override / provenance contradictions ════
def test_reject_empty_verification_results():
    with pytest.raises(REJECT):
        _project(verification_results=[])


def test_reject_malformed_verification_results():
    with pytest.raises(REJECT):
        _project(verification_results=[{"status": "PASS"}])


def test_reject_fail_or_mixed_paired_with_not_required():
    not_required = route_review(ai_verification=AV.PASS, policy_id="POL-X",
                                auto_disposition_action="monitor")
    with pytest.raises(REJECT):
        _project(verification_results=[FAIL_RESULT], routing_decision=not_required)
    with pytest.raises(REJECT):
        _project(verification_results=[PASS_RESULT, FAIL_RESULT], routing_decision=not_required)


def test_reject_not_required_carrying_a_human_review():
    not_required = route_review(ai_verification=AV.PASS, policy_id="POL-X",
                                auto_disposition_action="monitor")
    review = _complete_review("REV900", "ALERTX", "accepted", "monitor")
    with pytest.raises(REJECT):
        _project(routing_decision=not_required, human_review=review)


def test_missing_final_action_blocks_rather_than_leaking_complete():
    # A review complete except for final_action must BLOCK (never COMPLETE without action).
    rd = route_review(ai_verification=AV.PASS, policy_id="POL-A4",
                      mandatory_review_reasons=("RULE_REQUIRES_HUMAN_REVIEW",))
    review = _complete_review("REV010", "ALERTX", "accepted", "")
    lc = _project(routing_decision=rd, human_review=review)
    assert lc.review_gate is ReviewGateStatus.BLOCKED
    assert lc.final_action is None
    assert lc.disposition_source is DispositionSource.NONE


def test_reject_unknown_draft_disposition():
    rd = route_review(ai_verification=AV.PASS, policy_id="POL-A4",
                      mandatory_review_reasons=("RULE_REQUIRES_HUMAN_REVIEW",))
    review = _complete_review("REV011", "ALERTX", "maybe-later", "monitor")
    with pytest.raises(REJECT):
        _project(routing_decision=rd, human_review=review)


def test_blocked_review_never_leaks_a_present_final_action():
    # Review carries final_action="escalate" but is blocked for a different defect
    # (empty decision_reason). The final action must NOT survive into the lifecycle.
    rd = route_review(ai_verification=AV.PASS, policy_id="POL-A4",
                      mandatory_review_reasons=("RULE_REQUIRES_HUMAN_REVIEW",))
    review = _complete_review("REV012", "ALERTX", "accepted", "escalate")
    review["decision_reason"] = ""
    assert evaluate_review(review).blocked is True
    lc = _project(routing_decision=rd, human_review=review)
    assert lc.review_gate is ReviewGateStatus.BLOCKED
    assert lc.human_review_id == "REV012"
    assert lc.final_action is None
    assert lc.disposition_source is DispositionSource.NONE
    assert lc.disposition_reference is None


def test_reject_synthetic_fixture_with_model_id():
    with pytest.raises(REJECT):
        _project(ai_draft_source=AIDraftSource.SYNTHETIC_FIXTURE, ai_draft_reference="OUT1",
                 model_id="claude-haiku-4-5-20251001")


def test_reject_captured_live_missing_model_id():
    rd = route_review(ai_verification=AV.PASS, policy_id="POL-L", auto_disposition_action="monitor")
    with pytest.raises(REJECT):
        _project(ai_draft_source=AIDraftSource.CAPTURED_LIVE, ai_draft_reference="OUT1",
                 model_id=None, routing_decision=rd)


def test_reject_raw_nan_identifiers():
    nan = float("nan")
    with pytest.raises(REJECT):
        _project(alert_id=nan)
    with pytest.raises(REJECT):
        _project(processing_run_id=nan)
    # A review whose review_id is NaN cannot be given a canonical identity.
    rd = route_review(ai_verification=AV.FAIL, policy_id="POL-A1")
    review = _complete_review(nan, "ALERTX", "edited", "monitor")
    with pytest.raises(REJECT):
        _project(verification_results=[FAIL_RESULT], routing_decision=rd, human_review=review)


def test_reject_malformed_identifier_types():
    for bad in (123, True, ["ALERT"], 4.0):
        with pytest.raises(REJECT):
            _project(alert_id=bad)


def test_reject_unknown_enum_string():
    with pytest.raises(REJECT):
        _project(ai_draft_source="teleported")
    with pytest.raises(REJECT):
        _project(override_status="haggling")


def test_reject_non_routing_decision():
    with pytest.raises(REJECT):
        _project(routing_decision="NOT_REQUIRED")


# ═══ Load-bearing invariant: draft_disposition is never the final action ═════
def test_draft_disposition_is_never_used_as_final_action():
    rd = route_review(ai_verification=AV.PASS, policy_id="POL-A4",
                      mandatory_review_reasons=("RULE_REQUIRES_HUMAN_REVIEW",))
    for disposition, action, expected in (
        ("accepted", "monitor", HumanReviewDecision.ACCEPTED),
        ("edited", "escalate", HumanReviewDecision.EDITED),
        ("rejected", "close_no_action", HumanReviewDecision.REJECTED),
    ):
        review = _complete_review("REV020", "ALERTX", disposition, action)
        lc = _project(routing_decision=rd, human_review=review)
        assert lc.human_review_decision is expected
        assert lc.final_action == action
        assert lc.final_action != disposition
        # provenance points at the review, not at the decision word.
        assert lc.disposition_reference == "REV020"


def test_overrides_are_a_separate_axis_from_disposition():
    # Identical disposition inputs; only the override differs -> same disposition fields,
    # different queue status. Overrides never change routing or the recorded disposition.
    rd = route_review(ai_verification=AV.PASS, policy_id="POL-A2", auto_disposition_action="monitor")
    closed = _project(routing_decision=rd, override_status=OverrideStatus.NONE,
                      override_request_id=None)
    pending = _project(routing_decision=rd, override_status=OverrideStatus.PENDING,
                       override_request_id="CHG-1")
    assert closed.final_action == pending.final_action == "monitor"
    assert closed.disposition_source is pending.disposition_source is DispositionSource.SYSTEM_POLICY
    assert closed.disposition_reference == pending.disposition_reference == "POL-A2"
    assert derive_queue_status(closed) is QueueStatus.CLOSED
    assert derive_queue_status(pending) is QueueStatus.AWAITING_MANAGER


# ═══ Purity: deterministic, frozen, no I/O, no forbidden dependencies ════════
def test_projection_is_deterministic():
    rd = route_review(ai_verification=AV.FAIL, policy_id="POL-A1")
    review = _complete_review("REV003", "ALERT001", "edited", "monitor")
    records = [
        _project(alert_id="ALERT001", verification_results=[FAIL_RESULT],
                 routing_decision=rd, human_review=review)
        for _ in range(5)
    ]
    assert all(r == records[0] for r in records)


def test_result_is_frozen():
    lc = _project()
    assert isinstance(lc, CaseLifecycle)
    with pytest.raises(dataclasses.FrozenInstanceError):
        lc.final_action = "tampered"  # type: ignore[misc]


def test_projection_touches_no_files_or_network(monkeypatch):
    def _no_open(*a, **k):
        raise AssertionError("projection attempted file access")

    def _no_net(*a, **k):
        raise AssertionError("projection attempted network access")

    monkeypatch.setattr(builtins, "open", _no_open)
    monkeypatch.setattr(socket, "socket", _no_net)
    monkeypatch.setattr(socket, "create_connection", _no_net)

    rd = route_review(ai_verification=AV.FAIL, policy_id="POL-A1")
    review = _complete_review("REV003", "ALERT001", "edited", "monitor")
    lc = _project(alert_id="ALERT001", verification_results=[FAIL_RESULT],
                  routing_decision=rd, human_review=review)
    assert lc.review_gate is ReviewGateStatus.COMPLETE  # full path ran with no I/O


def test_module_is_pure_no_forbidden_dependencies():
    import src.lifecycle_projector as lp

    source = inspect.getsource(lp)
    forbidden_tokens = [
        "import os", "import socket", "import pandas", "import csv", "import datetime",
        "from datetime", "import time", "import pathlib", "from pathlib", "import requests",
        "import httpx", "import urllib", "import anthropic", "import streamlit", "streamlit",
        "import app", "from app", "open(", "os.environ", "read_csv", "to_csv", ".now(",
        "from src.audit", "import audit", "audit_log", "from src.pipeline", "verify_claim",
    ]
    for token in forbidden_tokens:
        assert token not in source, f"projector unexpectedly references {token!r}"
    for name in ("os", "socket", "pd", "pandas", "csv", "datetime", "time", "anthropic",
                 "streamlit", "Path", "requests", "audit"):
        assert not hasattr(lp, name), f"projector unexpectedly bound {name!r}"
