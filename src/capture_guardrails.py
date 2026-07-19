"""Pure, capture-only Anthropic budget/authorization guardrail policy for LUMEN.

This is a DETERMINISTIC POLICY LAYER, nothing more. It has no Anthropic SDK import,
no network capability, and it never reads, receives, stores, logs, or returns the API
key. Its job is to bound a future one-time, human-authorized capture of AI drafts for
the seven flagship alerts (ALERT001..ALERT007): the capture run must never exceed a
small fixed budget, touch a non-allowlisted alert, retry, run concurrently, use a
different model, or proceed without an explicit live authorization.

This module performs NO requests. A caller consults it before and after each request
IT makes:
  - ``require_live_authorization(...)`` — gate the whole run (env flag + exact phrase
    + confirmed API-key presence). Default state (missing flag/phrase) is dry-run and
    is rejected here, so callers stay in dry-run unless every gate passes.
  - ``CaptureBudget.authorize_attempt(...)`` — gate one request against the budget
    (allowlist, fixed model, per-request caps, attempt cap, one-attempt-per-alert,
    cumulative caps). On success it CONSUMES one of the seven attempts BEFORE the
    request is made, so a request that later fails still counts and cannot be retried.
  - ``CaptureBudget.record_usage(...)`` — record actual token usage AFTER a response.
  - ``CaptureBudget.summary()`` — a read-only, counts-and-totals-only view.

Integration into src/llm_drafter.py is a separate, later step. Nothing here calls an
AI provider or a token-counting endpoint.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

__all__ = [
    "CapturePolicyError",
    "ALLOWED_ALERTS",
    "CAPTURE_MODEL",
    "MAX_ATTEMPTS",
    "MAX_OUTPUT_TOKENS_PER_REQUEST",
    "MAX_CUMULATIVE_INPUT_TOKENS",
    "MAX_CUMULATIVE_OUTPUT_TOKENS",
    "MAX_REQUEST_CHARS",
    "REQUEST_TIMEOUT_SECONDS",
    "MAX_RETRIES",
    "CONCURRENCY",
    "EXTENDED_THINKING_ENABLED",
    "MODEL_FALLBACK_ENABLED",
    "REQUIRED_ENV_FLAG",
    "REQUIRED_ENV_VALUE",
    "REQUIRED_CONFIRMATION_PHRASE",
    "is_live_authorized",
    "require_live_authorization",
    "CaptureSummary",
    "CaptureBudget",
]


class CapturePolicyError(Exception):
    """Raised when a capture action violates the guardrail policy.

    Fail-closed: when this is raised, nothing is consumed and nothing is recorded —
    the caller may not proceed with the rejected request.
    """


# ── Immutable limits ─────────────────────────────────────────────────────────
# Allowlist: exactly the seven flagship alerts, nothing else.
ALLOWED_ALERTS: frozenset[str] = frozenset(f"ALERT{n:03d}" for n in range(1, 8))  # ALERT001..ALERT007
# Fixed capture model. Model fallback is disabled, so any other model is rejected.
CAPTURE_MODEL: str = "claude-haiku-4-5-20251001"
MAX_ATTEMPTS: int = 7
MAX_OUTPUT_TOKENS_PER_REQUEST: int = 700
MAX_CUMULATIVE_INPUT_TOKENS: int = 35_000
MAX_CUMULATIVE_OUTPUT_TOKENS: int = 4_900
MAX_REQUEST_CHARS: int = 20_000        # serialized request size cap, per request
REQUEST_TIMEOUT_SECONDS: int = 30
MAX_RETRIES: int = 0                    # zero automatic retries
CONCURRENCY: int = 1
EXTENDED_THINKING_ENABLED: bool = False
MODEL_FALLBACK_ENABLED: bool = False

# ── Live-authorization gates ─────────────────────────────────────────────────
REQUIRED_ENV_FLAG: str = "LUMEN_CAPTURE_LIVE_AI"
REQUIRED_ENV_VALUE: str = "1"
REQUIRED_CONFIRMATION_PHRASE: str = "CAPTURE-7-AUTHORIZED"


def is_live_authorized(
    env: Mapping[str, str],
    confirmation_phrase: str,
    api_key_present: bool,
) -> bool:
    """Pure predicate: True only when ALL live gates pass. Never raises.

    Gates: the capture flag equals ``"1"``, the confirmation phrase matches exactly,
    and API-key presence is the boolean ``True``. The default state (a missing flag
    or phrase) is dry-run and returns False. Reads ONLY the capture flag from ``env``;
    it never reads the API key — the caller passes ``api_key_present`` (a boolean it
    computed itself), so the key is never received, stored, logged, or returned here.
    """
    return (
        str(env.get(REQUIRED_ENV_FLAG, "")) == REQUIRED_ENV_VALUE
        and confirmation_phrase == REQUIRED_CONFIRMATION_PHRASE
        and api_key_present is True
    )


def require_live_authorization(
    env: Mapping[str, str],
    confirmation_phrase: str,
    api_key_present: bool,
) -> None:
    """Enforce every live-authorization gate; raise CapturePolicyError on any failure.

    Because the default state is dry-run, calling this without the flag/phrase is a
    rejection — a caller stays in dry-run unless all three gates pass. The API key is
    never read here; presence is supplied by the caller as the boolean
    ``api_key_present``.
    """
    if str(env.get(REQUIRED_ENV_FLAG, "")) != REQUIRED_ENV_VALUE:
        raise CapturePolicyError(
            f"live capture denied: {REQUIRED_ENV_FLAG} must equal {REQUIRED_ENV_VALUE!r} "
            "(default state is dry-run)"
        )
    if confirmation_phrase != REQUIRED_CONFIRMATION_PHRASE:
        raise CapturePolicyError("live capture denied: confirmation phrase does not match")
    if api_key_present is not True:
        raise CapturePolicyError("live capture denied: API-key presence not confirmed")


@dataclass(frozen=True)
class CaptureSummary:
    """Read-only budget summary: counts and totals ONLY.

    Contains no alert content, prompts, responses, credentials, headers, or customer
    data — every field is a plain integer.
    """

    attempt_count: int
    attempts_remaining: int
    usage_recorded_count: int
    cumulative_input_tokens: int
    cumulative_output_tokens: int


class CaptureBudget:
    """Deterministic single-session attempt + token accountant.

    Concurrency is fixed at 1, so this is intentionally not thread-safe. It stores only
    alert IDs (for allowlist/duplicate enforcement) and integer token totals — never
    prompts, responses, credentials, headers, or customer data.
    """

    def __init__(self) -> None:
        self._attempted: set[str] = set()          # alert IDs whose attempt was authorized
        self._usage_recorded: set[str] = set()     # alert IDs whose usage was recorded
        self._cumulative_input_tokens: int = 0
        self._cumulative_output_tokens: int = 0

    @property
    def attempt_count(self) -> int:
        return len(self._attempted)

    @property
    def attempts_remaining(self) -> int:
        return MAX_ATTEMPTS - len(self._attempted)

    def authorize_attempt(
        self,
        *,
        alert_id: str,
        model: str,
        request_chars: int,
        requested_output_tokens: int,
        estimated_input_tokens: int,
    ) -> None:
        """Authorize one capture request against the budget, or raise CapturePolicyError.

        Every check below runs BEFORE the attempt is consumed, so a rejected request
        (wrong model, oversized, over-budget, non-allowlisted, duplicate) costs nothing.
        Only when all checks pass is one of the seven attempts consumed — BEFORE the
        caller issues the request — so a request that subsequently fails still counts
        and (with zero retries) can never be attempted again this session.
        """
        # Fixed model only (fallback disabled).
        if model != CAPTURE_MODEL:
            raise CapturePolicyError(
                f"model {model!r} is not the fixed capture model {CAPTURE_MODEL!r} "
                "(model fallback is disabled)"
            )
        # Non-negative request figures.
        if request_chars < 0 or requested_output_tokens < 0 or estimated_input_tokens < 0:
            raise CapturePolicyError("request figures (chars, output tokens, input estimate) must be non-negative")
        # Per-request caps — rejected before authorization (no attempt consumed).
        if requested_output_tokens > MAX_OUTPUT_TOKENS_PER_REQUEST:
            raise CapturePolicyError(
                f"requested_output_tokens {requested_output_tokens} exceeds the per-request cap "
                f"{MAX_OUTPUT_TOKENS_PER_REQUEST}"
            )
        if request_chars > MAX_REQUEST_CHARS:
            raise CapturePolicyError(
                f"serialized request size {request_chars} exceeds the per-request cap {MAX_REQUEST_CHARS}"
            )
        # Attempt cap — checked before allowlist/duplicate so any 8th attempt, whatever
        # alert it names, is rejected as an attempt-cap breach.
        if len(self._attempted) >= MAX_ATTEMPTS:
            raise CapturePolicyError(f"attempt cap reached: at most {MAX_ATTEMPTS} attempts per session")
        # Allowlist.
        if alert_id not in ALLOWED_ALERTS:
            raise CapturePolicyError(
                f"alert {alert_id!r} is not on the capture allowlist (ALERT001..ALERT007)"
            )
        # One attempt per alert per session (zero retry: a consumed alert never repeats).
        if alert_id in self._attempted:
            raise CapturePolicyError(f"alert {alert_id!r} was already attempted this session")
        # Cumulative caps, against ACTUAL recorded usage.
        if self._cumulative_input_tokens >= MAX_CUMULATIVE_INPUT_TOKENS:
            raise CapturePolicyError("cumulative input-token limit already reached")
        if self._cumulative_output_tokens >= MAX_CUMULATIVE_OUTPUT_TOKENS:
            raise CapturePolicyError("cumulative output-token limit already reached")
        if self._cumulative_input_tokens + estimated_input_tokens > MAX_CUMULATIVE_INPUT_TOKENS:
            raise CapturePolicyError("request would exceed the cumulative input-token limit")
        if self._cumulative_output_tokens + requested_output_tokens > MAX_CUMULATIVE_OUTPUT_TOKENS:
            raise CapturePolicyError("request would exceed the cumulative output-token limit")
        # All gates passed → consume one attempt now, before the request is issued.
        self._attempted.add(alert_id)

    def record_usage(self, *, alert_id: str, input_tokens: int, output_tokens: int) -> None:
        """Record the ACTUAL token usage reported by a response, exactly once per alert.

        Rejects usage for an un-attempted alert, a second recording for the same alert,
        and any negative token count.
        """
        if alert_id not in self._attempted:
            raise CapturePolicyError(f"cannot record usage for un-attempted alert {alert_id!r}")
        if alert_id in self._usage_recorded:
            raise CapturePolicyError(f"usage already recorded for alert {alert_id!r}")
        if input_tokens < 0 or output_tokens < 0:
            raise CapturePolicyError("usage token counts must be non-negative")
        self._usage_recorded.add(alert_id)
        self._cumulative_input_tokens += int(input_tokens)
        self._cumulative_output_tokens += int(output_tokens)

    def summary(self) -> CaptureSummary:
        """Return a read-only, counts-and-totals-only snapshot (no content, no secrets)."""
        return CaptureSummary(
            attempt_count=len(self._attempted),
            attempts_remaining=MAX_ATTEMPTS - len(self._attempted),
            usage_recorded_count=len(self._usage_recorded),
            cumulative_input_tokens=self._cumulative_input_tokens,
            cumulative_output_tokens=self._cumulative_output_tokens,
        )
