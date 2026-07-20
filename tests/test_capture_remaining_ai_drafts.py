"""Tests for the one-time DEMO capture of the 32 remaining alerts (ALERT008..ALERT039).

Every test uses FAKE clients. No test reads a real API key, constructs a real Anthropic
client, or opens a socket. The script under test lives in scripts/ (not a package), so it
is loaded by path.

These tests prove the contract that matters before any money is spent: exactly 32 alerts
are planned, ALERT001..ALERT007 are never called, each remaining alert is requested exactly
once against the fixed model with a 700-token cap, a succeeded alert is never re-called,
any failure halts without retry, no CSV is mutated, and no audit event is written.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import socket
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT))

SCRIPT = ROOT / "scripts" / "capture_remaining_ai_drafts.py"


def _load_script():
    """Load the CLI script by path: scripts/ has no __init__.py, so it is not importable."""
    spec = importlib.util.spec_from_file_location("capture_remaining_ai_drafts", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


capture = _load_script()
CaptureError = capture.CaptureError
REMAINING = capture.REMAINING_ALERTS
FIRST_SEVEN = tuple(f"ALERT{n:03d}" for n in range(1, 8))
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


def _ok_script(n=len(REMAINING)):
    return [_ok_response() for _ in range(n)]


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


def _factory(script, on_call=None):
    holder = {"constructed": 0}

    def make():
        holder["constructed"] += 1
        client = _FakeClient(script, on_call)
        holder["client"] = client
        return client

    make.holder = holder
    return make


def _poison_factory():
    def make():
        raise AssertionError("client factory must NOT be called")
    return make


def _live_env(**over):
    env = {"LUMEN_CAPTURE_LIVE_AI": "1", "ANTHROPIC_API_KEY": FAKE_KEY}
    env.update(over)
    return env


def _paths(tmp_path):
    return {"manifest": str(tmp_path / "manifest.json"), "output_dir": str(tmp_path / "out")}


def _now():
    return "2026-07-19T00:00:00+00:00"


def _run(tmp_path, script, env=None, on_call=None, factory=None, **kw):
    p = _paths(tmp_path)
    f = factory if factory is not None else _factory(script, on_call)
    summary = capture.run_capture(
        env=_live_env() if env is None else env,
        confirmation_phrase=capture.REQUIRED_CONFIRMATION_PHRASE,
        manifest_path=p["manifest"], output_dir=p["output_dir"],
        client_factory=f, now_fn=_now, **kw,
    )
    return summary, f, p


def _manifest(p) -> dict:
    return json.loads(Path(p["manifest"]).read_text(encoding="utf-8"))


def _alerts_in(request_kwargs) -> set:
    return set(re.findall(r"ALERT\d{3}", request_kwargs["messages"][0]["content"]))


def _csv_hashes() -> dict:
    return {f.name: hashlib.sha256(f.read_bytes()).hexdigest()
            for f in sorted(DATA.glob("*.csv"))}


# ── scope + caps ─────────────────────────────────────────────────────────────
def test_scope_is_exactly_alert008_through_alert039():
    assert REMAINING == tuple(f"ALERT{n:03d}" for n in range(8, 40))
    assert len(REMAINING) == 32
    assert REMAINING[0] == "ALERT008" and REMAINING[-1] == "ALERT039"
    assert not (set(REMAINING) & set(FIRST_SEVEN))
    assert set(capture.EXCLUDED_ALERTS) == set(FIRST_SEVEN)


def test_declared_caps_match_the_demo_contract():
    assert capture.REQUIRED_CONFIRMATION_PHRASE == "CAPTURE-32-AUTHORIZED"
    assert capture.MAX_ATTEMPTS == 32
    assert capture.MAX_CUMULATIVE_ESTIMATED_INPUT_TOKENS == 100_000
    assert capture.MAX_CUMULATIVE_REQUESTED_OUTPUT_TOKENS == 22_400 == 32 * 700
    assert capture.CAPTURE_MODEL == "claude-haiku-4-5-20251001"
    assert capture.MAX_OUTPUT_TOKENS_PER_REQUEST == 700
    assert capture.REQUEST_TIMEOUT_SECONDS == 30
    assert capture.MAX_RETRIES == 0
    assert capture.CONCURRENCY == 1


# ── dry-run ──────────────────────────────────────────────────────────────────
def test_dry_run_plans_exactly_32_alerts():
    lines: list[str] = []
    assert capture.dry_run(print_fn=lines.append) is True
    planned = [ln for ln in lines if re.match(r"\s+ALERT\d{3}: request_chars=", ln)]
    assert len(planned) == 32
    ids = [re.search(r"ALERT\d{3}", ln).group(0) for ln in planned]
    assert ids == list(REMAINING)


def test_dry_run_totals_are_within_the_caps():
    lines: list[str] = []
    capture.dry_run(print_fn=lines.append)
    agg = next(ln for ln in lines if ln.startswith("Aggregate:"))
    assert "attempts=32/32" in agg
    assert f"/{capture.MAX_CUMULATIVE_ESTIMATED_INPUT_TOKENS}" in agg
    assert "requested_output_total=22400/22400" in agg
    assert "concurrency=1" in agg and "timeout=30s" in agg and "retries=0" in agg


def test_dry_run_makes_no_request_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "socket", lambda *a, **k: (_ for _ in ()).throw(AssertionError("net")))
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: (_ for _ in ()).throw(AssertionError("net")))
    before = _csv_hashes()
    assert capture.main([]) == 0                       # default mode is dry-run
    assert _csv_hashes() == before
    assert not list(tmp_path.iterdir())


def test_plan_never_includes_the_first_seven_alerts():
    plans = capture.plan_remaining()
    ids = [p.alert_id for p in plans]
    assert len(ids) == 32
    assert not (set(ids) & set(FIRST_SEVEN))
    assert all(p.model == capture.CAPTURE_MODEL for p in plans)
    assert all(p.requested_output_tokens == 700 for p in plans)


# ── request shape: one per alert, fixed model/token cap, never the first seven ──
def test_exactly_one_request_per_remaining_alert(tmp_path):
    summary, f, p = _run(tmp_path, _ok_script())
    calls = f.holder["client"].calls
    assert len(calls) == 32                            # exactly one request per alert
    assert summary["succeeded"] == 32 and summary["attempts"] == 32
    targeted = [sorted(_alerts_in(c))[0] for c in calls]
    assert targeted == list(REMAINING)                 # in order, each exactly once
    assert len(set(targeted)) == 32
    assert f.holder["constructed"] == 1                # sequential: a single client


def test_no_request_ever_mentions_alert001_through_alert007(tmp_path):
    _, f, _ = _run(tmp_path, _ok_script())
    for call in f.holder["client"].calls:
        ids = _alerts_in(call)
        assert len(ids) == 1, f"a request referenced multiple alerts: {sorted(ids)}"
        assert not (ids & set(FIRST_SEVEN)), f"request touched an already-captured alert: {sorted(ids)}"


def test_every_request_uses_the_fixed_model_and_700_token_cap(tmp_path):
    _, f, _ = _run(tmp_path, _ok_script())
    for call in f.holder["client"].calls:
        assert call["model"] == "claude-haiku-4-5-20251001"
        assert call["max_tokens"] == 700
        assert "extended_thinking" not in call and "thinking" not in call


def test_default_client_uses_timeout_30_and_zero_retries(monkeypatch):
    """Proves the client construction contract WITHOUT importing the real SDK."""
    recorded: dict = {}

    class _FakeAnthropic:
        def __init__(self, **kwargs):
            recorded.update(kwargs)

    fake_sdk = types.ModuleType("anthropic")
    fake_sdk.Anthropic = _FakeAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", fake_sdk)
    capture._default_client_factory()
    assert recorded == {"timeout": 30, "max_retries": 0}


def test_outputs_are_one_json_per_alert(tmp_path):
    _, _, p = _run(tmp_path, _ok_script())
    out = Path(p["output_dir"])
    files = sorted(f.name for f in out.iterdir())
    assert files == [f"{a}.capture.json" for a in REMAINING]
    doc = json.loads((out / "ALERT008.capture.json").read_text())
    assert doc["alert_id"] == "ALERT008"
    assert doc["model"] == "claude-haiku-4-5-20251001"
    assert doc["processing_run_id"] == capture.CAPTURE_RUN_ID
    assert doc["input_tokens"] == 900 and doc["output_tokens"] == 400
    assert len(doc["claims"]) == 1


# ── crash safety: pending before the request ─────────────────────────────────
def test_pending_is_written_before_each_request(tmp_path):
    seen: list[tuple[str, str]] = []
    holder: dict = {}

    def on_call(kwargs):
        alert_id = sorted(_alerts_in(kwargs))[0]
        m = json.loads(Path(holder["manifest"]).read_text(encoding="utf-8"))
        seen.append((alert_id, m["alerts"][alert_id]["status"]))

    p = _paths(tmp_path)
    holder["manifest"] = p["manifest"]
    f = _factory(_ok_script(), on_call)
    capture.run_capture(
        env=_live_env(), confirmation_phrase=capture.REQUIRED_CONFIRMATION_PHRASE,
        manifest_path=p["manifest"], output_dir=p["output_dir"],
        client_factory=f, now_fn=_now,
    )
    assert len(seen) == 32
    assert all(status == "pending" for _, status in seen)


# ── resume ───────────────────────────────────────────────────────────────────
def test_resume_never_repeats_a_successful_alert(tmp_path):
    _, f1, p = _run(tmp_path, _ok_script())
    assert len(f1.holder["client"].calls) == 32
    # Second run over the same manifest: everything already succeeded -> no client at all.
    summary = capture.run_capture(
        env=_live_env(), confirmation_phrase=capture.REQUIRED_CONFIRMATION_PHRASE,
        manifest_path=p["manifest"], output_dir=p["output_dir"],
        client_factory=_poison_factory(), now_fn=_now,
    )
    assert summary["succeeded"] == 32
    assert summary["attempts"] == 32          # replayed through the budget, not re-requested


def test_resume_only_requests_the_alerts_still_planned(tmp_path):
    _, _, p = _run(tmp_path, _ok_script())
    # Roll the last two back to 'planned' (as if the run had been interrupted there).
    m = _manifest(p)
    for alert_id in ("ALERT038", "ALERT039"):
        (Path(p["output_dir"]) / f"{alert_id}.capture.json").unlink()
        m["alerts"][alert_id].update(status="planned", actual_input_tokens=None,
                                     actual_output_tokens=None, output_filename=None,
                                     output_sha256=None)
    Path(p["manifest"]).write_text(json.dumps(m, sort_keys=True, indent=2), encoding="utf-8")

    f = _factory(_ok_script(2))
    summary = capture.run_capture(
        env=_live_env(), confirmation_phrase=capture.REQUIRED_CONFIRMATION_PHRASE,
        manifest_path=p["manifest"], output_dir=p["output_dir"],
        client_factory=f, now_fn=_now,
    )
    calls = f.holder["client"].calls
    assert len(calls) == 2                                   # only the two still planned
    assert [sorted(_alerts_in(c))[0] for c in calls] == ["ALERT038", "ALERT039"]
    assert summary["succeeded"] == 32


def test_resume_halts_on_a_tampered_succeeded_output(tmp_path):
    _, _, p = _run(tmp_path, _ok_script())
    target = Path(p["output_dir"]) / "ALERT010.capture.json"
    target.write_text(target.read_text(encoding="utf-8") + "\n// tampered", encoding="utf-8")
    with pytest.raises(CaptureError, match="hash mismatch"):
        capture.run_capture(
            env=_live_env(), confirmation_phrase=capture.REQUIRED_CONFIRMATION_PHRASE,
            manifest_path=p["manifest"], output_dir=p["output_dir"],
            client_factory=_poison_factory(), now_fn=_now,
        )


# ── failure halts, with no retry ─────────────────────────────────────────────
def test_provider_failure_stops_immediately_with_no_retry(tmp_path):
    script = [_ok_response(), _ok_response(), RuntimeError("boom")] + _ok_script(29)
    p = _paths(tmp_path)
    f = _factory(script)
    with pytest.raises(CaptureError, match="capture halted"):
        capture.run_capture(
            env=_live_env(), confirmation_phrase=capture.REQUIRED_CONFIRMATION_PHRASE,
            manifest_path=p["manifest"], output_dir=p["output_dir"],
            client_factory=f, now_fn=_now,
        )
    calls = f.holder["client"].calls
    assert len(calls) == 3                                   # failed once, never retried
    m = _manifest(p)
    assert m["alerts"]["ALERT008"]["status"] == "succeeded"
    assert m["alerts"]["ALERT009"]["status"] == "succeeded"
    assert m["alerts"]["ALERT010"]["status"] == "failed"
    assert m["alerts"]["ALERT010"]["failure_code"] == "error:RuntimeError"
    assert sum(1 for e in m["alerts"].values() if e["status"] == "planned") == 29
    assert not (Path(p["output_dir"]) / "ALERT010.capture.json").exists()


def test_a_failed_manifest_halts_on_resume_without_calling(tmp_path):
    script = [RuntimeError("boom")] + _ok_script(31)
    p = _paths(tmp_path)
    with pytest.raises(CaptureError):
        capture.run_capture(
            env=_live_env(), confirmation_phrase=capture.REQUIRED_CONFIRMATION_PHRASE,
            manifest_path=p["manifest"], output_dir=p["output_dir"],
            client_factory=_factory(script), now_fn=_now,
        )
    with pytest.raises(CaptureError, match="previously failed"):
        capture.run_capture(
            env=_live_env(), confirmation_phrase=capture.REQUIRED_CONFIRMATION_PHRASE,
            manifest_path=p["manifest"], output_dir=p["output_dir"],
            client_factory=_poison_factory(), now_fn=_now,
        )


def test_invalid_response_stops_the_run(tmp_path):
    """An unusable provider response (zero valid claims) must never be stored as success."""
    script = [_FakeResponse([], 900, 400)] + _ok_script(31)
    p = _paths(tmp_path)
    f = _factory(script)
    with pytest.raises(CaptureError, match="capture halted"):
        capture.run_capture(
            env=_live_env(), confirmation_phrase=capture.REQUIRED_CONFIRMATION_PHRASE,
            manifest_path=p["manifest"], output_dir=p["output_dir"],
            client_factory=f, now_fn=_now,
        )
    assert len(f.holder["client"].calls) == 1
    m = _manifest(p)
    assert m["alerts"]["ALERT008"]["status"] == "failed"
    assert m["alerts"]["ALERT008"]["failure_code"] == "error:CaptureResponseError"
    assert not (Path(p["output_dir"]) / "ALERT008.capture.json").exists()


def test_missing_usage_stops_the_run(tmp_path):
    script = [_FakeResponse([dict(_VALID_CLAIM)], None, None)] + _ok_script(31)
    p = _paths(tmp_path)
    with pytest.raises(CaptureError, match="capture halted"):
        capture.run_capture(
            env=_live_env(), confirmation_phrase=capture.REQUIRED_CONFIRMATION_PHRASE,
            manifest_path=p["manifest"], output_dir=p["output_dir"],
            client_factory=_factory(script), now_fn=_now,
        )
    assert _manifest(p)["alerts"]["ALERT008"]["status"] == "failed"


def test_a_pending_manifest_halts_on_resume(tmp_path):
    _, _, p = _run(tmp_path, _ok_script())
    m = _manifest(p)
    m["alerts"]["ALERT020"]["status"] = "pending"
    Path(p["manifest"]).write_text(json.dumps(m, sort_keys=True, indent=2), encoding="utf-8")
    with pytest.raises(CaptureError, match="pending"):
        capture.run_capture(
            env=_live_env(), confirmation_phrase=capture.REQUIRED_CONFIRMATION_PHRASE,
            manifest_path=p["manifest"], output_dir=p["output_dir"],
            client_factory=_poison_factory(), now_fn=_now,
        )


def test_a_malformed_manifest_halts_on_resume(tmp_path):
    p = _paths(tmp_path)
    Path(p["output_dir"]).mkdir(parents=True, exist_ok=True)
    Path(p["manifest"]).write_text("{not json", encoding="utf-8")
    with pytest.raises(CaptureError, match="malformed"):
        capture.run_capture(
            env=_live_env(), confirmation_phrase=capture.REQUIRED_CONFIRMATION_PHRASE,
            manifest_path=p["manifest"], output_dir=p["output_dir"],
            client_factory=_poison_factory(), now_fn=_now,
        )


# ── gates: every refusal happens before a client exists ──────────────────────
@pytest.mark.parametrize("env,confirm", [
    ({}, "CAPTURE-32-AUTHORIZED"),                                   # no env flag
    ({"LUMEN_CAPTURE_LIVE_AI": "0", "ANTHROPIC_API_KEY": FAKE_KEY}, "CAPTURE-32-AUTHORIZED"),
    ({"LUMEN_CAPTURE_LIVE_AI": "1", "ANTHROPIC_API_KEY": FAKE_KEY}, "CAPTURE-7-AUTHORIZED"),
    ({"LUMEN_CAPTURE_LIVE_AI": "1", "ANTHROPIC_API_KEY": FAKE_KEY}, "capture-32-authorized"),
    ({"LUMEN_CAPTURE_LIVE_AI": "1", "ANTHROPIC_API_KEY": FAKE_KEY}, ""),
    ({"LUMEN_CAPTURE_LIVE_AI": "1"}, "CAPTURE-32-AUTHORIZED"),       # no API key present
])
def test_missing_gate_refuses_before_any_client(tmp_path, env, confirm):
    p = _paths(tmp_path)
    with pytest.raises(CaptureError):
        capture.run_capture(
            env=env, confirmation_phrase=confirm,
            manifest_path=p["manifest"], output_dir=p["output_dir"],
            client_factory=_poison_factory(), now_fn=_now,
        )
    assert not Path(p["manifest"]).exists()


@pytest.mark.parametrize("bad", ["", "relative/path.json", "./x.json"])
def test_relative_or_empty_paths_are_refused(tmp_path, bad):
    with pytest.raises(CaptureError):
        capture.run_capture(
            env=_live_env(), confirmation_phrase=capture.REQUIRED_CONFIRMATION_PHRASE,
            manifest_path=bad, output_dir=str(tmp_path / "out"),
            client_factory=_poison_factory(), now_fn=_now,
        )


def test_paths_inside_the_repository_are_refused(tmp_path):
    for manifest, out in [(str(ROOT / "m.json"), str(tmp_path / "out")),
                          (str(tmp_path / "m.json"), str(ROOT / "data"))]:
        with pytest.raises(CaptureError, match="OUTSIDE"):
            capture.run_capture(
                env=_live_env(), confirmation_phrase=capture.REQUIRED_CONFIRMATION_PHRASE,
                manifest_path=manifest, output_dir=out,
                client_factory=_poison_factory(), now_fn=_now,
            )


def test_budget_refuses_an_already_captured_alert():
    budget = capture._Budget()
    for alert_id in FIRST_SEVEN:
        with pytest.raises(CaptureError, match="already captured"):
            budget.authorize_attempt(alert_id=alert_id, model=capture.CAPTURE_MODEL,
                                     request_chars=100, estimated_input_tokens=10,
                                     requested_output_tokens=700)
    assert budget.attempts == 0


def test_budget_stops_at_32_attempts():
    budget = capture._Budget()
    for alert_id in REMAINING:
        budget.authorize_attempt(alert_id=alert_id, model=capture.CAPTURE_MODEL,
                                 request_chars=100, estimated_input_tokens=10,
                                 requested_output_tokens=700)
    assert budget.attempts == 32
    assert budget.requested_output_tokens == 22_400          # exactly the cumulative cap
    with pytest.raises(CaptureError):
        budget.authorize_attempt(alert_id="ALERT039", model=capture.CAPTURE_MODEL,
                                 request_chars=100, estimated_input_tokens=10,
                                 requested_output_tokens=700)


def test_budget_refuses_a_different_model():
    budget = capture._Budget()
    with pytest.raises(CaptureError, match="model is fixed"):
        budget.authorize_attempt(alert_id="ALERT008", model="claude-3-opus-20240229",
                                 request_chars=100, estimated_input_tokens=10,
                                 requested_output_tokens=700)


# ── no network, no CSV mutation, no audit, no secrets ────────────────────────
def test_no_network_is_opened_anywhere(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "socket", lambda *a, **k: (_ for _ in ()).throw(AssertionError("net")))
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: (_ for _ in ()).throw(AssertionError("net")))
    _load_script()                                   # re-import opens no network
    capture.dry_run(print_fn=lambda _: None)
    summary, _, _ = _run(tmp_path, _ok_script())
    assert summary["succeeded"] == 32


def test_no_committed_csv_is_mutated(tmp_path):
    before = _csv_hashes()
    _run(tmp_path, _ok_script())
    assert _csv_hashes() == before


def test_no_audit_event_is_written(tmp_path, monkeypatch):
    import src.audit as audit_mod
    calls: list = []
    monkeypatch.setattr(audit_mod, "log_event", lambda **kw: calls.append(kw) or {})
    _run(tmp_path, _ok_script())
    assert calls == []


def test_manifest_and_summary_never_contain_key_prompt_or_response(tmp_path):
    summary, _, p = _run(tmp_path, _ok_script())
    manifest_text = Path(p["manifest"]).read_text(encoding="utf-8")
    for blob in (manifest_text, json.dumps(summary)):
        assert FAKE_KEY not in blob
        assert "sk-ant" not in blob
        assert "ANTHROPIC_API_KEY" not in blob
        assert "You are" not in blob                 # no system/user prompt text
        assert "submit_aml_claims" not in blob       # no raw request/tool payload
    m = _manifest(p)
    assert set(m["alerts"]) == set(REMAINING)
    assert m["model"] == "claude-haiku-4-5-20251001"
    assert m["completed_at"] is not None


def test_failure_code_carries_no_exception_message(tmp_path):
    script = [RuntimeError("SECRET-DETAIL-should-not-persist")] + _ok_script(31)
    p = _paths(tmp_path)
    with pytest.raises(CaptureError) as exc:
        capture.run_capture(
            env=_live_env(), confirmation_phrase=capture.REQUIRED_CONFIRMATION_PHRASE,
            manifest_path=p["manifest"], output_dir=p["output_dir"],
            client_factory=_factory(script), now_fn=_now,
        )
    assert "SECRET-DETAIL" not in str(exc.value)
    assert "SECRET-DETAIL" not in Path(p["manifest"]).read_text(encoding="utf-8")
