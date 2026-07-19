"""Tests for the pure capture-only Anthropic guardrail policy (src/capture_guardrails).

The module is a deterministic policy layer: no Anthropic SDK import, no network, and
it never touches the API key. These tests exercise every authorization gate, every
budget cap, the attempt/usage accounting, and prove no SDK import and no network
access occur. No test imports the Anthropic SDK, and no test makes a live request.
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


def _authorize(budget, alert_id, *, model=None, request_chars=100,
               requested_output_tokens=700, estimated_input_tokens=1000):
    budget.authorize_attempt(
        alert_id=alert_id,
        model=model if model is not None else cg.CAPTURE_MODEL,
        request_chars=request_chars,
        requested_output_tokens=requested_output_tokens,
        estimated_input_tokens=estimated_input_tokens,
    )


# ── Immutable-limit constants ────────────────────────────────────────────────
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


# ── Live authorization ───────────────────────────────────────────────────────
def test_dry_run_denies_live_authorization():
    assert cg.is_live_authorized({}, "", False) is False        # default state = dry-run
    with pytest.raises(cg.CapturePolicyError):
        cg.require_live_authorization({}, "", False)


def test_wrong_or_missing_env_flag_rejected():
    with pytest.raises(cg.CapturePolicyError):
        cg.require_live_authorization({}, PHRASE, True)                       # missing
    with pytest.raises(cg.CapturePolicyError):
        cg.require_live_authorization({"LUMEN_CAPTURE_LIVE_AI": "0"}, PHRASE, True)  # wrong
    assert cg.is_live_authorized({"LUMEN_CAPTURE_LIVE_AI": "0"}, PHRASE, True) is False


def test_wrong_or_missing_confirmation_phrase_rejected():
    with pytest.raises(cg.CapturePolicyError):
        cg.require_live_authorization(LIVE_ENV, "", True)                     # missing
    with pytest.raises(cg.CapturePolicyError):
        cg.require_live_authorization(LIVE_ENV, "capture-7-authorized", True)  # wrong case
    assert cg.is_live_authorized(LIVE_ENV, "nope", True) is False


def test_missing_api_key_presence_flag_rejected():
    with pytest.raises(cg.CapturePolicyError):
        cg.require_live_authorization(LIVE_ENV, PHRASE, False)
    assert cg.is_live_authorized(LIVE_ENV, PHRASE, False) is False


def test_full_live_authorization_passes():
    cg.require_live_authorization(LIVE_ENV, PHRASE, True)          # does not raise
    assert cg.is_live_authorized(LIVE_ENV, PHRASE, True) is True


# ── Allowlist + fixed model ──────────────────────────────────────────────────
def test_alert008_and_arbitrary_ids_rejected():
    b = cg.CaptureBudget()
    for bad in ("ALERT008", "ALERT000", "ALERT042", "nope", "", "alert001"):
        with pytest.raises(cg.CapturePolicyError):
            _authorize(b, bad)
    assert b.attempt_count == 0                                   # nothing consumed


def test_alert001_through_007_allowlisted():
    b = cg.CaptureBudget()
    for n in range(1, 8):
        _authorize(b, f"ALERT{n:03d}")
    assert b.attempt_count == 7
    assert b.attempts_remaining == 0


def test_wrong_model_rejected():
    b = cg.CaptureBudget()
    with pytest.raises(cg.CapturePolicyError):
        _authorize(b, "ALERT001", model="claude-sonnet-4-20250514")
    assert b.attempt_count == 0


# ── Attempt cap, duplicates, no-retry ────────────────────────────────────────
def test_eighth_request_rejected():
    b = cg.CaptureBudget()
    for n in range(1, 8):
        _authorize(b, f"ALERT{n:03d}")
    assert b.attempts_remaining == 0
    with pytest.raises(cg.CapturePolicyError):        # an 8th attempt (any alert) is rejected
        _authorize(b, "ALERT001")
    assert b.attempt_count == 7


def test_duplicate_alert_attempt_rejected():
    b = cg.CaptureBudget()
    _authorize(b, "ALERT001")
    with pytest.raises(cg.CapturePolicyError):
        _authorize(b, "ALERT001")
    assert b.attempt_count == 1


def test_failed_attempt_cannot_be_retried():
    b = cg.CaptureBudget()
    _authorize(b, "ALERT001")            # attempt consumed BEFORE the request
    # simulate an API failure: no usage is recorded. The attempt still counts.
    assert b.attempts_remaining == 6
    with pytest.raises(cg.CapturePolicyError):
        _authorize(b, "ALERT001")        # zero retry: the same alert cannot be re-attempted
    assert b.attempt_count == 1


# ── Per-request caps (rejected before authorization) ─────────────────────────
def test_per_request_output_cap_enforced():
    b = cg.CaptureBudget()
    with pytest.raises(cg.CapturePolicyError):
        _authorize(b, "ALERT001", requested_output_tokens=cg.MAX_OUTPUT_TOKENS_PER_REQUEST + 1)
    assert b.attempt_count == 0                                   # not consumed


def test_serialized_request_size_cap_enforced():
    b = cg.CaptureBudget()
    with pytest.raises(cg.CapturePolicyError):
        _authorize(b, "ALERT001", request_chars=cg.MAX_REQUEST_CHARS + 1)
    assert b.attempt_count == 0


# ── Cumulative caps ──────────────────────────────────────────────────────────
def test_cumulative_input_cap_enforced_would_exceed_and_reached():
    b = cg.CaptureBudget()
    _authorize(b, "ALERT001", estimated_input_tokens=1000)
    b.record_usage(alert_id="ALERT001", input_tokens=34_500, output_tokens=100)  # cum_in = 34500
    # would exceed: 34500 + 1000 > 35000
    with pytest.raises(cg.CapturePolicyError):
        _authorize(b, "ALERT002", estimated_input_tokens=1000)
    assert b.attempt_count == 1
    # push actual usage to the cap, then every further attempt is rejected
    _authorize(b, "ALERT002", estimated_input_tokens=0)
    b.record_usage(alert_id="ALERT002", input_tokens=500, output_tokens=100)     # cum_in = 35000
    with pytest.raises(cg.CapturePolicyError):
        _authorize(b, "ALERT003", estimated_input_tokens=0)


def test_cumulative_output_cap_enforced_would_exceed_and_reached():
    b = cg.CaptureBudget()
    _authorize(b, "ALERT001")
    b.record_usage(alert_id="ALERT001", input_tokens=100, output_tokens=4_500)   # cum_out = 4500
    # would exceed: 4500 + 700 > 4900
    with pytest.raises(cg.CapturePolicyError):
        _authorize(b, "ALERT002", requested_output_tokens=700)
    # a smaller request still fits: 4500 + 300 <= 4900
    _authorize(b, "ALERT002", requested_output_tokens=300)
    b.record_usage(alert_id="ALERT002", input_tokens=100, output_tokens=400)     # cum_out = 4900
    with pytest.raises(cg.CapturePolicyError):                                    # reached
        _authorize(b, "ALERT003", requested_output_tokens=1)


# ── Usage accounting ─────────────────────────────────────────────────────────
def test_actual_usage_is_tracked():
    b = cg.CaptureBudget()
    _authorize(b, "ALERT001")
    b.record_usage(alert_id="ALERT001", input_tokens=900, output_tokens=650)
    _authorize(b, "ALERT002")
    b.record_usage(alert_id="ALERT002", input_tokens=1100, output_tokens=350)
    s = b.summary()
    assert s.cumulative_input_tokens == 2000
    assert s.cumulative_output_tokens == 1000
    assert s.attempt_count == 2
    assert s.usage_recorded_count == 2
    assert s.attempts_remaining == 5


def test_negative_usage_rejected():
    b = cg.CaptureBudget()
    _authorize(b, "ALERT001")
    with pytest.raises(cg.CapturePolicyError):
        b.record_usage(alert_id="ALERT001", input_tokens=-1, output_tokens=100)
    with pytest.raises(cg.CapturePolicyError):
        b.record_usage(alert_id="ALERT001", input_tokens=100, output_tokens=-5)
    assert b.summary().cumulative_input_tokens == 0


def test_duplicate_usage_recording_rejected():
    b = cg.CaptureBudget()
    _authorize(b, "ALERT001")
    b.record_usage(alert_id="ALERT001", input_tokens=100, output_tokens=100)
    with pytest.raises(cg.CapturePolicyError):
        b.record_usage(alert_id="ALERT001", input_tokens=100, output_tokens=100)
    assert b.summary().cumulative_input_tokens == 100


def test_usage_for_unattempted_alert_rejected():
    b = cg.CaptureBudget()
    with pytest.raises(cg.CapturePolicyError):
        b.record_usage(alert_id="ALERT001", input_tokens=100, output_tokens=100)


# ── Summary carries no secret / request content ──────────────────────────────
def test_summary_exposes_no_secret_or_request_content():
    b = cg.CaptureBudget()
    _authorize(b, "ALERT001")
    b.record_usage(alert_id="ALERT001", input_tokens=900, output_tokens=650)
    s = b.summary()
    fields = vars(s)
    # only counts and integer totals — no strings, no content, no IDs, no key material
    assert set(fields) == {
        "attempt_count", "attempts_remaining", "usage_recorded_count",
        "cumulative_input_tokens", "cumulative_output_tokens",
    }
    assert all(isinstance(v, int) and not isinstance(v, bool) for v in fields.values())
    blob = repr(s).lower()
    for forbidden in ("key", "sk-", "prompt", "response", "authorization", "bearer", "header", "customer"):
        assert forbidden not in blob


# ── No Anthropic SDK import, no network ──────────────────────────────────────
def test_module_does_not_import_anthropic_sdk():
    assert not hasattr(cg, "anthropic")      # the module never bound the SDK
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
    cg.require_live_authorization(LIVE_ENV, PHRASE, True)
    b = cg.CaptureBudget()
    _authorize(b, "ALERT001")
    b.record_usage(alert_id="ALERT001", input_tokens=900, output_tokens=650)
    assert b.summary().cumulative_output_tokens == 650
