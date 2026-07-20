"""Tests for the guarded, resumable, one-time Anthropic capture runner.

Zero live requests: every test uses a FAKE client and temporary directories OUTSIDE the
repository worktree (pytest ``tmp_path`` lives in the system temp). A poison client
factory proves a rejected gate fails BEFORE any client is constructed, and a socket
monkeypatch proves no network is opened.
"""

from __future__ import annotations

import hashlib
import json
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT))

from src import live_capture  # noqa: E402
from src.live_capture import CaptureError  # noqa: E402
from src.capture_guardrails import (  # noqa: E402
    CAPTURE_MODEL, CapturePolicyError, MAX_OUTPUT_TOKENS_PER_REQUEST,
    REQUIRED_CONFIRMATION_PHRASE, authorize_capture_session,
)

CAPTURE_ALERTS = live_capture.CAPTURE_ALERTS
FAKE_KEY = "sk-ant-FAKE-not-a-real-key-000"


# ── Fakes ────────────────────────────────────────────────────────────────────
class _FakeUsage:
    def __init__(self, i, o):
        self.input_tokens = i
        self.output_tokens = o


class _FakeToolUse:
    def __init__(self, claims):
        self.type = "tool_use"
        self.name = "submit_aml_claims"
        self.input = {"claims": claims}


class _FakeResponse:
    def __init__(self, claims, input_tokens, output_tokens):
        self.content = [_FakeToolUse(claims)]
        self.usage = _FakeUsage(input_tokens, output_tokens)


_VALID_CLAIM = {"claim_type": "prior_sar_history", "asserted_value": "true",
                "evidence_refs": ["prior_cases.customer_id=CUST0001"]}


def _ok_response(input_tokens=900, output_tokens=400):
    return _FakeResponse([dict(_VALID_CLAIM)], input_tokens, output_tokens)


class _FakeClient:
    """Records every ``messages.create`` call; replays a scripted response/exception."""

    def __init__(self, script, on_call=None):
        self.calls: list[dict] = []
        self._script = list(script)
        self._on_call = on_call
        self.messages = self._Messages(self)

    class _Messages:
        def __init__(self, owner):
            self._owner = owner

        def create(self, **kwargs):
            self._owner.calls.append(kwargs)
            if self._owner._on_call is not None:
                self._owner._on_call(kwargs)
            item = self._owner._script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item


def _factory(script, on_call=None, log=None):
    holder = {}

    def make():
        if log is not None:
            log.append("constructed")
        client = _FakeClient(script, on_call)
        holder["client"] = client
        return client

    make.holder = holder
    return make


def _poison_factory():
    def make():
        raise AssertionError("client factory must NOT be called when a gate is missing")
    return make


class _RecordingEnv(dict):
    """A dict that records every ``get`` key access (to prove key-read ordering)."""

    def __init__(self, data):
        super().__init__(data)
        self.accessed: list[str] = []

    def get(self, key, default=None):
        self.accessed.append(key)
        return super().get(key, default)


def _live_env(**over):
    env = {"LUMEN_CAPTURE_LIVE_AI": "1", "ANTHROPIC_API_KEY": FAKE_KEY}
    env.update(over)
    return env


def _paths(tmp_path):
    return {"manifest": str(tmp_path / "manifest.json"), "output_dir": str(tmp_path / "out")}


def _now():
    return "2026-07-19T00:00:00+00:00"


def _run(env, tmp_path, script, **kw):
    p = _paths(tmp_path)
    return live_capture.run_capture(
        env=env, confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
        manifest_path=p["manifest"], output_dir=p["output_dir"],
        client_factory=_factory(script), now_fn=_now, **kw,
    )


def _alert_of(request_kwargs) -> str:
    import re
    m = re.search(r"ALERT\d{3}", request_kwargs["messages"][0]["content"])
    return m.group(0) if m else "?"


# ── Dry run: no key, no network, no writes ───────────────────────────────────
def test_dry_run_plans_exactly_seven_and_fits(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    lines: list[str] = []
    fits = live_capture.dry_run(print_fn=lines.append)
    assert fits is True
    assert live_capture.plan_requests.__module__  # planned
    assert len([p for p in live_capture.plan_requests()]) == 7
    joined = "\n".join(lines)
    for aid in CAPTURE_ALERTS:
        assert aid in joined
    assert CAPTURE_MODEL in joined


def test_dry_run_opens_no_network_and_reads_no_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(socket, "socket", lambda *a, **k: (_ for _ in ()).throw(AssertionError("net")))
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: (_ for _ in ()).throw(AssertionError("net")))
    assert live_capture.dry_run(print_fn=lambda s: None) is True


def test_dry_run_only_prints_nonsensitive_planning(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    lines: list[str] = []
    live_capture.dry_run(print_fn=lines.append)
    joined = "\n".join(lines)
    for secret in ("Dana Whitfield", "You are an AML", "prior_cases.customer_id", "CUST0001"):
        assert secret not in joined


# ── Authorization gates fail before any client is constructed ────────────────
def test_relative_manifest_path_rejected_before_client(tmp_path):
    with pytest.raises(CaptureError):
        live_capture.run_capture(env=_live_env(), confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
                                 manifest_path="relative/manifest.json", output_dir=str(tmp_path / "out"),
                                 client_factory=_poison_factory(), now_fn=_now)


def test_in_repo_manifest_path_rejected(tmp_path):
    with pytest.raises(CaptureError):
        live_capture.run_capture(env=_live_env(), confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
                                 manifest_path=str(ROOT / "manifest.json"), output_dir=str(tmp_path / "out"),
                                 client_factory=_poison_factory(), now_fn=_now)


def test_in_repo_output_dir_rejected(tmp_path):
    with pytest.raises(CaptureError):
        live_capture.run_capture(env=_live_env(), confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
                                 manifest_path=str(tmp_path / "manifest.json"), output_dir=str(ROOT / "out"),
                                 client_factory=_poison_factory(), now_fn=_now)


def test_relative_output_dir_rejected(tmp_path):
    with pytest.raises(CaptureError):
        live_capture.run_capture(env=_live_env(), confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
                                 manifest_path=str(tmp_path / "manifest.json"), output_dir="out",
                                 client_factory=_poison_factory(), now_fn=_now)


def test_missing_flag_gate_fails_before_client(tmp_path):
    p = _paths(tmp_path)
    with pytest.raises(CaptureError):
        live_capture.run_capture(env={"ANTHROPIC_API_KEY": FAKE_KEY}, confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
                                 manifest_path=p["manifest"], output_dir=p["output_dir"],
                                 client_factory=_poison_factory(), now_fn=_now)


def test_wrong_confirmation_phrase_fails_before_client(tmp_path):
    p = _paths(tmp_path)
    with pytest.raises(CaptureError):
        live_capture.run_capture(env=_live_env(), confirmation_phrase="nope",
                                 manifest_path=p["manifest"], output_dir=p["output_dir"],
                                 client_factory=_poison_factory(), now_fn=_now)


def test_missing_api_key_fails_before_client(tmp_path):
    p = _paths(tmp_path)
    with pytest.raises(CapturePolicyError):
        live_capture.run_capture(env={"LUMEN_CAPTURE_LIVE_AI": "1"}, confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
                                 manifest_path=p["manifest"], output_dir=p["output_dir"],
                                 client_factory=_poison_factory(), now_fn=_now)


def test_api_key_read_only_after_other_gates(tmp_path):
    # A bad path fails before ANY env is read.
    env = _RecordingEnv(_live_env())
    with pytest.raises(CaptureError):
        live_capture.run_capture(env=env, confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
                                 manifest_path="relative.json", output_dir=str(tmp_path / "out"),
                                 client_factory=_poison_factory(), now_fn=_now)
    assert "ANTHROPIC_API_KEY" not in env.accessed
    # A full run reads the key only AFTER the capture flag.
    env2 = _RecordingEnv(_live_env())
    _run(env2, tmp_path, [_ok_response() for _ in CAPTURE_ALERTS])
    assert "ANTHROPIC_API_KEY" in env2.accessed
    assert env2.accessed.index("ANTHROPIC_API_KEY") > env2.accessed.index("LUMEN_CAPTURE_LIVE_AI")


# ── Planning / allowlist ─────────────────────────────────────────────────────
def test_exactly_seven_allowlisted_requests_planned():
    plans = live_capture.plan_requests()
    assert [p.alert_id for p in plans] == list(CAPTURE_ALERTS)
    assert len(plans) == 7
    assert all(p.model == CAPTURE_MODEL for p in plans)
    assert all(p.requested_output_tokens == MAX_OUTPUT_TOKENS_PER_REQUEST for p in plans)


def test_eighth_or_arbitrary_alert_cannot_be_attempted():
    budget = authorize_capture_session(_live_env(), REQUIRED_CONFIRMATION_PHRASE, True)
    for aid in CAPTURE_ALERTS:
        budget.authorize_attempt(alert_id=aid, model=CAPTURE_MODEL, request_chars=100,
                                 requested_output_tokens=1, estimated_input_tokens=1)
        budget.record_usage(alert_id=aid, input_tokens=1, output_tokens=1)
    # an 8th attempt (attempt cap) is refused
    with pytest.raises(CapturePolicyError):
        budget.authorize_attempt(alert_id="ALERT001", model=CAPTURE_MODEL, request_chars=100,
                                 requested_output_tokens=1, estimated_input_tokens=1)
    # a non-allowlisted alert is refused by a fresh budget
    budget2 = authorize_capture_session(_live_env(), REQUIRED_CONFIRMATION_PHRASE, True)
    with pytest.raises(CapturePolicyError):
        budget2.authorize_attempt(alert_id="ALERT008", model=CAPTURE_MODEL, request_chars=100,
                                  requested_output_tokens=1, estimated_input_tokens=1)


# ── Happy path: one call per alert, right config, usage recorded ─────────────
def test_seven_captures_one_call_each_with_fixed_config(tmp_path):
    log: list[str] = []
    p = _paths(tmp_path)
    factory = _factory([_ok_response(900, 400) for _ in CAPTURE_ALERTS], log=log)
    summary = live_capture.run_capture(
        env=_live_env(), confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
        manifest_path=p["manifest"], output_dir=p["output_dir"],
        client_factory=factory, now_fn=_now,
    )
    client = factory.holder["client"]
    assert len(client.calls) == 7                                   # one call per alert
    assert sorted(_alert_of(c) for c in client.calls) == list(CAPTURE_ALERTS)
    assert log == ["constructed"]                                   # single client, concurrency 1
    for c in client.calls:
        assert c["model"] == CAPTURE_MODEL
        assert c["max_tokens"] == MAX_OUTPUT_TOKENS_PER_REQUEST     # 700
        assert c["tool_choice"] == {"type": "any"}                 # forced structured tool
        assert len(c["tools"]) == 1                                 # exactly one tool; no web/tool
        assert "thinking" not in c                                 # extended thinking disabled
        assert "fallback" not in c and "models" not in c           # no model fallback
    assert summary["succeeded"] == 7 and summary["halted"] is False
    assert summary["cumulative_input_tokens"] == 6300 and summary["cumulative_output_tokens"] == 2800
    manifest = json.loads(Path(p["manifest"]).read_text())
    assert all(e["status"] == "succeeded" for e in manifest["alerts"].values())
    assert manifest["completed_at"] == _now()


def test_default_client_factory_uses_timeout_30_retries_0(monkeypatch):
    captured = {}

    class _Spy:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", _Spy)
    live_capture._default_client_factory()
    assert captured == {"timeout": 30, "max_retries": 0}


def test_usage_recorded_from_provider_response(tmp_path):
    p = _paths(tmp_path)
    live_capture.run_capture(env=_live_env(), confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
                             manifest_path=p["manifest"], output_dir=p["output_dir"],
                             client_factory=_factory([_ok_response(1234, 321) for _ in CAPTURE_ALERTS]),
                             now_fn=_now)
    manifest = json.loads(Path(p["manifest"]).read_text())
    e = manifest["alerts"]["ALERT001"]
    assert e["actual_input_tokens"] == 1234 and e["actual_output_tokens"] == 321


# ── Overage and failure both halt ────────────────────────────────────────────
def test_actual_overage_is_recorded_then_halts(tmp_path):
    # First response over the per-request output limit (700) -> recorded, then halts.
    script = [_ok_response(900, 800)] + [_ok_response() for _ in range(6)]
    summary = _run(_live_env(), tmp_path, script)
    assert summary["halted"] is True
    assert summary["cumulative_output_tokens"] == 800     # actual over-limit usage recorded
    assert summary["succeeded"] == 1                       # halted after the first
    manifest = json.loads((Path(_paths(tmp_path)["manifest"])).read_text())
    assert sum(1 for e in manifest["alerts"].values() if e["status"] == "planned") == 6


def test_failure_consumes_attempt_and_halts(tmp_path):
    script = [RuntimeError("boom")] + [_ok_response() for _ in range(6)]
    with pytest.raises(CaptureError):
        _run(_live_env(), tmp_path, script)
    manifest = json.loads((Path(_paths(tmp_path)["manifest"])).read_text())
    assert manifest["alerts"]["ALERT001"]["status"] == "failed"
    assert manifest["alerts"]["ALERT001"]["failure_code"] == "error:RuntimeError"
    assert manifest["alerts"]["ALERT002"]["status"] == "planned"     # halted, never attempted


def test_missing_usage_is_a_failure(tmp_path):
    bad = _FakeResponse([dict(_VALID_CLAIM)], None, None)   # no usage
    with pytest.raises(CaptureError):
        _run(_live_env(), tmp_path, [bad] + [_ok_response() for _ in range(6)])
    manifest = json.loads((Path(_paths(tmp_path)["manifest"])).read_text())
    assert manifest["alerts"]["ALERT001"]["status"] == "failed"


# ── Resume / crash safety ────────────────────────────────────────────────────
def test_succeeded_alerts_not_called_again_on_resume(tmp_path):
    p = _paths(tmp_path)
    # First run: all seven succeed.
    live_capture.run_capture(env=_live_env(), confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
                             manifest_path=p["manifest"], output_dir=p["output_dir"],
                             client_factory=_factory([_ok_response() for _ in CAPTURE_ALERTS]), now_fn=_now)
    # Resume: the client must never be called (all succeeded).
    resume = _factory([AssertionError("must not call the API on resume")])
    summary = live_capture.run_capture(env=_live_env(), confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
                                       manifest_path=p["manifest"], output_dir=p["output_dir"],
                                       client_factory=resume, now_fn=_now)
    assert "client" not in resume.holder            # factory never invoked
    assert summary["succeeded"] == 7 and summary["halted"] is False


def _seed_manifest(tmp_path, mutate):
    """Run once (all succeed), then mutate the manifest dict and rewrite it."""
    p = _paths(tmp_path)
    live_capture.run_capture(env=_live_env(), confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
                             manifest_path=p["manifest"], output_dir=p["output_dir"],
                             client_factory=_factory([_ok_response() for _ in CAPTURE_ALERTS]), now_fn=_now)
    m = json.loads(Path(p["manifest"]).read_text())
    mutate(m)
    Path(p["manifest"]).write_text(json.dumps(m))
    return p


def test_pending_manifest_halts_resume(tmp_path):
    def mutate(m):
        m["alerts"]["ALERT003"]["status"] = "pending"
    p = _seed_manifest(tmp_path, mutate)
    with pytest.raises(CaptureError):
        live_capture.run_capture(env=_live_env(), confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
                                 manifest_path=p["manifest"], output_dir=p["output_dir"],
                                 client_factory=_poison_factory(), now_fn=_now)


def test_failed_manifest_halts_resume(tmp_path):
    def mutate(m):
        m["alerts"]["ALERT003"].update(status="failed", failure_code="error:X")
    p = _seed_manifest(tmp_path, mutate)
    with pytest.raises(CaptureError):
        live_capture.run_capture(env=_live_env(), confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
                                 manifest_path=p["manifest"], output_dir=p["output_dir"],
                                 client_factory=_poison_factory(), now_fn=_now)


def test_source_hash_mismatch_halts_resume(tmp_path):
    def mutate(m):
        m["source_data_hash"] = "0" * 64
    p = _seed_manifest(tmp_path, mutate)
    with pytest.raises(CaptureError):
        live_capture.run_capture(env=_live_env(), confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
                                 manifest_path=p["manifest"], output_dir=p["output_dir"],
                                 client_factory=_poison_factory(), now_fn=_now)


def test_git_commit_mismatch_halts_resume(tmp_path):
    def mutate(m):
        m["source_git_commit"] = "deadbeef"
    p = _seed_manifest(tmp_path, mutate)
    with pytest.raises(CaptureError):
        live_capture.run_capture(env=_live_env(), confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
                                 manifest_path=p["manifest"], output_dir=p["output_dir"],
                                 client_factory=_poison_factory(), now_fn=_now)


def test_wrong_model_manifest_halts_resume(tmp_path):
    def mutate(m):
        m["model"] = "claude-something-else"
    p = _seed_manifest(tmp_path, mutate)
    with pytest.raises(CaptureError):
        live_capture.run_capture(env=_live_env(), confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
                                 manifest_path=p["manifest"], output_dir=p["output_dir"],
                                 client_factory=_poison_factory(), now_fn=_now)


def test_malformed_manifest_halts(tmp_path):
    p = _paths(tmp_path)
    Path(p["manifest"]).parent.mkdir(parents=True, exist_ok=True)
    Path(p["manifest"]).write_text("{ not json")
    with pytest.raises(CaptureError):
        live_capture.run_capture(env=_live_env(), confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
                                 manifest_path=p["manifest"], output_dir=p["output_dir"],
                                 client_factory=_poison_factory(), now_fn=_now)


def test_output_hash_mismatch_halts_resume(tmp_path):
    p = _paths(tmp_path)
    live_capture.run_capture(env=_live_env(), confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
                             manifest_path=p["manifest"], output_dir=p["output_dir"],
                             client_factory=_factory([_ok_response() for _ in CAPTURE_ALERTS]), now_fn=_now)
    # Corrupt one output file so its hash no longer matches the manifest.
    out = Path(p["output_dir"]) / "ALERT001.capture.json"
    out.write_text(out.read_text() + "\n// tampered")
    with pytest.raises(CaptureError):
        live_capture.run_capture(env=_live_env(), confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
                                 manifest_path=p["manifest"], output_dir=p["output_dir"],
                                 client_factory=_poison_factory(), now_fn=_now)


def test_crash_safe_pending_recorded_before_each_call(tmp_path):
    p = _paths(tmp_path)
    statuses_at_call: list[str] = []

    def on_call(kwargs):
        m = json.loads(Path(p["manifest"]).read_text())
        statuses_at_call.append(m["alerts"][_alert_of(kwargs)]["status"])

    live_capture.run_capture(env=_live_env(), confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
                             manifest_path=p["manifest"], output_dir=p["output_dir"],
                             client_factory=_factory([_ok_response() for _ in CAPTURE_ALERTS], on_call=on_call),
                             now_fn=_now)
    assert statuses_at_call == ["pending"] * 7      # manifest marked pending before every request


# ── Secret / data hygiene ────────────────────────────────────────────────────
def test_manifest_contains_no_secrets_or_customer_data(tmp_path):
    p = _paths(tmp_path)
    live_capture.run_capture(env=_live_env(), confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
                             manifest_path=p["manifest"], output_dir=p["output_dir"],
                             client_factory=_factory([_ok_response() for _ in CAPTURE_ALERTS]), now_fn=_now)
    text = Path(p["manifest"]).read_text()
    for forbidden in (FAKE_KEY, "You are an AML", "Dana Whitfield", "asserted_value",
                      "prior_cases.customer_id", "authorization", "x-api-key", "CUST0001"):
        assert forbidden not in text


def test_output_contains_no_key_or_headers(tmp_path):
    p = _paths(tmp_path)
    live_capture.run_capture(env=_live_env(), confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
                             manifest_path=p["manifest"], output_dir=p["output_dir"],
                             client_factory=_factory([_ok_response() for _ in CAPTURE_ALERTS]), now_fn=_now)
    text = (Path(p["output_dir"]) / "ALERT001.capture.json").read_text()
    obj = json.loads(text)
    assert obj["model"] == CAPTURE_MODEL and obj["processing_run_id"] == live_capture.CAPTURE_RUN_ID
    assert obj["input_tokens"] == 900 and obj["output_tokens"] == 400
    for forbidden in (FAKE_KEY, "authorization", "x-api-key", "api_key"):
        assert forbidden not in text.lower() if forbidden.islower() else forbidden not in text


def test_failure_code_carries_no_exception_text(tmp_path):
    script = [ValueError("SECRET-detail-in-message")] + [_ok_response() for _ in range(6)]
    with pytest.raises(CaptureError):
        _run(_live_env(), tmp_path, script)
    text = Path(_paths(tmp_path)["manifest"]).read_text()
    assert "SECRET-detail-in-message" not in text
    assert json.loads(text)["alerts"]["ALERT001"]["failure_code"] == "error:ValueError"


# ── No live request occurred anywhere in this module ─────────────────────────
def test_capture_module_and_run_open_no_network(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "socket", lambda *a, **k: (_ for _ in ()).throw(AssertionError("net")))
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: (_ for _ in ()).throw(AssertionError("net")))
    _run(_live_env(), tmp_path, [_ok_response() for _ in CAPTURE_ALERTS])   # fake client, no sockets


def test_normal_app_import_performs_no_capture_or_api_call(monkeypatch):
    monkeypatch.setattr(socket, "socket", lambda *a, **k: (_ for _ in ()).throw(AssertionError("net")))
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: (_ for _ in ()).throw(AssertionError("net")))
    import importlib
    import app  # noqa: F401
    importlib.reload(importlib.import_module("src.live_capture"))   # re-import: no network at import
    # app does not construct an Anthropic client or reference live_capture at startup
    assert not hasattr(app, "live_capture")


def test_committed_csvs_untouched_by_capture(tmp_path):
    def h(name):
        return hashlib.sha256((DATA / name).read_bytes()).hexdigest()
    names = ["ai_outputs.csv", "case_lifecycle.csv", "alerts.csv", "audit_log.csv"]
    before = {n: h(n) for n in names}
    _run(_live_env(), tmp_path, [_ok_response() for _ in CAPTURE_ALERTS])
    assert {n: h(n) for n in names} == before
