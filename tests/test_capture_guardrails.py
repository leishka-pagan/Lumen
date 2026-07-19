"""Tests for the hardened, pure capture-only Anthropic guardrail policy
(src/capture_guardrails).

The module is a deterministic policy layer: no Anthropic SDK import, no network, and
it never touches the API key. A CaptureBudget can be obtained ONLY through
``authorize_capture_session`` (live authorization enforced). At most one attempt is
pending at a time; success records actual usage (halting on overage), and failure
permanently halts the session. Every numeric field must be an exact positive int.

No test imports the Anthropic SDK, and no test makes a live request.
"""

from __future__ import annotations

import inspect
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import capture_guardrails as cg  # noqa: E402

PHRASE = "CAPTURE-7-AUTHORIZED"
LIVE_ENV = {"LUMEN_CAPTURE_LIVE_AI": "1"}


def _session() -> "cg.CaptureBudget":
    """An authorized, usable budget (all live gates pass)."""
    return cg.authorize_capture_session(LIVE_ENV, PHRASE, True)


def _authorize(b, alert_id, *, model=None, request_chars=100,
               requested_output_tokens=700, estimated_input_tokens=1000):
    b.authorize_attempt(
        alert_id=alert_id,
        model=model if model is not None else cg.CAPTURE_MODEL,
        request_chars=request_chars,
        requested_output_tokens=requested_output_tokens,
        estimated_input_tokens=estimated_input_tokens,
    )


def _attempt_and_resolve(b, alert_id, *, input_tokens=100, output_tokens=100):
    _authorize(b, alert_id)
    b.record_usage(alert_id=alert_id, input_tokens=input_tokens, output_tokens=output_tokens)


# ── Immutable-limit constants (unchanged) ────────────────────────────────────
def test_constants_have_required_values():
    assert cg.CAPTURE_MODEL == "claude-haiku-4-5-20251001"
    assert cg.ALLOWED_ALERTS == frozenset(
        {"ALERT001", "ALERT002", "ALERT003", "ALERT004", "ALERT005", "ALERT006", "ALERT007"}
    )
    assert cg.MAX_ATTEMPTS == 7
    assert cg.MAX_OUTPUT_TOKENS_PER_REQUEST == 700
    assert cg.MAX_CUMULATIVE_INPUT_TOKENS == 35_000
    assert cg.MAX_CUMULATIVE_OUTPUT_TOKENS == 4_900
    assert cg.MAX_REQUEST_CHARS == 20_000
    assert cg.REQUEST_TIMEOUT_SECONDS == 30
    assert cg.MAX_RETRIES == 0
    assert cg.CONCURRENCY == 1
    assert cg.EXTENDED_THINKING_ENABLED is False
    assert cg.MODEL_FALLBACK_ENABLED is False
    assert cg.REQUIRED_ENV_FLAG == "LUMEN_CAPTURE_LIVE_AI"
    assert cg.REQUIRED_ENV_VALUE == "1"
    assert cg.REQUIRED_CONFIRMATION_PHRASE == PHRASE


# ── Session authorization binding ────────────────────────────────────────────
def test_direct_capturebudget_construction_fails():
    with pytest.raises(cg.CapturePolicyError):
        cg.CaptureBudget()
    with pytest.raises(cg.CapturePolicyError):
        cg.CaptureBudget(_authorization=object())     # any non-sentinel object is rejected


def test_session_factory_rejects_every_missing_or_wrong_gate():
    for env, phrase, key in [
        ({}, "", False),                                   # dry-run default
        ({}, PHRASE, True),                                # missing flag
        ({"LUMEN_CAPTURE_LIVE_AI": "0"}, PHRASE, True),    # wrong flag
        (LIVE_ENV, "", True),                              # missing phrase
        (LIVE_ENV, "capture-7-authorized", True),          # wrong-case phrase
        (LIVE_ENV, PHRASE, False),                         # no API key present
    ]:
        with pytest.raises(cg.CapturePolicyError):
            cg.authorize_capture_session(env, phrase, key)


def test_session_factory_returns_usable_budget_only_when_all_gates_pass():
    b = cg.authorize_capture_session(LIVE_ENV, PHRASE, True)
    assert isinstance(b, cg.CaptureBudget)
    _authorize(b, "ALERT001")                              # usable
    assert b.attempt_count == 1


def test_sentinel_is_not_exported():
    assert "_SESSION_AUTHORIZATION" not in cg.__all__


# ── Allowlist + fixed model ──────────────────────────────────────────────────
def test_alert008_and_arbitrary_ids_rejected():
    b = _session()
    for bad in ("ALERT008", "ALERT000", "ALERT042", "nope", "", "alert001"):
        with pytest.raises(cg.CapturePolicyError):
            _authorize(b, bad)
    assert b.attempt_count == 0


def test_alert001_through_007_allowlisted():
    b = _session()
    for n in range(1, 8):
        _attempt_and_resolve(b, f"ALERT{n:03d}")
    assert b.attempt_count == 7
    assert b.attempts_remaining == 0


def test_wrong_model_rejected():
    b = _session()
    with pytest.raises(cg.CapturePolicyError):
        _authorize(b, "ALERT001", model="claude-sonnet-4-20250514")
    assert b.attempt_count == 0


# ── Single-request sequencing ────────────────────────────────────────────────
def test_second_attempt_blocked_while_first_pending():
    b = _session()
    _authorize(b, "ALERT001")
    assert b.has_pending_attempt is True
    with pytest.raises(cg.CapturePolicyError):
        _authorize(b, "ALERT002")
    assert b.attempt_count == 1


def test_valid_usage_clears_pending_state():
    b = _session()
    _authorize(b, "ALERT001")
    b.record_usage(alert_id="ALERT001", input_tokens=900, output_tokens=650)
    assert b.has_pending_attempt is False


def test_second_alert_proceeds_only_after_usage_recorded():
    b = _session()
    _authorize(b, "ALERT001")
    with pytest.raises(cg.CapturePolicyError):
        _authorize(b, "ALERT002")
    b.record_usage(alert_id="ALERT001", input_tokens=100, output_tokens=100)
    _authorize(b, "ALERT002")                              # now allowed
    assert b.attempt_count == 2 and b.has_pending_attempt is True


def test_eighth_request_rejected():
    b = _session()
    for n in range(1, 8):
        _attempt_and_resolve(b, f"ALERT{n:03d}")
    assert b.attempts_remaining == 0 and b.is_halted is False
    with pytest.raises(cg.CapturePolicyError):
        _authorize(b, "ALERT001")
    assert b.attempt_count == 7


def test_duplicate_alert_attempt_rejected():
    b = _session()
    _attempt_and_resolve(b, "ALERT001")
    with pytest.raises(cg.CapturePolicyError):
        _authorize(b, "ALERT001")
    assert b.attempt_count == 1


# ── Failure handling ─────────────────────────────────────────────────────────
def test_record_failure_halts_session():
    b = _session()
    _authorize(b, "ALERT001")
    b.record_failure("ALERT001")
    assert b.is_halted is True
    assert b.has_pending_attempt is False
    assert b.summary().failure_count == 1


def test_every_future_alert_blocked_after_failure():
    b = _session()
    _authorize(b, "ALERT001")
    b.record_failure("ALERT001")
    with pytest.raises(cg.CapturePolicyError):
        _authorize(b, "ALERT002")
    with pytest.raises(cg.CapturePolicyError):
        _authorize(b, "ALERT001")     # failed alert stays consumed; cannot be retried
    assert b.attempt_count == 1


def test_failure_cannot_be_recorded_for_wrong_alert():
    b = _session()
    _authorize(b, "ALERT001")
    with pytest.raises(cg.CapturePolicyError):
        b.record_failure("ALERT002")
    # no pending -> record_failure rejected
    b2 = _session()
    with pytest.raises(cg.CapturePolicyError):
        b2.record_failure("ALERT001")


def test_record_failure_takes_no_error_content():
    # The signature must accept only the alert id (no message / exception parameter).
    sig = inspect.signature(cg.CaptureBudget.record_failure)
    assert list(sig.parameters) == ["self", "alert_id"]


# ── Usage targeting ──────────────────────────────────────────────────────────
def test_usage_cannot_be_recorded_for_wrong_alert():
    b = _session()
    _authorize(b, "ALERT001")
    with pytest.raises(cg.CapturePolicyError):
        b.record_usage(alert_id="ALERT002", input_tokens=100, output_tokens=100)
    b2 = _session()
    with pytest.raises(cg.CapturePolicyError):        # no pending attempt at all
        b2.record_usage(alert_id="ALERT001", input_tokens=100, output_tokens=100)


# ── Actual-usage overage: recorded, then halts and raises ────────────────────
def test_actual_output_over_request_limit_records_then_halts_and_raises():
    b = _session()
    _authorize(b, "ALERT001", requested_output_tokens=700)
    with pytest.raises(cg.CapturePolicyError):
        b.record_usage(alert_id="ALERT001", input_tokens=100, output_tokens=800)  # 800 > 700
    s = b.summary()
    assert s.cumulative_output_tokens == 800     # the overage WAS recorded
    assert s.is_halted is True
    with pytest.raises(cg.CapturePolicyError):    # halted session rejects further attempts
        _authorize(b, "ALERT002")


def test_actual_cumulative_input_overage_records_then_halts_and_raises():
    b = _session()
    _authorize(b, "ALERT001", estimated_input_tokens=1)
    with pytest.raises(cg.CapturePolicyError):
        b.record_usage(alert_id="ALERT001",
                       input_tokens=cg.MAX_CUMULATIVE_INPUT_TOKENS + 1, output_tokens=100)
    s = b.summary()
    assert s.cumulative_input_tokens == cg.MAX_CUMULATIVE_INPUT_TOKENS + 1  # recorded overage
    assert s.is_halted is True


def test_actual_cumulative_output_overage_records_then_halts_and_raises():
    # Per-request cap is 700 and cumulative is 4900 (== 7 * 700), so a cumulative-output
    # overage necessarily coincides with a per-request overage; both record, halt, raise.
    b = _session()
    for n in range(1, 7):                         # ALERT001..006: cum_output -> 4200
        _attempt_and_resolve(b, f"ALERT{n:03d}", input_tokens=100, output_tokens=700)
    _authorize(b, "ALERT007", requested_output_tokens=700)
    with pytest.raises(cg.CapturePolicyError):
        b.record_usage(alert_id="ALERT007", input_tokens=100, output_tokens=800)  # -> 5000
    s = b.summary()
    assert s.cumulative_output_tokens == 5000     # > 4900, recorded
    assert s.is_halted is True


def test_reaching_a_cumulative_limit_halts_without_raising():
    b = _session()
    _authorize(b, "ALERT001", estimated_input_tokens=1)
    b.record_usage(alert_id="ALERT001",
                   input_tokens=cg.MAX_CUMULATIVE_INPUT_TOKENS, output_tokens=100)  # == cap, no raise
    s = b.summary()
    assert s.cumulative_input_tokens == cg.MAX_CUMULATIVE_INPUT_TOKENS
    assert s.is_halted is True
    with pytest.raises(cg.CapturePolicyError):
        _authorize(b, "ALERT002")


# ── Per-request caps (rejected before authorization) ─────────────────────────
def test_per_request_output_cap_enforced():
    b = _session()
    with pytest.raises(cg.CapturePolicyError):
        _authorize(b, "ALERT001", requested_output_tokens=cg.MAX_OUTPUT_TOKENS_PER_REQUEST + 1)
    assert b.attempt_count == 0 and b.has_pending_attempt is False


def test_serialized_request_size_cap_enforced():
    b = _session()
    with pytest.raises(cg.CapturePolicyError):
        _authorize(b, "ALERT001", request_chars=cg.MAX_REQUEST_CHARS + 1)
    assert b.attempt_count == 0


def test_pre_flight_cumulative_input_would_exceed_rejected():
    b = _session()
    with pytest.raises(cg.CapturePolicyError):
        _authorize(b, "ALERT001", estimated_input_tokens=cg.MAX_CUMULATIVE_INPUT_TOKENS + 1)
    assert b.attempt_count == 0


# ── Strict numeric validation (int only; no bool/float/str/zero/negative) ────
BAD_VALUES = [True, False, 1.0, 0.0, "1", "700", 0, -1]


@pytest.mark.parametrize("bad", BAD_VALUES)
def test_authorize_rejects_bad_request_chars(bad):
    b = _session()
    with pytest.raises(cg.CapturePolicyError):
        _authorize(b, "ALERT001", request_chars=bad)
    assert b.attempt_count == 0


@pytest.mark.parametrize("bad", BAD_VALUES)
def test_authorize_rejects_bad_requested_output_tokens(bad):
    b = _session()
    with pytest.raises(cg.CapturePolicyError):
        _authorize(b, "ALERT001", requested_output_tokens=bad)
    assert b.attempt_count == 0


@pytest.mark.parametrize("bad", BAD_VALUES)
def test_authorize_rejects_bad_estimated_input_tokens(bad):
    b = _session()
    with pytest.raises(cg.CapturePolicyError):
        _authorize(b, "ALERT001", estimated_input_tokens=bad)
    assert b.attempt_count == 0


@pytest.mark.parametrize("bad", BAD_VALUES)
def test_record_usage_rejects_bad_input_tokens(bad):
    b = _session()
    _authorize(b, "ALERT001")
    with pytest.raises(cg.CapturePolicyError):
        b.record_usage(alert_id="ALERT001", input_tokens=bad, output_tokens=100)
    assert b.summary().cumulative_input_tokens == 0 and b.has_pending_attempt is True


@pytest.mark.parametrize("bad", BAD_VALUES)
def test_record_usage_rejects_bad_output_tokens(bad):
    b = _session()
    _authorize(b, "ALERT001")
    with pytest.raises(cg.CapturePolicyError):
        b.record_usage(alert_id="ALERT001", input_tokens=100, output_tokens=bad)
    assert b.summary().cumulative_output_tokens == 0 and b.has_pending_attempt is True


# ── Summary shape / no sensitive content ─────────────────────────────────────
def test_summary_exposes_counts_totals_and_flags_only():
    b = _session()
    _authorize(b, "ALERT001")
    b.record_usage(alert_id="ALERT001", input_tokens=900, output_tokens=650)
    s = b.summary()
    fields = vars(s)
    assert set(fields) == {
        "attempt_count", "attempts_remaining", "usage_recorded_count", "failure_count",
        "cumulative_input_tokens", "cumulative_output_tokens", "is_halted", "has_pending_attempt",
    }
    for name, value in fields.items():
        if name in ("is_halted", "has_pending_attempt"):
            assert isinstance(value, bool)
        else:
            assert isinstance(value, int) and not isinstance(value, bool)
    blob = repr(s).lower()
    for forbidden in ("alert", "key", "sk-", "prompt", "response", "authorization",
                      "bearer", "header", "customer", "model", "claude", "haiku"):
        assert forbidden not in blob


def test_summary_is_frozen_read_only():
    s = _session().summary()
    with pytest.raises(Exception):
        s.attempt_count = 99  # frozen dataclass


# ── No Anthropic SDK import, no network ──────────────────────────────────────
def test_module_does_not_import_anthropic_sdk():
    assert not hasattr(cg, "anthropic")
    src = inspect.getsource(cg)
    assert "import anthropic" not in src
    assert "anthropic." not in src           # no SDK attribute access (e.g. anthropic.Anthropic)


def test_no_network_access_during_full_policy_flow(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("network access attempted by the guardrail policy")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    if hasattr(socket, "getaddrinfo"):
        monkeypatch.setattr(socket, "getaddrinfo", _boom)

    # Exercise the whole policy surface; none of it may open a socket.
    b = cg.authorize_capture_session(LIVE_ENV, PHRASE, True)
    _authorize(b, "ALERT001")
    b.record_usage(alert_id="ALERT001", input_tokens=900, output_tokens=650)
    _authorize(b, "ALERT002")
    b.record_failure("ALERT002")
    assert b.summary().is_halted is True
