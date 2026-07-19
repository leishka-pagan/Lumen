"""Safe demo-reset for the Manager Override workflow.

Restores ONLY the two runtime files — data/pending_overrides.csv and
data/audit_log.csv — from committed baseline snapshots, atomically (write to a
temp file in the destination directory, then os.replace). The baseline pending
file is validated before anything is touched, and the reset fails closed on any
anomaly. This module never runs Git, never runs scripts/generate_data.py, writes
no audit event, and touches no other data file.
"""

from __future__ import annotations

import csv
import os
import shutil
import tempfile
from pathlib import Path


class DemoResetError(Exception):
    """Raised when the demo baseline fails validation. Nothing is replaced."""


# The columns the reset validation depends on (the baseline may carry more).
_REQUIRED_COLUMNS = {"change_id", "status", "reviewed_by", "reviewed_at", "review_note"}
_BLANK_FIELDS = ("reviewed_by", "reviewed_at", "review_note")


def validate_baseline_pending(baseline_pending_path) -> None:
    """Fail closed unless the baseline pending snapshot is exactly the canonical
    three pending requests with blank reviewer / reviewed_at / review_note."""
    path = Path(baseline_pending_path)
    if not path.exists():
        raise DemoResetError(f"baseline pending snapshot not found: {path}")
    with path.open("r", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if len(rows) != 3:
        raise DemoResetError(f"baseline must contain exactly 3 rows, found {len(rows)}")
    missing = _REQUIRED_COLUMNS - set(rows[0].keys())
    if missing:
        raise DemoResetError(f"baseline missing required columns: {sorted(missing)}")
    for r in rows:
        if (r.get("status") or "") != "pending":
            raise DemoResetError(f"baseline row {r.get('change_id')!r} is not 'pending'")
        for field in _BLANK_FIELDS:
            if (r.get(field) or "").strip() != "":
                raise DemoResetError(f"baseline row {r.get('change_id')!r} has non-blank {field}")


def _atomic_replace(src, dst) -> None:
    """Copy src -> dst atomically: write to a temp file in dst's directory, fsync,
    then os.replace (same-filesystem atomic rename). Cleans up on failure."""
    src = Path(src)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dst.parent), prefix=".demo_reset_", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as out, src.open("rb") as inp:
            shutil.copyfileobj(inp, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, dst)          # atomic on the same filesystem
    except BaseException:
        try:
            os.unlink(tmp)            # the replace never happened; drop the temp file
        except OSError:
            pass
        raise


def reset_override_demo(pending_path, audit_path,
                        baseline_pending_path, baseline_audit_path) -> dict:
    """Restore ONLY pending_overrides.csv and audit_log.csv from their baseline
    snapshots. Validates the baseline pending file first; on failure raises
    DemoResetError and replaces nothing. Returns a small summary dict.

    Guarantees: no Git, no dataset generation, no audit-event write, and no other
    file touched. Each replacement is atomic (temp file + os.replace).
    """
    baseline_audit = Path(baseline_audit_path)
    if not baseline_audit.exists():
        raise DemoResetError(f"baseline audit snapshot not found: {baseline_audit}")
    validate_baseline_pending(baseline_pending_path)   # fail closed BEFORE any write

    _atomic_replace(baseline_pending_path, pending_path)
    _atomic_replace(baseline_audit, audit_path)
    return {
        "pending_restored": str(Path(pending_path)),
        "audit_restored": str(Path(audit_path)),
        "pending_count": 3,
    }
