"""Tests for the hardened canonical Case Lifecycle domain model (src/case_lifecycle).

Proves every valid lifecycle path, the new runtime-type + provenance rules (each
reproduced defect has an adversarial test), that records are frozen, that queue
derivation is unchanged/deterministic, and that the module is pure — no network, SDK,
filesystem, CSV, environment, clock, application, or Streamlit access.
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
POLICY = "POL-ROUTE-1"


# ── Valid-record kwargs builders ─────────────────────────────────────────────
def kw_not_processed(alert_id="ALERT008"):
    return dict(alert_id=alert_id, processing_status=PS.NOT_PROCESSED)


def kw_error(alert_id="ALERT099"):
    return dict(alert_id=alert_id, processing_status=PS.ERROR, error_code="E-DRAFT-FAILED")


def _processed_base(alert_id, ai_verif, source=DS.SYNTHETIC_FIXTURE):
    # captured live drafts carry the Haiku model id; synthetic fixtures carry model_id=None;
    # every PROCESSED record includes routing_policy_id.
    return dict(
        alert_id=alert_id, processing_status=PS.PROCESSED,
        processing_run_id="RUN-001", processed_at="2026-05-21T14:00:00",
        ai_draft_source=source, ai_draft_reference="OUT001",
        model_id=(MODEL if source is DS.CAPTURED_LIVE else None),
        ai_verification=ai_verif, routing_policy_id=POLICY,
    )


def kw_pass_required_pending(alert_id="ALERT002"):
    d = _processed_base(alert_id, AV.PASS)
    d.update(review_routing=RR.REQUIRED, review_gate=RG.PENDING)
    return d


def kw_pass_not_required_system(alert_id="ALERT003"):
    d = _processed_base(alert_id, AV.PASS)
    d.update(review_routing=RR.NOT_REQUIRED, review_gate=RG.NOT_APPLICABLE,
             final_action="monitor", disposition_source=DPS.SYSTEM_POLICY,
             disposition_reference=POLICY)  # == routing_policy_id
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
             human_review_decision=HRD.ACCEPTED, human_review_id="REV001")  # id required, decision free
    return d


def kw_complete_plus_pending_override(alert_id="ALERT001"):
    d = kw_fail_required_complete(alert_id)
    d.update(override_status=OS.PENDING, override_request_id="CHG-SEED-003")
    return d


def _rejects(kwargs):
    with pytest.raises(LifecycleInvariantError):
        CaseLifecycle(**kwargs)


# ── Rule 1: enums are stable lowercase str values inheriting from str+Enum ────
def test_enum_values_are_lowercase_strings():
    for enum_cls in (PS, DS, AV, RR, RG, HRD, OS, DPS, QS):
        for member in enum_cls:
            assert isinstance(member, str)                      # inherits str
            assert isinstance(member.value, str) and member.value == member.value.lower()
    assert PS.PROCESSED.value == "processed"
    assert DS.SYNTHETIC_FIXTURE.value == "synthetic_fixture"
    assert HRD.EDITED.value == "edited"


# ── Valid paths + queue derivation (preserved) ───────────────────────────────
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
    assert derive_queue_status(CaseLifecycle(**kw_complete_plus_pending_override())) is QS.AWAITING_MANAGER


def test_valid_captured_live_draft():
    d = _processed_base("ALERT004", AV.PASS, source=DS.CAPTURED_LIVE)
    d.update(review_routing=RR.NOT_REQUIRED, review_gate=RG.NOT_APPLICABLE,
             final_action="monitor", disposition_source=DPS.SYSTEM_POLICY, disposition_reference=POLICY)
    rec = CaseLifecycle(**d)
    assert rec.model_id == MODEL and rec.ai_draft_source is DS.CAPTURED_LIVE
    assert derive_queue_status(rec) is QS.CLOSED


def test_valid_blocked_with_id_and_none_decision():
    d = kw_mixed_blocked(); d["human_review_decision"] = HRD.NONE
    assert derive_queue_status(CaseLifecycle(**d)) is QS.BLOCKED


# ── Defect 1: enum-field runtime types ───────────────────────────────────────
def test_reject_plain_string_processing_status():
    d = kw_not_processed(); d["processing_status"] = "not_processed"     # raw str
    _rejects(d)


def test_reject_none_and_wrong_enum_and_scalar_in_enum_fields():
    d = kw_not_processed(); d["processing_status"] = None
    _rejects(d)
    d = kw_pass_required_pending(); d["ai_draft_source"] = QS.CLOSED     # member of another enum
    _rejects(d)
    d = kw_pass_required_pending(); d["ai_verification"] = 1             # int
    _rejects(d)
    d = kw_pass_required_pending(); d["review_gate"] = True              # bool
    _rejects(d)


# ── Defect 2: optional-string runtime types ──────────────────────────────────
def test_reject_int_nan_and_bad_types_in_optional_strings():
    d = kw_pass_required_pending(); d["processing_run_id"] = 5           # int
    _rejects(d)
    d = kw_pass_required_pending(); d["ai_draft_reference"] = float("nan")  # NaN
    _rejects(d)
    d = kw_pass_not_required_system(); d["final_action"] = []            # list
    _rejects(d)
    d = _processed_base("ALERT004", AV.PASS, source=DS.CAPTURED_LIVE); d["model_id"] = 5
    d.update(review_routing=RR.NOT_REQUIRED, review_gate=RG.NOT_APPLICABLE,
             final_action="monitor", disposition_source=DPS.SYSTEM_POLICY, disposition_reference=POLICY)
    _rejects(d)
    d = kw_fail_required_complete(); d["final_action"] = HRD.EDITED      # enum member, not exact str
    _rejects(d)


def test_reject_empty_whitespace_and_surrounding_whitespace_strings():
    d = kw_not_processed(); d["alert_id"] = " ALERT008 "                 # surrounding whitespace
    _rejects(d)
    d = kw_not_processed(); d["alert_id"] = ""                           # empty
    _rejects(d)
    d = kw_pass_required_pending(); d["processing_run_id"] = "   "       # whitespace only
    _rejects(d)
    d = kw_pass_required_pending(); d["processing_run_id"] = " RUN-1 "   # surrounding whitespace
    _rejects(d)


# ── Defect 3 + rule 5: AI-draft provenance ──────────────────────────────────
def test_reject_synthetic_fixture_with_model_id():
    d = kw_pass_not_required_system(); d["model_id"] = MODEL             # SYNTHETIC_FIXTURE must be None
    _rejects(d)


def test_reject_captured_live_without_model_or_reference():
    d = _processed_base("ALERT004", AV.PASS, source=DS.CAPTURED_LIVE)
    d.update(review_routing=RR.NOT_REQUIRED, review_gate=RG.NOT_APPLICABLE,
             final_action="monitor", disposition_source=DPS.SYSTEM_POLICY, disposition_reference=POLICY)
    no_model = dict(d); no_model["model_id"] = None
    _rejects(no_model)
    no_ref = dict(d); no_ref["ai_draft_reference"] = None
    _rejects(no_ref)


def test_reject_draft_source_none_with_reference_or_model():
    d = kw_not_processed(); d["ai_draft_reference"] = "OUT001"           # NONE source -> must be None
    _rejects(d)
    d2 = kw_not_processed(); d2["model_id"] = MODEL
    _rejects(d2)


# ── Defect 4 + rule 6: routing_policy_id on every PROCESSED ──────────────────
def test_reject_processed_required_without_routing_policy_id():
    d = kw_pass_required_pending(); d["routing_policy_id"] = None
    _rejects(d)


def test_reject_processed_not_required_without_routing_policy_id():
    d = kw_pass_not_required_system(); d["routing_policy_id"] = None; d["disposition_reference"] = None
    _rejects(d)


# ── Defect 7 + rule 7: NOT_PROCESSED routing_policy_id must be None ──────────
def test_reject_not_processed_with_routing_policy_id():
    d = kw_not_processed(); d["routing_policy_id"] = POLICY
    _rejects(d)


# ── Defect 5 + rule 8: REQUIRED/PENDING has no completed-review metadata ─────
def test_reject_required_pending_with_review_decision():
    d = kw_pass_required_pending(); d["human_review_decision"] = HRD.EDITED
    _rejects(d)


def test_reject_required_pending_with_human_review_id():
    d = kw_pass_required_pending(); d["human_review_id"] = "REV003"
    _rejects(d)


def test_reject_required_pending_with_final_action():
    d = kw_pass_required_pending(); d["final_action"] = "monitor"
    _rejects(d)


# ── Defect 6 + rule 9: REQUIRED/BLOCKED requires human_review_id ─────────────
def test_reject_required_blocked_without_human_review_id():
    d = kw_mixed_blocked(); d["human_review_id"] = None
    _rejects(d)


def test_reject_blocked_with_final_action():
    d = kw_mixed_blocked(); d["final_action"] = "monitor"
    _rejects(d)


# ── Rule 10: COMPLETE / NOT_REQUIRED preserved ───────────────────────────────
def test_reject_complete_without_human_review_id():
    d = kw_fail_required_complete(); d["human_review_id"] = None
    _rejects(d)


def test_reject_complete_without_final_action():
    d = kw_fail_required_complete(); d["final_action"] = None
    _rejects(d)


def test_reject_system_policy_under_required_routing():
    d = kw_fail_required_complete(); d["disposition_source"] = DPS.SYSTEM_POLICY
    _rejects(d)


def test_reject_human_review_under_not_required_routing():
    d = kw_pass_not_required_system(); d["disposition_source"] = DPS.HUMAN_REVIEW
    _rejects(d)


def test_reject_fail_or_mixed_routed_not_required():
    for av in (AV.FAIL, AV.MIXED):
        d = _processed_base("ALERT009", av)
        d.update(review_routing=RR.NOT_REQUIRED, review_gate=RG.NOT_APPLICABLE,
                 final_action="monitor", disposition_source=DPS.SYSTEM_POLICY, disposition_reference=POLICY)
        _rejects(d)


def test_reject_not_required_with_human_review_fields():
    d = kw_pass_not_required_system(); d["human_review_decision"] = HRD.ACCEPTED
    _rejects(d)
    d2 = kw_pass_not_required_system(); d2["human_review_id"] = "REV001"
    _rejects(d2)


def test_reject_edited_used_as_final_action():
    d = kw_pass_not_required_system(); d["final_action"] = "EDITED"
    _rejects(d)


# ── Rule 11: ERROR flexibility preserved ─────────────────────────────────────
def test_valid_error_with_partial_typevalid_provenance():
    d = kw_error("ALERT050")
    d.update(processing_run_id="RUN-9", processed_at="2026-05-21T14:00:00",
             ai_draft_source=DS.SYNTHETIC_FIXTURE, ai_draft_reference="OUT050",
             ai_verification=AV.PASS, review_routing=RR.REQUIRED, review_gate=RG.PENDING,
             override_status=OS.PENDING, override_request_id="CHG-9")
    rec = CaseLifecycle(**d)                                             # accepted: partial but type-valid
    assert derive_queue_status(rec) is QS.PROCESSING_ERROR


def test_reject_error_without_error_code():
    d = kw_error(); d["error_code"] = None
    _rejects(d)


def test_reject_error_with_final_action_or_provenance():
    d = kw_error(); d["final_action"] = "monitor"; d["disposition_source"] = DPS.HUMAN_REVIEW
    d["disposition_reference"] = "REV1"
    _rejects(d)


def test_reject_error_violating_global_draft_source_coherence():
    d = kw_error(); d["ai_draft_source"] = DS.SYNTHETIC_FIXTURE
    d["ai_draft_reference"] = "OUT1"; d["model_id"] = MODEL             # fixture must have model None
    _rejects(d)


# ── Rule 12: override behavior + queue derivation preserved ──────────────────
def test_override_not_forbidden_by_processing_status():
    # A pending override is allowed even on a NOT_PROCESSED alert; queue still derives
    # NOT_PROCESSED (priority 1 beats a pending override).
    d = kw_not_processed(); d.update(override_status=OS.PENDING, override_request_id="CHG-Z")
    rec = CaseLifecycle(**d)
    assert derive_queue_status(rec) is QS.NOT_PROCESSED


def test_reject_pending_override_without_request_id():
    d = kw_complete_plus_pending_override(); d["override_request_id"] = None
    _rejects(d)


def test_reject_override_request_id_when_status_none():
    d = kw_fail_required_complete(); d["override_request_id"] = "CHG-1"
    _rejects(d)


def test_pending_override_priority_beats_blocked():
    d = kw_mixed_blocked(); d.update(override_status=OS.PENDING, override_request_id="CHG-X")
    assert derive_queue_status(CaseLifecycle(**d)) is QS.AWAITING_MANAGER


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
            assert derive_queue_status(rec) is first


def test_queue_status_is_derived_only_not_a_field():
    field_names = {f.name for f in dataclasses.fields(CaseLifecycle)}
    assert "queue_status" not in field_names
    assert len(field_names) == 19


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
