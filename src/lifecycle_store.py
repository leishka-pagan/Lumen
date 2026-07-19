"""Environment-redirectable, atomic persistence store for the canonical lifecycle.

Loads and validates ``data/case_lifecycle.csv`` (redirectable via
``LUMEN_CASE_LIFECYCLE_CSV``), reconstructing every row as a validated
``CaseLifecycle``, and exposes ONE focused, atomic update: applying a single manager
override decision to the lifecycle record for an alert.

Contract:
  * load: read all rows, deserialize enums explicitly, normalize blank optional
    fields to None, reconstruct+validate each as CaseLifecycle, reject duplicate or
    missing alert IDs, preserve column and row order. Never writes.
  * write: temp file in the destination directory + atomic os.replace. Canonical
    column order (dataclass field order); unrelated rows keep their position.
  * apply_override_decision: locate the record by alert_id, require its
    override_request_id to match the request being decided, build the fully updated
    record, VALIDATE it, and only then persist. On any failure it raises and writes
    nothing.

Pure persistence: no network, no data generation, and no write during load.
"""

from __future__ import annotations

import dataclasses
import os
from enum import Enum
from pathlib import Path

import pandas as pd

from src.case_lifecycle import (
    AIDraftSource, AIVerificationStatus, CaseLifecycle, DispositionSource,
    HumanReviewDecision, OverrideStatus, ProcessingStatus, ReviewGateStatus,
    ReviewRoutingStatus,
)

__all__ = [
    "LifecycleStoreError",
    "lifecycle_path",
    "load_lifecycle",
    "write_lifecycle",
    "apply_override_decision",
    "LIFECYCLE_COLUMNS",
]

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LIFECYCLE_CSV = _PROJECT_ROOT / "data" / "case_lifecycle.csv"

# Canonical column order == CaseLifecycle dataclass field order (queue_status is derived
# and never persisted).
LIFECYCLE_COLUMNS = [f.name for f in dataclasses.fields(CaseLifecycle)]

# The eight enum-typed fields and their classes, for explicit deserialization.
_ENUM_FIELDS = {
    "processing_status": ProcessingStatus,
    "ai_draft_source": AIDraftSource,
    "ai_verification": AIVerificationStatus,
    "review_routing": ReviewRoutingStatus,
    "review_gate": ReviewGateStatus,
    "human_review_decision": HumanReviewDecision,
    "override_status": OverrideStatus,
    "disposition_source": DispositionSource,
}


class LifecycleStoreError(Exception):
    """Raised on a store-level failure (missing columns, duplicate/missing alert_id,
    unknown decision, or an override-request mismatch). Domain-invariant violations
    raise CaseLifecycle's own ``LifecycleInvariantError``."""


def lifecycle_path(path=None) -> Path:
    """Resolve the lifecycle CSV path: explicit arg > LUMEN_CASE_LIFECYCLE_CSV > default."""
    if path is not None:
        return Path(path)
    return Path(os.environ.get("LUMEN_CASE_LIFECYCLE_CSV") or DEFAULT_LIFECYCLE_CSV)


def _row_to_record(row: dict) -> CaseLifecycle:
    """Reconstruct one CaseLifecycle from a serialized CSV row: enum-value strings ->
    members, blank optional fields -> None. Construction runs __post_init__, so the
    record is validated here."""
    kwargs = {}
    for name in LIFECYCLE_COLUMNS:
        value = row[name]
        if name in _ENUM_FIELDS:
            kwargs[name] = _ENUM_FIELDS[name](value)   # explicit, fails loudly on unknown
        else:
            kwargs[name] = value if value != "" else None
    return CaseLifecycle(**kwargs)


def load_lifecycle(path=None) -> list[CaseLifecycle]:
    """Load, validate, and return all lifecycle records in file order. Never writes."""
    p = lifecycle_path(path)
    df = pd.read_csv(p, dtype=str, keep_default_na=False)
    missing_cols = [c for c in LIFECYCLE_COLUMNS if c not in df.columns]
    if missing_cols:
        raise LifecycleStoreError(f"case_lifecycle is missing columns: {missing_cols}")

    records: list[CaseLifecycle] = []
    seen: set[str] = set()
    for _, raw in df.iterrows():
        row = raw.to_dict()
        alert_id = row.get("alert_id")
        if alert_id is None or str(alert_id).strip() == "":
            raise LifecycleStoreError("lifecycle row with a missing alert_id")
        if alert_id in seen:
            raise LifecycleStoreError(f"duplicate alert_id in lifecycle: {alert_id!r}")
        seen.add(alert_id)
        records.append(_row_to_record(row))
    return records


def _serialize(record: CaseLifecycle) -> dict:
    """One CaseLifecycle -> a serialized row: enum -> lowercase .value, None -> ''."""
    row = {}
    for field in dataclasses.fields(record):
        value = getattr(record, field.name)
        if value is None:
            row[field.name] = ""
        elif isinstance(value, Enum):
            row[field.name] = value.value
        else:
            row[field.name] = value
    return row


def write_lifecycle(records, path=None) -> None:
    """Persist records in canonical column order via a temp file + atomic os.replace.
    Row order is exactly the order given (callers preserve unrelated rows)."""
    p = lifecycle_path(path)
    df = pd.DataFrame([_serialize(r) for r in records], columns=LIFECYCLE_COLUMNS)
    text = df.to_csv(index=False)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="")
    os.replace(tmp, p)                      # atomic on the same filesystem


def apply_override_decision(*, alert_id, change_id, decision, field_changed,
                            new_value, path=None) -> CaseLifecycle:
    """Apply ONE manager override decision to the lifecycle record for ``alert_id`` and
    persist atomically.

    Rules:
      * decision must be 'approved' or 'rejected';
      * the record for alert_id must exist and its override_request_id must equal
        ``change_id`` (else nothing is written);
      * override_status -> APPROVED / REJECTED;
      * an APPROVED decision whose field is exactly 'disposition' also sets
        final_action = normalized new_value, disposition_source = MANAGER_OVERRIDE,
        disposition_reference = change_id;
      * every other case (non-disposition field, or a rejection) preserves final_action
        and the existing disposition provenance.

    The FULL updated record is validated (CaseLifecycle __post_init__) BEFORE the file
    is written; on any failure this raises and writes nothing. Returns the updated
    record.
    """
    if decision not in ("approved", "rejected"):
        raise LifecycleStoreError(f"unsupported decision {decision!r}")

    records = load_lifecycle(path)
    index = next((i for i, r in enumerate(records) if r.alert_id == alert_id), None)
    if index is None:
        raise LifecycleStoreError(f"no lifecycle record for alert_id {alert_id!r}")
    record = records[index]
    if record.override_request_id != change_id:
        raise LifecycleStoreError(
            f"override request mismatch for {alert_id!r}: record has "
            f"{record.override_request_id!r}, decision is for {change_id!r}"
        )

    updates = {
        "override_status": OverrideStatus.APPROVED if decision == "approved"
        else OverrideStatus.REJECTED
    }
    if decision == "approved" and str(field_changed).strip().lower() == "disposition":
        # Manager overrides the recorded disposition; provenance moves to the request.
        updates["final_action"] = str(new_value).strip().lower()
        updates["disposition_source"] = DispositionSource.MANAGER_OVERRIDE
        updates["disposition_reference"] = change_id
    # else: final_action + existing disposition provenance are preserved unchanged.

    updated = dataclasses.replace(record, **updates)   # __post_init__ validates FIRST
    records[index] = updated
    write_lifecycle(records, path)
    return updated
