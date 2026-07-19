"""Pure, capture-only Anthropic budget/authorization guardrail policy for LUMEN.

This is a DETERMINISTIC POLICY LAYER, nothing more. It has no Anthropic SDK import,
no network capability, and it never reads, receives, stores, logs, or returns the API
key. Its job is to bound a future one-time, human-authorized capture of AI drafts for
the seven flagship alerts (ALERT001..ALERT007): the capture run must never exceed a
small fixed budget, touch a non-allowlisted alert, retry, run concurrently, use a
different model, run two requests at once, or proceed without an explicit live
authorization.

This module performs NO requests. A caller drives it like this:

  budget = authorize_capture_session(env, confirmation_phrase, api_key_present)
  # ^ the ONLY way to obtain a CaptureBudget; enforces the live-authorization gates.
  budget.authorize_attempt(alert_id=..., model=..., request_chars=...,
                           requested_output_tokens=..., estimated_input_tokens=...)
  # ^ consumes one of the seven attempts and marks it PENDING, before the request.
  #   Only one attempt may be pending at a time.
  ...caller issues its request...
  budget.record_usage(alert_id=..., input_tokens=..., output_tokens=...)   # on success
  # or
  budget.record_failure(alert_id=...)                                       # on failure

Success/failure resolves the pending attempt. record_usage records actual usage first,
then halts (and raises) on any overage. record_failure permanently halts the session:
after a failure, no other alert may be attempted and the failed alert cannot be retried.

PROCESS-RESTART PROTECTION IS NOT PROVIDED HERE. This session lives in memory only; a
restart resets its counters. Restart-safe enforcement must be provided later by the
capture runner's PERSISTENT MANIFEST (a durable per-alert ledger the runner consults
before authorizing) and by the PROVIDER-SIDE SPEND LIMIT on the API key. This module
is intentionally pure and adds no persistence.

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
    "authorize_capture_session",
    "CaptureSummary",
    "CaptureBudget",
]


class CapturePolicyError(Exception):
    """Raised when a capture action violates the guardrail policy.

    Fail-closed: when this is raised for a *rejection* (bad authorization, oversized
    request, duplicate, pending conflict, wrong value) nothing is consumed. When it is
    raised for a recorded *overage* the actual usage has already been recorded and the
    session is halted before the exception propagates.
    """


# ── Immutable limits (unchanged) ─────────────────────────────────────────────
ALLOWED_ALERTS: frozenset[str] = frozenset(f"ALERT{n:03d}" for n in range(1, 8))  # ALERT001..ALERT007
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

# Module-private sentinel: only ``authorize_capture_session`` holds it, so a
# CaptureBudget cannot be constructed without passing through live authorization.
# NOT exported (absent from __all__ and underscore-prefixed).
_SESSION_AUTHORIZATION = object()


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
    rejection. The API key is never read here; presence is supplied by the caller as
    the boolean ``api_key_present``.
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


def authorize_capture_session(
    env: Mapping[str, str],
    confirmation_phrase: str,
    api_key_present: bool,
) -> "CaptureBudget":
    """The ONLY public way to obtain a usable CaptureBudget.

    Enforces the live-authorization gates (``require_live_authorization``) and, only
    when all pass, returns a CaptureBudget bound to the module-private authorization
    sentinel. Rejects every missing/wrong gate by raising CapturePolicyError.
    """
    require_live_authorization(env, confirmation_phrase, api_key_present)
    return CaptureBudget(_authorization=_SESSION_AUTHORIZATION)


def _require_positive_int(value: object, name: str) -> int:
    """Accept exactly a positive ``int``. Reject bool, float, str, zero, and negatives."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise CapturePolicyError(
            f"{name} must be an int (bool/float/str rejected); got {type(value).__name__}"
        )
    if value <= 0:
        raise CapturePolicyError(f"{name} must be a positive int; got {value}")
    return value


@dataclass(frozen=True)
class _PendingAttempt:
    """The single in-flight attempt. Stores ONLY the four request figures — never a
    prompt, response, key, header, or customer datum."""

    alert_id: str
    requested_output_tokens: int
    estimated_input_tokens: int
    request_chars: int


@dataclass(frozen=True)
class CaptureSummary:
    """Read-only budget summary: counts, totals, and status flags ONLY.

    Contains no alert IDs, model input, prompts, responses, credentials, headers,
    customer data, or exception text.
    """

    attempt_count: int
    attempts_remaining: int
    usage_recorded_count: int
    failure_count: int
    cumulative_input_tokens: int
    cumulative_output_tokens: int
    is_halted: bool
    has_pending_attempt: bool


class CaptureBudget:
    """Deterministic single-session attempt + token accountant.

    Obtain one ONLY via ``authorize_capture_session``; direct construction fails closed.
    Concurrency is fixed at 1, so this is intentionally single-threaded: at most one
    attempt may be pending, and it must be resolved (``record_usage`` or
    ``record_failure``) before another may be authorized. It stores only alert IDs and
    integer token totals — never prompts, responses, credentials, headers, or customer
    data.
    """

    def __init__(self, *, _authorization: object = None) -> None:
        if _authorization is not _SESSION_AUTHORIZATION:
            raise CapturePolicyError(
                "CaptureBudget must be created via authorize_capture_session() "
                "(direct construction is not permitted)"
            )
        self._attempted: set[str] = set()          # alert IDs whose attempt was authorized
        self._usage_recorded: set[str] = set()     # alert IDs whose usage was recorded
        self._pending: _PendingAttempt | None = None
        self._cumulative_input_tokens: int = 0
        self._cumulative_output_tokens: int = 0
        self._failure_count: int = 0
        self._halted: bool = False

    @property
    def attempt_count(self) -> int:
        return len(self._attempted)

    @property
    def attempts_remaining(self) -> int:
        return MAX_ATTEMPTS - len(self._attempted)

    @property
    def is_halted(self) -> bool:
        return self._halted

    @property
    def has_pending_attempt(self) -> bool:
        return self._pending is not None

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

        Refuses when the session is halted or another attempt is still pending. Every
        check runs BEFORE the attempt is consumed, so a rejected request costs nothing.
        On success one of the seven attempts is consumed and the attempt is marked
        pending — BEFORE the caller issues the request — so a request that later fails
        still counts and (zero retries) can never be attempted again this session.
        """
        if self._halted:
            raise CapturePolicyError("session is halted: no further attempts may be authorized")
        if self._pending is not None:
            raise CapturePolicyError(
                f"another attempt ({self._pending.alert_id}) is still pending; "
                "record its usage or failure first (concurrency is 1)"
            )
        # Exact positive-int figures (reject bool/float/str/zero/negative).
        request_chars = _require_positive_int(request_chars, "request_chars")
        requested_output_tokens = _require_positive_int(requested_output_tokens, "requested_output_tokens")
        estimated_input_tokens = _require_positive_int(estimated_input_tokens, "estimated_input_tokens")
        # Fixed model only (fallback disabled).
        if model != CAPTURE_MODEL:
            raise CapturePolicyError(
                f"model {model!r} is not the fixed capture model {CAPTURE_MODEL!r} "
                "(model fallback is disabled)"
            )
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
        # Pre-flight cumulative caps, against ACTUAL recorded usage plus this estimate.
        if self._cumulative_input_tokens + estimated_input_tokens > MAX_CUMULATIVE_INPUT_TOKENS:
            raise CapturePolicyError("request would exceed the cumulative input-token limit")
        if self._cumulative_output_tokens + requested_output_tokens > MAX_CUMULATIVE_OUTPUT_TOKENS:
            raise CapturePolicyError("request would exceed the cumulative output-token limit")
        # All gates passed → consume one attempt and mark it pending, before the request.
        self._attempted.add(alert_id)
        self._pending = _PendingAttempt(
            alert_id=alert_id,
            requested_output_tokens=requested_output_tokens,
            estimated_input_tokens=estimated_input_tokens,
            request_chars=request_chars,
        )

    def record_usage(self, *, alert_id: str, input_tokens: int, output_tokens: int) -> None:
        """Record the ACTUAL token usage for the currently pending alert.

        Requires exact positive ints (bool/float/str/zero/negative rejected). Records
        the actual usage and clears the pending attempt FIRST, then evaluates overages:
        actual output above the authorized request limit, or actual/cumulative usage at
        or over a hard limit, halts the session; an overage beyond a hard limit is
        recorded and then raised. A halted session rejects every future attempt.
        """
        if self._pending is None or self._pending.alert_id != alert_id:
            raise CapturePolicyError(
                f"record_usage must target the currently pending alert; got {alert_id!r}"
            )
        input_tokens = _require_positive_int(input_tokens, "input_tokens")
        output_tokens = _require_positive_int(output_tokens, "output_tokens")

        # Record actual usage and clear the pending attempt BEFORE evaluating overage.
        requested_output_limit = self._pending.requested_output_tokens
        self._usage_recorded.add(alert_id)
        self._cumulative_input_tokens += input_tokens
        self._cumulative_output_tokens += output_tokens
        self._pending = None

        # Evaluate overages against the now-recorded actuals.
        output_over_request = output_tokens > requested_output_limit
        input_over_cap = self._cumulative_input_tokens > MAX_CUMULATIVE_INPUT_TOKENS
        output_over_cap = self._cumulative_output_tokens > MAX_CUMULATIVE_OUTPUT_TOKENS
        input_reached_cap = self._cumulative_input_tokens >= MAX_CUMULATIVE_INPUT_TOKENS
        output_reached_cap = self._cumulative_output_tokens >= MAX_CUMULATIVE_OUTPUT_TOKENS

        if output_over_request or input_over_cap or output_over_cap:
            # Overage: the actual usage is already recorded; halt and raise.
            self._halted = True
            if output_over_request:
                raise CapturePolicyError(
                    f"actual output {output_tokens} exceeds the authorized request limit "
                    f"{requested_output_limit}; session halted"
                )
            raise CapturePolicyError("actual usage exceeds a cumulative hard limit; session halted")
        if input_reached_cap or output_reached_cap:
            # Reached a cumulative hard limit exactly: halt (no raise), but no further work.
            self._halted = True

    def record_failure(self, alert_id: str) -> None:
        """Record that the currently pending attempt FAILED (no error text is accepted).

        Applies only to the currently pending alert. Clears the pending attempt and
        PERMANENTLY halts the session: after any failure no other alert may be
        attempted, and the failed alert stays consumed and cannot be retried.
        """
        if self._pending is None or self._pending.alert_id != alert_id:
            raise CapturePolicyError(
                f"record_failure must target the currently pending alert; got {alert_id!r}"
            )
        self._pending = None
        self._failure_count += 1
        self._halted = True

    def summary(self) -> CaptureSummary:
        """Return a read-only snapshot: counts, totals, and status flags only."""
        return CaptureSummary(
            attempt_count=len(self._attempted),
            attempts_remaining=MAX_ATTEMPTS - len(self._attempted),
            usage_recorded_count=len(self._usage_recorded),
            failure_count=self._failure_count,
            cumulative_input_tokens=self._cumulative_input_tokens,
            cumulative_output_tokens=self._cumulative_output_tokens,
            is_halted=self._halted,
            has_pending_attempt=self._pending is not None,
        )
