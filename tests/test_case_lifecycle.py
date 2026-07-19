"""Tests for the canonical Case Lifecycle domain model (src/case_lifecycle).

Proves every valid lifecycle path, every rejected invariant violation, that records
are frozen, that queue derivation is deterministic, and that the module is pure — no
network, SDK, filesystem, CSV, environment, clock, application, or Streamlit access.
"""

from __future__ import annotations

import dataclasses
import inspect
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import case_lifecycle as cl  # noqa: E402
from src.case_lifecycle import (  # noqa: E402
    CaseLifecycle, LifecycleInvariantError, derive_queue_status,
    ProcessingStatus as PS, AIDraftSource as DS, AIVerificationStatus as AV,
    ReviewRoutingStatus as RR, ReviewGateStatus as RG, HumanReviewDecision as HRD,
    OverrideStatus as OS, DispositionSource as DPS, QueueStatus as QS,
)

MODEL = "claude-haiku-4-5-20251001"


# ── Valid-record kwargs builders ─────────────────────────────────────────────
def kw_not_processed(alert_id="ALERT008"):
    return dict(alert_id=alert_id, processing_status=PS.NOT_PROCESSED)


def kw_error(alert_id="ALERT099"):
    return dict(alert_id=alert_id, processing_status=PS.ERROR, error_code="E-DRAFT-FAILED")


def _processed_base(alert_id, ai_verif):
    return dict(
        alert_id=alert_id, processing_status=PS.PROCESSED,
        processing_run_id="RUN-001", processed_at="2026-05-21T14:00:00",
        ai_draft_source=DS.SYNTHETIC_FIXTURE, ai_draft_reference="OUT001", model_id=MODEL,
        ai_verification=ai_verif,
    )


def kw_pass_required_pending(alert_id="ALERT002"):
    d = _processed_base(alert_id, AV.PASS)
    d.update(review_routing=RR.REQUIRED, review_gate=RG.PENDING)
    return d


def kw_pass_not_required_system(alert_id="ALERT003"):
    d = _processed_base(alert_id, AV.PASS)
    d.update(review_routing=RR.NOT_REQUIRED, review_gate=RG.NOT_APPLICABLE,
             final_action="monitor", disposition_source=DPS.SYSTEM_POLICY,
             routing_policy_id="POL-LOWRISK-1", disposition_reference="POL-LOWRISK-1")
    return d


def kw_fail_required_complete(alert_id="ALERT001"):
    d = _processed_base(alert_id, AV.FAIL)
    d.update(review_routing=RR.REQUIRED, review_gate=RG.COMPLETE,
             human_review_decision=HRD.EDITED, human_review_id="REV003",
             final_action="monitor", disposition_source=DPS.HUMAN_REVIEW, disposition_reference="REV003")
    return d


def kw_mixed_blocked(alert_id="ALERT007"):
    d = _processed_base(alert_id, AV.MIXED)
    d.update(review_routing=RR.REQUIRED, review_gate=RG.BLOCKED,
             human_review_decision=HRD.ACCEPTED, human_review_id="REV001")  # incomplete -> blocked
    return d


def kw_complete_plus_pending_override(alert_id="ALERT001"):
    d = kw_fail_required_complete(alert_id)
    d.update(override_status=OS.PENDING, override_request_id="CHG-SEED-003")
    return d


# ── Valid paths + queue derivation ───────────────────────────────────────────
def test_valid_not_processed():
    assert derive_queue_status(CaseLifecycle(**kw_not_processed())) is QS.NOT_PROCESSED


def test_valid_processing_error():
    assert derive_queue_status(CaseLifecycle(**kw_error())) is QS.PROCESSING_ERROR


def test_valid_pass_required_pending_awaits_review():
    assert derive_queue_status(CaseLifecycle(**kw_pass_required_pending())) is QS.AWAITING_REVIEW


def test_valid_pass_not_required_system_closed():
    assert derive_queue_status(CaseLifecycle(**kw_pass_not_required_system())) is QS.CLOSED


def test_valid_fail_required_complete_human_closed():
    assert derive_queue_status(CaseLifecycle(**kw_fail_required_complete())) is QS.CLOSED


def test_valid_mixed_blocked_review():
    assert derive_queue_status(CaseLifecycle(**kw_mixed_blocked())) is QS.BLOCKED


def test_valid_complete_plus_pending_override_awaits_manager():
    # ALERT001-shaped: a completed HUMAN disposition coexists with a pending override.
    assert derive_queue_status(CaseLifecycle(**kw_complete_plus_pending_override())) is QS.AWAITING_MANAGER


# ── Rejections ───────────────────────────────────────────────────────────────
def _rejects(kwargs):
    with pytest.raises(LifecycleInvariantError):
        CaseLifecycle(**kwargs)


def test_reject_closed_equivalent_without_final_action():
    d = kw_fail_required_complete(); d["final_action"] = None
    _rejects(d)


def test_reject_final_action_without_provenance():
    d = kw_pass_not_required_system(); d["disposition_source"] = DPS.NONE
    _rejects(d)


def test_reject_system_policy_under_required_routing():
    d = kw_fail_required_complete(); d["disposition_source"] = DPS.SYSTEM_POLICY
    _rejects(d)


def test_reject_human_review_under_not_required_routing():
    d = kw_pass_not_required_system(); d["disposition_source"] = DPS.HUMAN_REVIEW
    _rejects(d)


def test_reject_fail_routed_not_required():
    d = _processed_base("ALERT009", AV.FAIL)
    d.update(review_routing=RR.NOT_REQUIRED, review_gate=RG.NOT_APPLICABLE,
             final_action="monitor", disposition_source=DPS.SYSTEM_POLICY,
             routing_policy_id="POL-1", disposition_reference="POL-1")
    _rejects(d)


def test_reject_mixed_routed_not_required():
    d = _processed_base("ALERT010", AV.MIXED)
    d.update(review_routing=RR.NOT_REQUIRED, review_gate=RG.NOT_APPLICABLE,
             final_action="monitor", disposition_source=DPS.SYSTEM_POLICY,
             routing_policy_id="POL-1", disposition_reference="POL-1")
    _rejects(d)


def test_reject_blocked_with_final_action():
    d = kw_mixed_blocked(); d["final_action"] = "monitor"
    _rejects(d)


def test_reject_pending_review_with_final_action():
    d = kw_pass_required_pending(); d["final_action"] = "monitor"
    _rejects(d)


def test_reject_complete_without_human_review_id():
    d = kw_fail_required_complete(); d["human_review_id"] = None
    _rejects(d)


def test_reject_complete_without_final_action():
    d = kw_fail_required_complete(); d["final_action"] = None
    _rejects(d)


def test_reject_edited_used_as_final_action():
    # EDITED is a review decision, not a case action; it may not be final_action.
    d = kw_pass_not_required_system(); d["final_action"] = "EDITED"
    _rejects(d)
    d2 = kw_fail_required_complete(); d2["final_action"] = "edited"
    _rejects(d2)


def test_reject_not_required_without_routing_policy_id():
    d = kw_pass_not_required_system(); d["routing_policy_id"] = None
    _rejects(d)


def test_reject_not_required_with_human_review_fields():
    d = kw_pass_not_required_system(); d["human_review_decision"] = HRD.ACCEPTED
    _rejects(d)
    d2 = kw_pass_not_required_system(); d2["human_review_id"] = "REV001"
    _rejects(d2)


def test_reject_processed_without_run_provenance():
    d = kw_pass_required_pending(); d["processing_run_id"] = None
    _rejects(d)
    d2 = kw_pass_required_pending(); d2["processed_at"] = None
    _rejects(d2)


def test_reject_not_processed_with_processing_provenance():
    d = kw_not_processed(); d["processing_run_id"] = "RUN-1"
    _rejects(d)
    d2 = kw_not_processed(); d2["ai_verification"] = AV.PASS
    _rejects(d2)


def test_reject_error_without_error_code():
    d = kw_error(); d["error_code"] = None
    _rejects(d)


def test_reject_empty_string_identifiers():
    d = kw_not_processed(); d["alert_id"] = ""
    _rejects(d)
    d2 = kw_pass_required_pending(); d2["processing_run_id"] = ""
    _rejects(d2)
    d3 = kw_fail_required_complete(); d3["human_review_id"] = "  "
    _rejects(d3)


def test_reject_pending_override_without_request_id():
    d = kw_complete_plus_pending_override(); d["override_request_id"] = None
    _rejects(d)


def test_reject_override_request_id_when_status_none():
    d = kw_fail_required_complete(); d["override_request_id"] = "CHG-1"   # override_status is NONE
    _rejects(d)


def test_reject_not_processed_error_code_present():
    d = kw_not_processed(); d["error_code"] = "E-1"
    _rejects(d)


# ── Frozen / deterministic ───────────────────────────────────────────────────
def test_records_are_frozen():
    rec = CaseLifecycle(**kw_fail_required_complete())
    with pytest.raises(dataclasses.FrozenInstanceError):
        rec.final_action = "escalate"  # type: ignore[misc]


def test_derive_queue_status_is_deterministic():
    records = [
        (CaseLifecycle(**kw_not_processed()), QS.NOT_PROCESSED),
        (CaseLifecycle(**kw_error()), QS.PROCESSING_ERROR),
        (CaseLifecycle(**kw_pass_required_pending()), QS.AWAITING_REVIEW),
        (CaseLifecycle(**kw_pass_not_required_system()), QS.CLOSED),
        (CaseLifecycle(**kw_fail_required_complete()), QS.CLOSED),
        (CaseLifecycle(**kw_mixed_blocked()), QS.BLOCKED),
        (CaseLifecycle(**kw_complete_plus_pending_override()), QS.AWAITING_MANAGER),
    ]
    for rec, expected in records:
        first = derive_queue_status(rec)
        assert first is expected
        for _ in range(5):
            assert derive_queue_status(rec) is first  # same input -> same output, always


def test_pending_override_priority_beats_blocked():
    # An override pending on a BLOCKED case still routes to the manager, not BLOCKED.
    d = kw_mixed_blocked(); d.update(override_status=OS.PENDING, override_request_id="CHG-X")
    assert derive_queue_status(CaseLifecycle(**d)) is QS.AWAITING_MANAGER


# ── Purity: no network / SDK / filesystem / CSV / env / clock / app / streamlit
def test_module_is_pure_no_forbidden_imports():
    src = inspect.getsource(cl)
    forbidden = [
        "import os", "import sys", "import socket", "import pandas", "import csv",
        "import datetime", "from datetime", "import time", "import pathlib",
        "from pathlib", "import requests", "import httpx", "import urllib",
        "import anthropic", "import streamlit", "streamlit", "import app", "from app",
        "open(", "os.environ", "read_csv", "to_csv", ".now(",
    ]
    for token in forbidden:
        assert token not in src, f"module unexpectedly references {token!r}"
    for name in ("os", "sys", "socket", "pd", "pandas", "csv", "datetime", "time",
                 "anthropic", "streamlit", "Path", "requests"):
        assert not hasattr(cl, name), f"module unexpectedly bound {name!r}"


def test_no_network_access_during_derivation(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("network access attempted by the lifecycle model")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    for kw in (kw_not_processed(), kw_pass_not_required_system(),
               kw_complete_plus_pending_override()):
        derive_queue_status(CaseLifecycle(**kw))


def test_queue_status_is_derived_only_not_a_field():
    field_names = {f.name for f in dataclasses.fields(CaseLifecycle)}
    assert "queue_status" not in field_names
    assert len(field_names) == 19
