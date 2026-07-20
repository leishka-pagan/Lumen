"""Safe demo-reset for the Manager Override workflow.

Restores the FOUR baseline-controlled runtime files — pending_overrides.csv,
audit_log.csv, case_lifecycle.csv, and ai_outputs.csv — from their committed
baseline snapshots, atomically (write to a temp file in the destination directory,
then os.replace). Every baseline is validated before anything is touched, and the
reset fails closed on any anomaly: if a single baseline is bad, NOTHING is replaced.
This module never runs Git, never runs scripts/generate_data.py, writes no audit
event, and touches no file other than the four runtime destinations.

The two additional destinations (lifecycle, ai_outputs) and their baselines are
environment-redirectable so tests point them at temp files:
  LUMEN_CASE_LIFECYCLE_CSV / LUMEN_BASELINE_LIFECYCLE
  LUMEN_AI_OUTPUTS_CSV     / LUMEN_BASELINE_AI_OUTPUTS
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DATA_DIR = _PROJECT_ROOT / "data"


class DemoResetError(Exception):
    """Raised when a demo baseline fails validation. Nothing is replaced."""


# The columns the reset validation depends on (the baseline may carry more).
_REQUIRED_COLUMNS = {"change_id", "status", "reviewed_by", "reviewed_at", "review_note"}
_BLANK_FIELDS = ("reviewed_by", "reviewed_at", "review_note")

# The canonical captured-live AI-draft baseline: 93 normalized claims across
# ALERT001..ALERT039, 1..3 per alert, every row declaring the fixed capture model.
_BASELINE_AI_OUTPUT_ROWS = 93
_BASELINE_ALERT_IDS = frozenset(f"ALERT{i:03d}" for i in range(1, 40))
_MIN_CLAIMS_PER_ALERT, _MAX_CLAIMS_PER_ALERT = 1, 3
_BASELINE_DRAFT_SOURCE = "captured_live"
_BASELINE_MODEL_ID = "claude-haiku-4-5-20251001"
_AI_OUTPUT_REQUIRED_COLUMNS = {
    "output_id", "alert_id", "claim_id", "claim_type", "asserted_value",
    "evidence_refs", "generated_at", "draft_source", "draft_reference",
    "model_id", "processing_run_id",
}


def _resolve(explicit, env_var: str, default_rel: str) -> Path:
    """Resolve a path: explicit arg > environment variable > data/ default."""
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get(env_var)
    return Path(env) if env else (_DATA_DIR / default_rel)


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


def validate_baseline_lifecycle(baseline_lifecycle_path) -> None:
    """Fail closed unless the baseline lifecycle is the canonical, fully-valid,
    unique-alert 39-row lifecycle (loaded through the lifecycle store, which
    reconstructs and validates every row)."""
    path = Path(baseline_lifecycle_path)
    if not path.exists():
        raise DemoResetError(f"baseline lifecycle snapshot not found: {path}")
    from src.lifecycle_store import load_lifecycle  # lazy: keep base import light
    try:
        records = load_lifecycle(path)
    except Exception as exc:  # invalid row, dup/missing alert_id, bad schema
        raise DemoResetError(f"baseline lifecycle failed validation: {exc}") from exc
    if len(records) != 39:
        raise DemoResetError(f"baseline lifecycle must contain 39 rows, found {len(records)}")


def validate_baseline_ai_outputs(baseline_ai_outputs_path) -> None:
    """Fail closed unless the baseline AI outputs are the canonical captured-live set.

    The canonical dataset is the 93 normalized claims captured for ALERT001..ALERT039
    (1..3 per alert), every row declaring captured_live provenance and the fixed capture
    model. Validation reads ONLY the committed baseline snapshot — never an external
    capture directory — so a reset can never depend on anything outside the repository.
    """
    path = Path(baseline_ai_outputs_path)
    if not path.exists():
        raise DemoResetError(f"baseline ai_outputs snapshot not found: {path}")
    with path.open("r", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    if len(rows) != _BASELINE_AI_OUTPUT_ROWS:
        raise DemoResetError(
            f"baseline ai_outputs must contain {_BASELINE_AI_OUTPUT_ROWS} rows, found {len(rows)}")
    missing = _AI_OUTPUT_REQUIRED_COLUMNS - set(rows[0].keys())
    if missing:
        raise DemoResetError(f"baseline ai_outputs missing columns: {sorted(missing)}")

    per_alert: dict[str, int] = {}
    output_ids: set[str] = set()
    claim_ids: set[str] = set()
    for r in rows:
        output_id = (r.get("output_id") or "").strip()
        claim_id = (r.get("claim_id") or "").strip()
        alert_id = (r.get("alert_id") or "").strip()

        for field in ("output_id", "alert_id", "claim_id", "claim_type",
                      "asserted_value", "generated_at", "draft_reference"):
            if not (r.get(field) or "").strip():
                raise DemoResetError(
                    f"baseline ai_outputs row {output_id or '<unknown>'!r} has an empty {field}")
        if (r.get("draft_source") or "") != _BASELINE_DRAFT_SOURCE:
            raise DemoResetError(
                f"baseline ai_outputs row {output_id!r} is not {_BASELINE_DRAFT_SOURCE}")
        if (r.get("model_id") or "") != _BASELINE_MODEL_ID:
            raise DemoResetError(
                f"baseline ai_outputs row {output_id!r} does not declare model {_BASELINE_MODEL_ID}")
        if output_id in output_ids:
            raise DemoResetError(f"baseline ai_outputs has a duplicate output_id {output_id!r}")
        if claim_id in claim_ids:
            raise DemoResetError(f"baseline ai_outputs has a duplicate claim_id {claim_id!r}")
        output_ids.add(output_id)
        claim_ids.add(claim_id)

        # Evidence references must still parse as a JSON list of nonempty strings.
        try:
            refs = json.loads(r.get("evidence_refs") or "[]")
        except ValueError:
            raise DemoResetError(
                f"baseline ai_outputs row {output_id!r} has unparseable evidence_refs") from None
        if not isinstance(refs, list) or any(not isinstance(x, str) or not x.strip() for x in refs):
            raise DemoResetError(
                f"baseline ai_outputs row {output_id!r} has malformed evidence_refs")

        per_alert[alert_id] = per_alert.get(alert_id, 0) + 1

    if set(per_alert) != _BASELINE_ALERT_IDS:
        missing_alerts = sorted(_BASELINE_ALERT_IDS - set(per_alert))
        unexpected = sorted(set(per_alert) - _BASELINE_ALERT_IDS)
        raise DemoResetError(
            f"baseline ai_outputs alert coverage is wrong "
            f"(missing={missing_alerts[:5]}, unexpected={unexpected[:5]})")
    for alert_id, count in sorted(per_alert.items()):
        if not (_MIN_CLAIMS_PER_ALERT <= count <= _MAX_CLAIMS_PER_ALERT):
            raise DemoResetError(
                f"baseline ai_outputs has {count} claims for {alert_id}; "
                f"expected {_MIN_CLAIMS_PER_ALERT}..{_MAX_CLAIMS_PER_ALERT}")


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
                        baseline_pending_path, baseline_audit_path,
                        lifecycle_path=None, baseline_lifecycle_path=None,
                        ai_outputs_path=None, baseline_ai_outputs_path=None) -> dict:
    """Restore the four baseline-controlled runtime files — pending_overrides.csv,
    audit_log.csv, case_lifecycle.csv, and ai_outputs.csv — from their committed
    baseline snapshots.

    The two lifecycle/ai_outputs destinations and baselines are environment-redirectable
    (see module docstring) and default to data/ when neither an explicit path nor an env
    var is given, so the caller (the app) needs no new arguments.

    ALL baselines are validated FIRST; on any failure this raises DemoResetError and
    replaces nothing. Each replacement is atomic (temp file + os.replace). No Git, no
    dataset generation, no audit-event write, and no file other than the four runtime
    destinations is touched. Returns a small summary dict.
    """
    lifecycle_path = _resolve(lifecycle_path, "LUMEN_CASE_LIFECYCLE_CSV", "case_lifecycle.csv")
    baseline_lifecycle_path = _resolve(
        baseline_lifecycle_path, "LUMEN_BASELINE_LIFECYCLE", "demo_baseline/case_lifecycle.csv")
    ai_outputs_path = _resolve(ai_outputs_path, "LUMEN_AI_OUTPUTS_CSV", "ai_outputs.csv")
    baseline_ai_outputs_path = _resolve(
        baseline_ai_outputs_path, "LUMEN_BASELINE_AI_OUTPUTS", "demo_baseline/ai_outputs.csv")

    baseline_audit = Path(baseline_audit_path)
    if not baseline_audit.exists():
        raise DemoResetError(f"baseline audit snapshot not found: {baseline_audit}")

    # Validate EVERY baseline before writing anything — fail closed, all-or-nothing.
    validate_baseline_pending(baseline_pending_path)
    validate_baseline_lifecycle(baseline_lifecycle_path)
    validate_baseline_ai_outputs(baseline_ai_outputs_path)

    _atomic_replace(baseline_pending_path, pending_path)
    _atomic_replace(baseline_audit, audit_path)
    _atomic_replace(baseline_lifecycle_path, lifecycle_path)
    _atomic_replace(baseline_ai_outputs_path, ai_outputs_path)
    return {
        "pending_restored": str(Path(pending_path)),
        "audit_restored": str(Path(audit_path)),
        "lifecycle_restored": str(Path(lifecycle_path)),
        "ai_outputs_restored": str(Path(ai_outputs_path)),
        "pending_count": 3,
    }
