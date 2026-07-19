"""Tests for the environment-redirectable, atomic lifecycle store (src/lifecycle_store).

Covers load/validate/reject, atomic env-redirected writes, and the focused
apply_override_decision update — including the five manager demo scenarios, manager
disposition provenance, non-disposition/rejection preservation, request-id mismatch
and lifecycle-validation failures writing nothing, read-only load, determinism, and
no network access.

Isolation: every test operates on a temp copy of the committed lifecycle; the committed
data/case_lifecycle.csv is never mutated.
"""

from __future__ import annotations

import shutil
import socket
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT))

from src import lifecycle_store as store  # noqa: E402
from src.case_lifecycle import (  # noqa: E402
    CaseLifecycle, DispositionSource, LifecycleInvariantError, OverrideStatus,
    QueueStatus, derive_queue_status,
)
from src.lifecycle_store import LifecycleStoreError  # noqa: E402

COMMITTED = DATA / "case_lifecycle.csv"


@pytest.fixture
def lc(tmp_path):
    """A temp copy of the committed lifecycle CSV (never mutate the committed file)."""
    path = tmp_path / "case_lifecycle.csv"
    shutil.copy(COMMITTED, path)
    return path


# ── Load / validate ──────────────────────────────────────────────────────────
def test_load_returns_39_validated_records(lc):
    records = store.load_lifecycle(lc)
    assert len(records) == 39
    assert all(isinstance(r, CaseLifecycle) for r in records)
    assert len({r.alert_id for r in records}) == 39


def test_load_is_read_only(lc):
    before = lc.read_bytes()
    store.load_lifecycle(lc)
    assert lc.read_bytes() == before          # loading never writes


def test_load_respects_env_redirect(lc, monkeypatch):
    monkeypatch.setenv("LUMEN_CASE_LIFECYCLE_CSV", str(lc))
    assert store.lifecycle_path() == lc
    assert len(store.load_lifecycle()) == 39   # no explicit path -> env


def test_load_rejects_duplicate_alert_id(lc):
    df = pd.read_csv(lc, dtype=str, keep_default_na=False)
    df.loc[len(df)] = df.iloc[0]               # duplicate the first alert
    df.to_csv(lc, index=False)
    with pytest.raises(LifecycleStoreError):
        store.load_lifecycle(lc)


def test_load_rejects_missing_alert_id(lc):
    df = pd.read_csv(lc, dtype=str, keep_default_na=False)
    df.loc[0, "alert_id"] = ""
    df.to_csv(lc, index=False)
    with pytest.raises((LifecycleStoreError, LifecycleInvariantError)):
        store.load_lifecycle(lc)


# ── Write: atomic, canonical order, row order preserved ──────────────────────
def test_write_preserves_column_and_row_order(lc):
    records = store.load_lifecycle(lc)
    store.write_lifecycle(records, lc)
    df = pd.read_csv(lc, dtype=str, keep_default_na=False)
    assert list(df.columns) == store.LIFECYCLE_COLUMNS
    assert list(df["alert_id"]) == [r.alert_id for r in records]


def test_write_leaves_no_temp_file(lc):
    store.write_lifecycle(store.load_lifecycle(lc), lc)
    assert not (lc.parent / (lc.name + ".tmp")).exists()   # atomic replace cleaned up


# ── apply_override_decision: the five demo scenarios ─────────────────────────
def _apply(lc, **kw):
    return store.apply_override_decision(path=lc, **kw)


def test_approve_severity_preserves_system_disposition_closed(lc):
    rec = _apply(lc, alert_id="ALERT002", change_id="CHG-SEED-001",
                 decision="approved", field_changed="severity", new_value="med")
    assert rec.override_status is OverrideStatus.APPROVED
    assert rec.final_action == "monitor"                          # system action preserved
    assert rec.disposition_source is DispositionSource.SYSTEM_POLICY
    assert derive_queue_status(rec) is QueueStatus.CLOSED


def test_reject_severity_preserves_system_disposition_closed(lc):
    rec = _apply(lc, alert_id="ALERT002", change_id="CHG-SEED-001",
                 decision="rejected", field_changed="severity", new_value="med")
    assert rec.override_status is OverrideStatus.REJECTED
    assert rec.final_action == "monitor"
    assert rec.disposition_source is DispositionSource.SYSTEM_POLICY
    assert derive_queue_status(rec) is QueueStatus.CLOSED


def test_approve_disposition_sets_manager_override_provenance_closed(lc):
    rec = _apply(lc, alert_id="ALERT005", change_id="CHG-SEED-002",
                 decision="approved", field_changed="disposition", new_value="escalate")
    assert rec.override_status is OverrideStatus.APPROVED
    assert rec.final_action == "escalate"                        # normalized new value
    assert rec.disposition_source is DispositionSource.MANAGER_OVERRIDE
    assert rec.disposition_reference == "CHG-SEED-002"           # provenance = request id
    assert derive_queue_status(rec) is QueueStatus.CLOSED


def test_reject_disposition_preserves_original_system_action_closed(lc):
    rec = _apply(lc, alert_id="ALERT005", change_id="CHG-SEED-002",
                 decision="rejected", field_changed="disposition", new_value="escalate")
    assert rec.override_status is OverrideStatus.REJECTED
    assert rec.final_action == "monitor"                         # original system action
    assert rec.disposition_source is DispositionSource.SYSTEM_POLICY
    assert derive_queue_status(rec) is QueueStatus.CLOSED


@pytest.mark.parametrize("decision", ["approved", "rejected"])
def test_risk_rating_override_preserves_human_review_provenance_closed(lc, decision):
    rec = _apply(lc, alert_id="ALERT001", change_id="CHG-SEED-003",
                 decision=decision, field_changed="risk_rating", new_value="medium")
    assert rec.override_status is (OverrideStatus.APPROVED if decision == "approved"
                                   else OverrideStatus.REJECTED)
    assert rec.final_action == "monitor"                         # human-review action kept
    assert rec.disposition_source is DispositionSource.HUMAN_REVIEW
    assert rec.disposition_reference == "REV003"
    assert derive_queue_status(rec) is QueueStatus.CLOSED


def test_manager_override_normalizes_new_value(lc):
    rec = _apply(lc, alert_id="ALERT005", change_id="CHG-SEED-002",
                 decision="approved", field_changed="disposition", new_value="  ESCALATE  ")
    assert rec.final_action == "escalate"


# ── apply_override_decision: failures write nothing ──────────────────────────
def test_request_id_mismatch_writes_nothing(lc):
    before = lc.read_bytes()
    with pytest.raises(LifecycleStoreError):
        _apply(lc, alert_id="ALERT002", change_id="CHG-WRONG",
               decision="approved", field_changed="severity", new_value="med")
    assert lc.read_bytes() == before


def test_unknown_alert_writes_nothing(lc):
    before = lc.read_bytes()
    with pytest.raises(LifecycleStoreError):
        _apply(lc, alert_id="ALERT404", change_id="CHG-SEED-001",
               decision="approved", field_changed="severity", new_value="med")
    assert lc.read_bytes() == before


def test_unknown_decision_writes_nothing(lc):
    before = lc.read_bytes()
    with pytest.raises(LifecycleStoreError):
        _apply(lc, alert_id="ALERT002", change_id="CHG-SEED-001",
               decision="maybe", field_changed="severity", new_value="med")
    assert lc.read_bytes() == before


def test_lifecycle_validation_failure_writes_nothing(lc):
    # Approving a *disposition* override on a REQUIRED/COMPLETE record would move its
    # provenance to MANAGER_OVERRIDE, which that state forbids -> invariant failure.
    before = lc.read_bytes()
    with pytest.raises(LifecycleInvariantError):
        _apply(lc, alert_id="ALERT001", change_id="CHG-SEED-003",
               decision="approved", field_changed="disposition", new_value="escalate")
    assert lc.read_bytes() == before                             # nothing persisted


# ── Determinism, purity ──────────────────────────────────────────────────────
def test_apply_is_deterministic(tmp_path):
    a = tmp_path / "a.csv"
    b = tmp_path / "b.csv"
    shutil.copy(COMMITTED, a)
    shutil.copy(COMMITTED, b)
    _apply(a, alert_id="ALERT005", change_id="CHG-SEED-002",
           decision="approved", field_changed="disposition", new_value="escalate")
    _apply(b, alert_id="ALERT005", change_id="CHG-SEED-002",
           decision="approved", field_changed="disposition", new_value="escalate")
    assert a.read_bytes() == b.read_bytes()


def test_apply_makes_no_network_call(lc, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("lifecycle store attempted network access")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    rec = _apply(lc, alert_id="ALERT005", change_id="CHG-SEED-002",
                 decision="approved", field_changed="disposition", new_value="escalate")
    assert rec.disposition_source is DispositionSource.MANAGER_OVERRIDE


def test_committed_lifecycle_untouched_by_this_module():
    # Sanity: nothing in this suite writes the committed file.
    assert (DATA / "case_lifecycle.csv").exists()
    records = store.load_lifecycle(COMMITTED)      # explicit committed path, read-only
    assert len(records) == 39
