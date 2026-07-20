"""Guarded, resumable, one-time Anthropic capture runner for ALERT001..ALERT007.

DEFAULT IS DRY-RUN. It makes ZERO live API requests, reads no API key, opens no
network, and writes nothing unless EVERY live-authorization gate passes. A live run
requires all of: LUMEN_CAPTURE_LIVE_AI=1, the confirmation phrase CAPTURE-7-AUTHORIZED,
a present ANTHROPIC_API_KEY, an explicit --live flag (enforced by the CLI), and an
absolute --manifest path and --output-dir that both live OUTSIDE the git repository.

Every limit is imported from src.capture_guardrails and never redefined here. The only
way to spend the budget is src.capture_guardrails.authorize_capture_session, which
enforces the flag/phrase/key gates; a CaptureBudget cannot otherwise be built. Restart
safety is provided by an atomic JSON manifest kept OUTSIDE the repo: a succeeded alert
is never called again, and a pending/failed/malformed/over-budget/wrong-model/
wrong-source manifest halts the whole run. The API is never called twice for one alert.

This module writes no runtime/baseline CSV and no audit event. The MANIFEST and the
returned SUMMARY contain no prompt, key, raw response, headers, or customer data. The
EXTERNAL capture output (written outside the repository) INTENTIONALLY contains
normalized alert IDs, claims, and evidence references, and must therefore be treated as
capture data.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from . import llm_drafter, pipeline
from .capture_guardrails import (
    ALLOWED_ALERTS,
    CAPTURE_MODEL,
    CONCURRENCY,
    EXTENDED_THINKING_ENABLED,
    MAX_ATTEMPTS,
    MAX_CUMULATIVE_INPUT_TOKENS,
    MAX_CUMULATIVE_OUTPUT_TOKENS,
    MAX_OUTPUT_TOKENS_PER_REQUEST,
    MAX_REQUEST_CHARS,
    MAX_RETRIES,
    MODEL_FALLBACK_ENABLED,
    REQUEST_TIMEOUT_SECONDS,
    REQUIRED_CONFIRMATION_PHRASE,
    REQUIRED_ENV_FLAG,
    REQUIRED_ENV_VALUE,
    CapturePolicyError,
    authorize_capture_session,
)

__all__ = [
    "CaptureError",
    "RequestPlan",
    "CAPTURE_ALERTS",
    "MANIFEST_SCHEMA_VERSION",
    "CAPTURE_RUN_ID",
    "repo_root",
    "plan_requests",
    "dry_run",
    "run_capture",
]

MANIFEST_SCHEMA_VERSION = 1
CAPTURE_RUN_ID = "LIVE-CAPTURE-V1"          # processing_run_id stamped on captured output
_API_KEY_ENV = "ANTHROPIC_API_KEY"          # presence only; the value is never read/stored
# Source tables the capture request is built from (the existing drafter/pipeline path).
SOURCE_TABLE_FILES = ("alerts", "customers", "transactions", "prior_cases",
                      "kyc_profile_status", "evidence_items")
CAPTURE_ALERTS: tuple[str, ...] = tuple(sorted(ALLOWED_ALERTS))   # ALERT001..ALERT007


class CaptureError(Exception):
    """A capture-runner failure (bad path, missing gate, manifest/resume conflict).

    Intentionally non-sensitive: it never carries an API key, a prompt, a raw
    request/response, a customer datum, or provider exception text.
    """


# ── repo / path safety ───────────────────────────────────────────────────────
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _is_inside_repo(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _require_external_absolute_path(raw: Any, kind: str, root: Path) -> Path:
    """A live path must be a non-empty absolute string OUTSIDE the repository."""
    if not raw or not isinstance(raw, str):
        raise CaptureError(f"{kind} path is required")
    p = Path(raw)
    if not p.is_absolute():
        raise CaptureError(f"{kind} path must be absolute; relative paths are refused")
    if _is_inside_repo(p, root):
        raise CaptureError(f"{kind} path must be OUTSIDE the git repository")
    return p


# ── request planning (pure; no client, no network, no key) ──────────────────
@dataclass(frozen=True)
class RequestPlan:
    alert_id: str
    request_chars: int
    estimated_input_tokens: int
    requested_output_tokens: int
    model: str


def _estimate_input_tokens(request_chars: int) -> int:
    """Conservative LOCAL estimate — no network token endpoint. ~3.5 chars/token, ceil,
    minimum 1. Deliberately over-estimates so pre-flight budgeting never under-counts."""
    return max(1, math.ceil(request_chars / 3.5))


def _request_char_count(alert: dict, source_tables: dict) -> int:
    """Serialized size of the exact request payload the provider would receive."""
    return len(json.dumps(llm_drafter.build_capture_request(alert, source_tables),
                          sort_keys=True, default=str))


def plan_requests(project_root: Path | None = None) -> list[RequestPlan]:
    """Plan the seven allowlisted requests from the existing source-data/prompt path.

    Enforces the per-request character cap BEFORE any authorization. Pure: reads source
    CSVs read-only, prints nothing, opens no client/network, reads no key.
    """
    plans: list[RequestPlan] = []
    for alert_id in CAPTURE_ALERTS:
        alert, source_tables = pipeline._build_live_inputs(alert_id)
        if alert is None:
            raise CaptureError(f"alert {alert_id} not found in source data")
        request_chars = _request_char_count(alert, source_tables)
        if request_chars > MAX_REQUEST_CHARS:
            raise CaptureError(
                f"alert {alert_id}: serialized request {request_chars} exceeds the "
                f"per-request character cap {MAX_REQUEST_CHARS}"
            )
        plans.append(RequestPlan(
            alert_id=alert_id,
            request_chars=request_chars,
            estimated_input_tokens=_estimate_input_tokens(request_chars),
            requested_output_tokens=MAX_OUTPUT_TOKENS_PER_REQUEST,
            model=CAPTURE_MODEL,
        ))
    return plans


def dry_run(project_root: Path | None = None, print_fn: Callable[[str], None] = print) -> bool:
    """Plan + print the seven requests and confirm they fit the guardrails.

    Prints ONLY: alert id, request-char count, estimated input tokens, requested output
    limit, the fixed model, and the aggregate caps — never prompt text or customer data.
    Reads no API key, writes no manifest/output, opens no network. Returns True only when
    all seven requests fit every guardrail.
    """
    plans = plan_requests(project_root)
    total_est_input = sum(p.estimated_input_tokens for p in plans)
    total_req_output = sum(p.requested_output_tokens for p in plans)

    print_fn("DRY RUN - no API key read, no network, no writes.")
    print_fn(f"Fixed model: {CAPTURE_MODEL}")
    print_fn(f"Allowlist: {', '.join(CAPTURE_ALERTS)}")
    for p in plans:
        print_fn(
            f"  {p.alert_id}: request_chars={p.request_chars} "
            f"est_input_tokens={p.estimated_input_tokens} "
            f"requested_output_tokens={p.requested_output_tokens} model={p.model}"
        )
    print_fn(
        f"Aggregate: attempts={len(plans)}/{MAX_ATTEMPTS} "
        f"est_input_total={total_est_input}/{MAX_CUMULATIVE_INPUT_TOKENS} "
        f"requested_output_total={total_req_output}/{MAX_CUMULATIVE_OUTPUT_TOKENS} "
        f"concurrency={CONCURRENCY} timeout={REQUEST_TIMEOUT_SECONDS}s retries={MAX_RETRIES} "
        f"extended_thinking={EXTENDED_THINKING_ENABLED} fallback={MODEL_FALLBACK_ENABLED}"
    )

    fits = (
        len(plans) == len(CAPTURE_ALERTS)
        and len(plans) <= MAX_ATTEMPTS
        and all(p.request_chars <= MAX_REQUEST_CHARS for p in plans)
        and all(p.requested_output_tokens <= MAX_OUTPUT_TOKENS_PER_REQUEST for p in plans)
        and total_est_input <= MAX_CUMULATIVE_INPUT_TOKENS
        and total_req_output <= MAX_CUMULATIVE_OUTPUT_TOKENS
    )
    print_fn("RESULT: all seven requests fit the guardrails." if fits
             else "RESULT: requests do NOT fit the guardrails.")
    return fits


# ── source provenance (local; no network) ───────────────────────────────────
def _source_data_hash(project_root: Path) -> str:
    h = hashlib.sha256()
    data_dir = project_root / "data"
    for name in sorted(SOURCE_TABLE_FILES):
        h.update(name.encode("utf-8"))
        h.update(b"\0")
        h.update((data_dir / f"{name}.csv").read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def _git_commit(project_root: Path) -> str:
    """Read the current commit from .git (no subprocess, no network). 'unknown' on any
    failure — a value only used for the manifest's source-provenance guard."""
    git = project_root / ".git"
    try:
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            ref = head.split(":", 1)[1].strip()
            loose = git / ref
            if loose.exists():
                return loose.read_text(encoding="utf-8").strip()
            packed = git / "packed-refs"
            if packed.exists():
                for line in packed.read_text(encoding="utf-8").splitlines():
                    if line and not line.startswith(("#", "^")) and " " in line:
                        sha, name = line.split(" ", 1)
                        if name.strip() == ref:
                            return sha.strip()
            return "unknown"
        return head
    except OSError:
        return "unknown"


def _limits_block() -> dict:
    return {
        "model": CAPTURE_MODEL,
        "max_attempts": MAX_ATTEMPTS,
        "max_output_tokens_per_request": MAX_OUTPUT_TOKENS_PER_REQUEST,
        "max_cumulative_input_tokens": MAX_CUMULATIVE_INPUT_TOKENS,
        "max_cumulative_output_tokens": MAX_CUMULATIVE_OUTPUT_TOKENS,
        "max_request_chars": MAX_REQUEST_CHARS,
        "timeout_seconds": REQUEST_TIMEOUT_SECONDS,
        "max_retries": MAX_RETRIES,
        "concurrency": CONCURRENCY,
        "extended_thinking_enabled": EXTENDED_THINKING_ENABLED,
        "model_fallback_enabled": MODEL_FALLBACK_ENABLED,
    }


# ── atomic writers ───────────────────────────────────────────────────────────
def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".capture_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_manifest(path: Path, manifest: dict) -> None:
    _atomic_write_text(path, json.dumps(manifest, sort_keys=True, indent=2))


# ── manifest lifecycle ───────────────────────────────────────────────────────
_VALID_STATUS = ("planned", "pending", "succeeded", "failed")


def _new_manifest(plans: list[RequestPlan], source_hash: str, git_commit: str, started_at: str) -> dict:
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "model": CAPTURE_MODEL,
        "limits": _limits_block(),
        "source_data_hash": source_hash,
        "source_git_commit": git_commit,
        "started_at": started_at,
        "completed_at": None,
        "alerts": {
            p.alert_id: {
                "status": "planned",
                "request_chars": p.request_chars,
                "estimated_input_tokens": p.estimated_input_tokens,
                "actual_input_tokens": None,
                "actual_output_tokens": None,
                "output_filename": None,
                "output_sha256": None,
                "failure_code": None,
            }
            for p in plans
        },
    }


def _load_manifest(path: Path, source_hash: str, git_commit: str) -> dict:
    """Load + validate an existing manifest. Halt (CaptureError) on malformed JSON,
    schema/model mismatch, or a source hash / git commit that differs from now."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise CaptureError("manifest is malformed (unreadable or not valid JSON)")
    if not isinstance(data, dict):
        raise CaptureError("manifest is malformed (not an object)")
    if data.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise CaptureError("manifest schema-version mismatch — halting")
    if data.get("model") != CAPTURE_MODEL:
        raise CaptureError("manifest is from a different model — halting")
    if data.get("source_data_hash") != source_hash:
        raise CaptureError("manifest source-data hash mismatch — halting")
    if data.get("source_git_commit") != git_commit:
        raise CaptureError("manifest source git-commit mismatch — halting")
    alerts = data.get("alerts")
    if not isinstance(alerts, dict) or set(alerts) != set(CAPTURE_ALERTS):
        raise CaptureError("manifest alert set is malformed — halting")
    for alert_id, entry in alerts.items():
        if not isinstance(entry, dict) or entry.get("status") not in _VALID_STATUS:
            raise CaptureError(f"manifest entry for {alert_id} is malformed — halting")
        if entry["status"] == "succeeded":
            for key in ("request_chars", "estimated_input_tokens", "actual_input_tokens",
                        "actual_output_tokens", "output_filename", "output_sha256"):
                if entry.get(key) is None:
                    raise CaptureError(f"succeeded manifest entry {alert_id} is incomplete — halting")
    return data


def _failure_code(exc: BaseException) -> str:
    """A SHORT non-sensitive code — the exception CLASS name only, never its message."""
    return f"error:{type(exc).__name__}"[:64]


def _valid_usage(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _utc_now() -> str:
    """Timezone-aware current UTC timestamp (ISO-8601). Used only in a live run — never
    in dry-run, and overridable via ``now_fn`` for deterministic tests."""
    return datetime.now(timezone.utc).isoformat()


def _preflight_output_dir(output_p: Path) -> None:
    """Prove ``output_dir`` exists, is a directory, and is writable BEFORE any client is
    constructed, any alert is marked pending, or any paid request is issued.

    Creates the directory if needed, confirms it is a directory, then proves writability
    with a UNIQUE probe created inside it (written, flushed, fsynced, removed). Raises
    CaptureError on any failure and always leaves NO probe file behind.
    """
    try:
        output_p.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise CaptureError("output-dir could not be created") from None
    if not output_p.is_dir():
        raise CaptureError("output-dir is not a directory")
    probe: str | None = None
    try:
        fd, probe = tempfile.mkstemp(dir=str(output_p), prefix=".capture_probe_", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("probe")
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        raise CaptureError("output-dir is not writable") from None
    finally:
        if probe is not None:
            try:
                os.unlink(probe)
            except OSError:
                pass


def _default_client_factory():
    """Construct the Anthropic client with the guardrail timeout and ZERO retries.

    Imported lazily and called ONLY after every live gate has passed. No fallback client.
    """
    import anthropic  # lazy: not needed for dry-run or import
    return anthropic.Anthropic(timeout=REQUEST_TIMEOUT_SECONDS, max_retries=MAX_RETRIES)


def run_capture(
    *,
    env: Mapping[str, str],
    confirmation_phrase: str,
    manifest_path: str,
    output_dir: str,
    project_root: Path | None = None,
    client_factory: Callable[[], Any] | None = None,
    now_fn: Callable[[], str] | None = None,
) -> dict:
    """Run (or resume) the guarded live capture. Every gate is checked BEFORE the API
    key is read and BEFORE any client is constructed. Returns a small non-sensitive
    summary. See the module docstring for the full contract.
    """
    root = repo_root() if project_root is None else Path(project_root)
    now = now_fn if now_fn is not None else _utc_now   # real UTC unless a test injects one

    # ── Gates that never touch the API key ───────────────────────────────────
    manifest_p = _require_external_absolute_path(manifest_path, "manifest", root)
    output_p = _require_external_absolute_path(output_dir, "output-dir", root)
    if str(env.get(REQUIRED_ENV_FLAG, "")) != REQUIRED_ENV_VALUE:
        raise CaptureError(
            f"live capture denied: {REQUIRED_ENV_FLAG} must equal {REQUIRED_ENV_VALUE!r}"
        )
    if confirmation_phrase != REQUIRED_CONFIRMATION_PHRASE:
        raise CaptureError("live capture denied: confirmation phrase does not match")

    # ── API-key PRESENCE — read only now that every other gate has passed ────
    api_key_present = bool(env.get(_API_KEY_ENV))     # truthiness only; value never stored
    # Authoritative gate: the ONLY way to a spendable budget.
    budget = authorize_capture_session(env, confirmation_phrase, api_key_present)

    # Output-directory preflight — AFTER every gate, but BEFORE constructing a client,
    # marking any alert pending, initializing the manifest, or issuing any paid request.
    _preflight_output_dir(output_p)

    # Plan + provenance (still no client, no network).
    plans = plan_requests(root)
    plan_by_id = {p.alert_id: p for p in plans}
    source_hash = _source_data_hash(root)
    git_commit = _git_commit(root)

    # Manifest: load+validate (resume) or initialize.
    if manifest_p.exists():
        manifest = _load_manifest(manifest_p, source_hash, git_commit)
    else:
        manifest = _new_manifest(plans, source_hash, git_commit, now())
        _write_manifest(manifest_p, manifest)

    # ── Resume: replay succeeded through the fresh budget; halt on pending/failed. ──
    for alert_id in CAPTURE_ALERTS:
        entry = manifest["alerts"][alert_id]
        status = entry["status"]
        if status == "succeeded":
            # Verify the stored output is intact (hash matches) before trusting it.
            out_file = output_p / str(entry["output_filename"])
            try:
                stored = out_file.read_text(encoding="utf-8")
            except OSError:
                raise CaptureError(f"resume halted: output for {alert_id} is missing")
            if hashlib.sha256(stored.encode("utf-8")).hexdigest() != entry["output_sha256"]:
                raise CaptureError(f"resume halted: output hash mismatch for {alert_id}")
            try:
                budget.authorize_attempt(
                    alert_id=alert_id, model=CAPTURE_MODEL,
                    request_chars=entry["request_chars"],
                    requested_output_tokens=MAX_OUTPUT_TOKENS_PER_REQUEST,
                    estimated_input_tokens=entry["estimated_input_tokens"],
                )
                budget.record_usage(
                    alert_id=alert_id,
                    input_tokens=entry["actual_input_tokens"],
                    output_tokens=entry["actual_output_tokens"],
                )
            except Exception as exc:
                raise CaptureError(f"resume halted: succeeded entry {alert_id} is over budget") from None
        elif status == "pending":
            raise CaptureError(
                f"resume halted: alert {alert_id} is pending — its billing is unknown; "
                "a pending request must never be assumed unbilled"
            )
        elif status == "failed":
            raise CaptureError(f"resume halted: alert {alert_id} previously failed; no automatic retry")

    # ── Process alerts never attempted (planned). ────────────────────────────
    client: Any = None
    for alert_id in CAPTURE_ALERTS:
        entry = manifest["alerts"][alert_id]
        if entry["status"] != "planned":
            continue
        if budget.is_halted:
            break
        plan = plan_by_id[alert_id]

        # 1. Authorize the attempt through the budget (marks it pending in the budget).
        budget.authorize_attempt(
            alert_id=alert_id, model=CAPTURE_MODEL,
            request_chars=plan.request_chars,
            requested_output_tokens=plan.requested_output_tokens,
            estimated_input_tokens=plan.estimated_input_tokens,
        )
        # 2. Record pending in the manifest ATOMICALLY, BEFORE issuing the request.
        entry["status"] = "pending"
        _write_manifest(manifest_p, manifest)

        # Construct the client lazily on first real need (after all gates).
        if client is None:
            factory = client_factory if client_factory is not None else _default_client_factory
            client = factory()

        # 3. Issue exactly one request.
        alert, source_tables = pipeline._build_live_inputs(alert_id)
        try:
            claims, in_tok, out_tok = llm_drafter.draft_claims_for_capture(alert, source_tables, client)
            if not _valid_usage(in_tok) or not _valid_usage(out_tok):
                raise CaptureError("provider response missing usage")
        except BaseException as exc:  # any failure halts the whole capture
            code = _failure_code(exc)
            budget.record_failure(alert_id)         # consume the attempt, halt the session
            entry.update(status="failed", failure_code=code)
            _write_manifest(manifest_p, manifest)
            raise CaptureError(f"capture halted: alert {alert_id} failed ({code})") from None

        # 4a. Success: record ACTUAL usage. An overage records the actuals inside
        # record_usage and halts the session (raising); a reached-cap halts silently.
        # Either way the actuals are recorded; we persist the received response and the
        # loop's top-of-iteration halt check then stops any further alerts.
        try:
            budget.record_usage(alert_id=alert_id, input_tokens=in_tok, output_tokens=out_tok)
        except CapturePolicyError:
            pass  # actual usage already recorded; budget is now halted
        output_obj = {
            "alert_id": alert_id,
            "model": CAPTURE_MODEL,
            "processing_run_id": CAPTURE_RUN_ID,
            "captured_at": now(),
            "claims": claims,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
        }
        filename = f"{alert_id}.capture.json"
        text = json.dumps(output_obj, sort_keys=True, indent=2)
        _atomic_write_text(output_p / filename, text)
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        output_obj_sha = sha
        entry.update(
            status="succeeded",
            actual_input_tokens=in_tok,
            actual_output_tokens=out_tok,
            output_filename=filename,
            output_sha256=output_obj_sha,
        )
        _write_manifest(manifest_p, manifest)

    if all(e["status"] == "succeeded" for e in manifest["alerts"].values()):
        manifest["completed_at"] = now()
        _write_manifest(manifest_p, manifest)

    s = budget.summary()
    return {
        "attempts": s.attempt_count,
        "succeeded": sum(1 for e in manifest["alerts"].values() if e["status"] == "succeeded"),
        "halted": s.is_halted,
        "cumulative_input_tokens": s.cumulative_input_tokens,
        "cumulative_output_tokens": s.cumulative_output_tokens,
        "manifest_path": str(manifest_p),
    }
