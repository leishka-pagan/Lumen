"""One-time DEMO capture of AI drafts for the 32 remaining alerts (ALERT008..ALERT039).

DEFAULT IS DRY-RUN — no API key is read, no network is opened, and nothing is written:

    py scripts/capture_remaining_ai_drafts.py

A live run requires EVERY gate, and both paths absolute and OUTSIDE the repository:

    py scripts/capture_remaining_ai_drafts.py --live --confirm CAPTURE-32-AUTHORIZED \
        --manifest ABSOLUTE_PATH --output-dir ABSOLUTE_DIRECTORY

Live mode additionally requires the environment LUMEN_CAPTURE_LIVE_AI=1 and a present
ANTHROPIC_API_KEY. Any missing gate fails before a client is constructed or a network
connection is opened. This script never prints or persists the API key.

This is a DEMO utility, deliberately kept separate from the audited seven-alert runner
(src/live_capture.py), which it imports READ-ONLY and never modifies. ALERT001..ALERT007
are already captured and are explicitly refused here.

Crash safety comes from an atomic JSON manifest kept OUTSIDE the repository: a succeeded
alert is never called again, and a pending / failed / malformed / mismatched manifest halts
the whole run. Requests are strictly sequential with zero retries.

This module writes no CSV and no audit event. The manifest and the printed summary contain
no key, prompt, header, or raw response. The EXTERNAL capture output (written outside the
repository) intentionally contains normalized alert ids, claims, and evidence references,
and must therefore be treated as capture data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Mapping

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import llm_drafter  # noqa: E402
from src.capture_guardrails import (  # noqa: E402
    ALLOWED_ALERTS, CAPTURE_MODEL, CONCURRENCY, EXTENDED_THINKING_ENABLED,
    MAX_OUTPUT_TOKENS_PER_REQUEST, MAX_REQUEST_CHARS, MAX_RETRIES,
    MODEL_FALLBACK_ENABLED, REQUEST_TIMEOUT_SECONDS, REQUIRED_ENV_FLAG, REQUIRED_ENV_VALUE,
)
# Read-only reuse of the audited runner's pure helpers (no behaviour of it is changed).
from src.live_capture import (  # noqa: E402
    CaptureError, RequestPlan, _atomic_write_text, _estimate_input_tokens, _failure_code,
    _git_commit, _preflight_output_dir, _request_char_count, _require_external_absolute_path,
    _source_data_hash, _utc_now, _valid_usage, repo_root,
)

# ── demo scope + caps (this script only; the seven-alert guardrails are untouched) ──
REMAINING_ALERTS: tuple[str, ...] = tuple(f"ALERT{n:03d}" for n in range(8, 40))
EXCLUDED_ALERTS: frozenset[str] = frozenset(ALLOWED_ALERTS)     # ALERT001..ALERT007
REQUIRED_CONFIRMATION_PHRASE = "CAPTURE-32-AUTHORIZED"
MAX_ATTEMPTS = 32
MAX_CUMULATIVE_ESTIMATED_INPUT_TOKENS = 100_000
MAX_CUMULATIVE_REQUESTED_OUTPUT_TOKENS = 22_400                 # 32 x 700
MANIFEST_SCHEMA_VERSION = 1
CAPTURE_RUN_ID = "DEMO-CAPTURE-REMAINING-V1"
_API_KEY_ENV = "ANTHROPIC_API_KEY"                              # presence only; never read
_VALID_STATUS = ("planned", "pending", "succeeded", "failed")

# The two sets must never intersect: ALERT001..ALERT007 are already captured.
assert not (set(REMAINING_ALERTS) & EXCLUDED_ALERTS), "remaining scope must exclude ALERT001-007"
assert len(REMAINING_ALERTS) == MAX_ATTEMPTS == 32


# ── planning (pure; no client, no network, no key) ───────────────────────────
def plan_remaining(project_root: Path | None = None) -> list[RequestPlan]:
    """Plan the 32 remaining requests from the existing source-data/prompt path.

    Enforces the per-request character cap BEFORE any authorization. Pure: reads source
    CSVs read-only, prints nothing, opens no client/network, reads no key.
    """
    from src import pipeline

    plans: list[RequestPlan] = []
    for alert_id in REMAINING_ALERTS:
        if alert_id in EXCLUDED_ALERTS:                          # unreachable; defence in depth
            raise CaptureError(f"alert {alert_id} is out of scope for the remaining capture")
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
    """Plan + print the 32 remaining requests and confirm they fit the demo caps.

    Prints ONLY: alert id, request-char count, estimated input tokens, requested output
    limit, the fixed model, and the aggregate caps — never prompt text or customer data.
    Reads no API key, writes nothing, opens no network.
    """
    plans = plan_remaining(project_root)
    total_est_input = sum(p.estimated_input_tokens for p in plans)
    total_req_output = sum(p.requested_output_tokens for p in plans)

    print_fn("DRY RUN - no API key read, no network, no writes.")
    print_fn(f"Fixed model: {CAPTURE_MODEL}")
    print_fn(f"Scope: {REMAINING_ALERTS[0]}..{REMAINING_ALERTS[-1]} ({len(REMAINING_ALERTS)} alerts)")
    print_fn(f"Excluded (already captured): {', '.join(sorted(EXCLUDED_ALERTS))}")
    for p in plans:
        print_fn(
            f"  {p.alert_id}: request_chars={p.request_chars} "
            f"est_input_tokens={p.estimated_input_tokens} "
            f"requested_output_tokens={p.requested_output_tokens} model={p.model}"
        )
    print_fn(
        f"Aggregate: attempts={len(plans)}/{MAX_ATTEMPTS} "
        f"est_input_total={total_est_input}/{MAX_CUMULATIVE_ESTIMATED_INPUT_TOKENS} "
        f"requested_output_total={total_req_output}/{MAX_CUMULATIVE_REQUESTED_OUTPUT_TOKENS} "
        f"concurrency={CONCURRENCY} timeout={REQUEST_TIMEOUT_SECONDS}s retries={MAX_RETRIES} "
        f"extended_thinking={EXTENDED_THINKING_ENABLED} fallback={MODEL_FALLBACK_ENABLED}"
    )

    fits = (
        len(plans) == len(REMAINING_ALERTS)
        and len(plans) <= MAX_ATTEMPTS
        and not (set(p.alert_id for p in plans) & EXCLUDED_ALERTS)
        and all(p.model == CAPTURE_MODEL for p in plans)
        and all(p.request_chars <= MAX_REQUEST_CHARS for p in plans)
        and all(p.requested_output_tokens <= MAX_OUTPUT_TOKENS_PER_REQUEST for p in plans)
        and total_est_input <= MAX_CUMULATIVE_ESTIMATED_INPUT_TOKENS
        and total_req_output <= MAX_CUMULATIVE_REQUESTED_OUTPUT_TOKENS
    )
    print_fn(f"RESULT: all {len(plans)} remaining requests fit the demo caps." if fits
             else "RESULT: requests do NOT fit the demo caps.")
    return fits


# ── sequential demo budget ───────────────────────────────────────────────────
class _Budget:
    """Caps attempts, cumulative ESTIMATED input, and cumulative REQUESTED output.

    Strictly sequential (one in-flight attempt). Any recorded failure halts the whole run;
    there is no retry. Actual provider usage is accumulated for the summary/manifest.
    """

    def __init__(self) -> None:
        self.attempts = 0
        self.estimated_input_tokens = 0
        self.requested_output_tokens = 0
        self.actual_input_tokens = 0
        self.actual_output_tokens = 0
        self.halted = False

    def authorize_attempt(self, *, alert_id: str, model: str, request_chars: int,
                          estimated_input_tokens: int, requested_output_tokens: int) -> None:
        if self.halted:
            raise CaptureError("budget is halted; no further attempts are authorized")
        if alert_id in EXCLUDED_ALERTS:
            raise CaptureError(f"alert {alert_id} is already captured and must never be re-called")
        if alert_id not in REMAINING_ALERTS:
            raise CaptureError(f"alert {alert_id} is outside the remaining allowlist")
        if model != CAPTURE_MODEL:
            raise CaptureError("model is fixed; a different model is refused")
        if request_chars > MAX_REQUEST_CHARS:
            raise CaptureError(f"request for {alert_id} exceeds the per-request character cap")
        if requested_output_tokens > MAX_OUTPUT_TOKENS_PER_REQUEST:
            raise CaptureError(f"requested output for {alert_id} exceeds the per-request cap")
        if self.attempts + 1 > MAX_ATTEMPTS:
            raise CaptureError(f"attempt cap {MAX_ATTEMPTS} reached")
        if self.estimated_input_tokens + estimated_input_tokens > MAX_CUMULATIVE_ESTIMATED_INPUT_TOKENS:
            raise CaptureError("cumulative estimated input-token cap would be exceeded")
        if self.requested_output_tokens + requested_output_tokens > MAX_CUMULATIVE_REQUESTED_OUTPUT_TOKENS:
            raise CaptureError("cumulative requested output-token cap would be exceeded")
        self.attempts += 1
        self.estimated_input_tokens += estimated_input_tokens
        self.requested_output_tokens += requested_output_tokens

    def record_usage(self, *, input_tokens: int, output_tokens: int) -> None:
        self.actual_input_tokens += input_tokens
        self.actual_output_tokens += output_tokens

    def record_failure(self) -> None:
        self.halted = True


# ── manifest ─────────────────────────────────────────────────────────────────
def _limits_block() -> dict:
    return {
        "model": CAPTURE_MODEL,
        "max_attempts": MAX_ATTEMPTS,
        "max_output_tokens_per_request": MAX_OUTPUT_TOKENS_PER_REQUEST,
        "max_cumulative_estimated_input_tokens": MAX_CUMULATIVE_ESTIMATED_INPUT_TOKENS,
        "max_cumulative_requested_output_tokens": MAX_CUMULATIVE_REQUESTED_OUTPUT_TOKENS,
        "max_request_chars": MAX_REQUEST_CHARS,
        "timeout_seconds": REQUEST_TIMEOUT_SECONDS,
        "max_retries": MAX_RETRIES,
        "concurrency": CONCURRENCY,
        "extended_thinking_enabled": EXTENDED_THINKING_ENABLED,
        "model_fallback_enabled": MODEL_FALLBACK_ENABLED,
    }


def _write_manifest(path: Path, manifest: dict) -> None:
    _atomic_write_text(path, json.dumps(manifest, sort_keys=True, indent=2))


def _new_manifest(plans: list[RequestPlan], source_hash: str, git_commit: str,
                  started_at: str) -> dict:
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "capture_run_id": CAPTURE_RUN_ID,
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
    """Load + validate an existing manifest. Halt on malformed JSON, schema/model/run-id
    mismatch, a changed source hash / git commit, or a wrong alert set."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise CaptureError("manifest is malformed (unreadable or not valid JSON)")
    if not isinstance(data, dict):
        raise CaptureError("manifest is malformed (not an object)")
    if data.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise CaptureError("manifest schema-version mismatch — halting")
    if data.get("capture_run_id") != CAPTURE_RUN_ID:
        raise CaptureError("manifest is from a different capture run — halting")
    if data.get("model") != CAPTURE_MODEL:
        raise CaptureError("manifest is from a different model — halting")
    if data.get("source_data_hash") != source_hash:
        raise CaptureError("manifest source-data hash mismatch — halting")
    if data.get("source_git_commit") != git_commit:
        raise CaptureError("manifest source git-commit mismatch — halting")
    alerts = data.get("alerts")
    if not isinstance(alerts, dict) or set(alerts) != set(REMAINING_ALERTS):
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


def _default_client_factory():
    """Construct the Anthropic client with the guardrail timeout and ZERO retries.

    Imported lazily so that merely importing this module never touches the SDK/network.
    """
    import anthropic

    return anthropic.Anthropic(timeout=REQUEST_TIMEOUT_SECONDS, max_retries=MAX_RETRIES)


# ── the live run ─────────────────────────────────────────────────────────────
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
    """Run (or resume) the 32-alert demo capture. Returns a non-sensitive summary."""
    root = repo_root() if project_root is None else Path(project_root)
    now = now_fn if now_fn is not None else _utc_now

    # ── Gates that never touch the API key ───────────────────────────────────
    manifest_p = _require_external_absolute_path(manifest_path, "manifest", root)
    output_p = _require_external_absolute_path(output_dir, "output-dir", root)

    if env.get(REQUIRED_ENV_FLAG) != REQUIRED_ENV_VALUE:
        raise CaptureError(
            f"live capture denied: {REQUIRED_ENV_FLAG} must be exactly {REQUIRED_ENV_VALUE}"
        )
    if confirmation_phrase != REQUIRED_CONFIRMATION_PHRASE:
        raise CaptureError("live capture denied: confirmation phrase does not match")

    # API-key PRESENCE only, read last, once every other gate has passed.
    if not env.get(_API_KEY_ENV):
        raise CaptureError(f"live capture denied: {_API_KEY_ENV} is not present")

    # Output-directory preflight: BEFORE constructing a client, marking any alert pending,
    # initializing the manifest, or issuing any paid request.
    _preflight_output_dir(output_p)

    budget = _Budget()

    # Plan + provenance (still no client, no network).
    plans = plan_remaining(root)
    plan_by_id = {p.alert_id: p for p in plans}
    source_hash = _source_data_hash(root)
    git_commit = _git_commit(root)

    if manifest_p.exists():
        manifest = _load_manifest(manifest_p, source_hash, git_commit)
    else:
        manifest = _new_manifest(plans, source_hash, git_commit, now())
        _write_manifest(manifest_p, manifest)

    # ── Resume: replay succeeded through the fresh budget; halt on pending/failed. ──
    for alert_id in REMAINING_ALERTS:
        entry = manifest["alerts"][alert_id]
        status = entry["status"]
        if status == "succeeded":
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
                    estimated_input_tokens=entry["estimated_input_tokens"],
                    requested_output_tokens=MAX_OUTPUT_TOKENS_PER_REQUEST,
                )
            except CaptureError:
                raise CaptureError(f"resume halted: succeeded entry {alert_id} is over budget") from None
            budget.record_usage(
                input_tokens=entry["actual_input_tokens"],
                output_tokens=entry["actual_output_tokens"],
            )
        elif status == "pending":
            raise CaptureError(
                f"resume halted: alert {alert_id} is pending — its billing is unknown; "
                "a pending request must never be assumed unbilled"
            )
        elif status == "failed":
            raise CaptureError(f"resume halted: alert {alert_id} previously failed; no automatic retry")

    # ── Process alerts never attempted (planned), strictly sequentially. ─────
    from src import pipeline

    client: Any = None
    for alert_id in REMAINING_ALERTS:
        entry = manifest["alerts"][alert_id]
        if entry["status"] != "planned":
            continue
        if budget.halted:
            break
        if alert_id in EXCLUDED_ALERTS:                  # unreachable; defence in depth
            raise CaptureError(f"refusing to call already-captured alert {alert_id}")
        plan = plan_by_id[alert_id]

        # 1. Authorize the attempt against the demo caps.
        budget.authorize_attempt(
            alert_id=alert_id, model=CAPTURE_MODEL,
            request_chars=plan.request_chars,
            estimated_input_tokens=plan.estimated_input_tokens,
            requested_output_tokens=plan.requested_output_tokens,
        )
        # 2. Record pending in the manifest ATOMICALLY, BEFORE issuing the request.
        entry["status"] = "pending"
        _write_manifest(manifest_p, manifest)

        # Construct the client lazily on first real need (after every gate).
        if client is None:
            factory = client_factory if client_factory is not None else _default_client_factory
            client = factory()

        # 3. Issue exactly one request; any failure halts the whole run.
        alert, source_tables = pipeline._build_live_inputs(alert_id)
        try:
            claims, in_tok, out_tok = llm_drafter.draft_claims_for_capture(alert, source_tables, client)
            if not _valid_usage(in_tok) or not _valid_usage(out_tok):
                raise CaptureError("provider response missing usage")
        except BaseException as exc:
            code = _failure_code(exc)               # class name only; never the message
            budget.record_failure()
            entry.update(status="failed", failure_code=code)
            _write_manifest(manifest_p, manifest)
            raise CaptureError(f"capture halted: alert {alert_id} failed ({code})") from None

        # 4. Success: record usage, persist the output, then mark succeeded.
        budget.record_usage(input_tokens=in_tok, output_tokens=out_tok)
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
        entry.update(
            status="succeeded",
            actual_input_tokens=in_tok,
            actual_output_tokens=out_tok,
            output_filename=filename,
            output_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )
        _write_manifest(manifest_p, manifest)

    if all(e["status"] == "succeeded" for e in manifest["alerts"].values()):
        manifest["completed_at"] = now()
        _write_manifest(manifest_p, manifest)

    return {
        "attempts": budget.attempts,
        "succeeded": sum(1 for e in manifest["alerts"].values() if e["status"] == "succeeded"),
        "halted": budget.halted,
        "estimated_input_tokens": budget.estimated_input_tokens,
        "requested_output_tokens": budget.requested_output_tokens,
        "actual_input_tokens": budget.actual_input_tokens,
        "actual_output_tokens": budget.actual_output_tokens,
        "manifest_path": str(manifest_p),
    }


# ── CLI ──────────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="capture_remaining_ai_drafts",
        description=(
            f"One-time DEMO Anthropic AI-draft capture for {REMAINING_ALERTS[0]}.."
            f"{REMAINING_ALERTS[-1]} (default: dry-run; makes no live API request). "
            "ALERT001-ALERT007 are already captured and are never called."
        ),
    )
    p.add_argument("--live", action="store_true",
                   help="Perform the live capture. Default (omitted) is a dry-run.")
    p.add_argument("--confirm", default="",
                   help=f"Confirmation phrase; must be exactly {REQUIRED_CONFIRMATION_PHRASE}.")
    p.add_argument("--manifest", default="",
                   help="Absolute manifest path OUTSIDE the git repository (live only).")
    p.add_argument("--output-dir", dest="output_dir", default="",
                   help="Absolute output directory OUTSIDE the git repository (live only).")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.live:
        return 0 if dry_run(print_fn=print) else 2

    try:
        summary = run_capture(
            env=os.environ,
            confirmation_phrase=args.confirm,
            manifest_path=args.manifest,
            output_dir=args.output_dir,
        )
    except CaptureError as exc:
        # Non-sensitive refusal message (never the key, prompt, or provider text).
        print(f"capture refused: {exc}", file=sys.stderr)
        return 1
    print(f"capture complete: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
