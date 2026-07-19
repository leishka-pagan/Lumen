"""Tests for the deterministic review-routing policy (src/review_routing).

Covers the seven demo-path inputs, the fail-closed rules, strict input validation,
the REQUIRED/NOT_REQUIRED system-action invariants, that overrides are not routing
inputs, that the decision is frozen, and that the module is pure (no network, fs,
env, CSV, Streamlit, SDK, or clock access).
"""

from __future__ import annotations

import dataclasses
import inspect
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.case_lifecycle import AIVerificationStatus as AV, ReviewRoutingStatus as RR  # noqa: E402
from src.review_routing import (  # noqa: E402
    ReviewRoutingDecision, ReviewRoutingError, route_review,
)


def _pass(**kw):
    return route_review(ai_verification=AV.PASS, policy_id="POL-1", **kw)


# ── Seven demo-path results (no alert IDs in production code) ─────────────────
def test_demo_alert001_fail_required():
    d = route_review(ai_verification=AV.FAIL, policy_id="POL-A1")
    assert d.routing is RR.REQUIRED and d.system_action is None
    assert d.reason_codes[0] == "AI_VERIFICATION_FAIL"


def test_demo_alert002_pass_auto_monitor_not_required():
    d = _pass(auto_disposition_action="monitor")
    assert d.routing is RR.NOT_REQUIRED
    assert d.system_action == "monitor"
    assert d.reason_codes == ("AUTO_DISPOSITION_AUTHORIZED",)


def test_demo_alert003_pass_auto_monitor_not_required():
    d = route_review(ai_verification=AV.PASS, policy_id="POL-A3", auto_disposition_action="monitor")
    assert d.routing is RR.NOT_REQUIRED and d.system_action == "monitor"


def test_demo_alert004_pass_mandatory_rule_required():
    d = _pass(mandatory_review_reasons=("RULE_REQUIRES_HUMAN_REVIEW",))
    assert d.routing is RR.REQUIRED and d.system_action is None
    assert d.reason_codes == ("RULE_REQUIRES_HUMAN_REVIEW",)


def test_demo_alert005_pass_auto_monitor_not_required():
    d = route_review(ai_verification=AV.PASS, policy_id="POL-A5", auto_disposition_action="monitor")
    assert d.routing is RR.NOT_REQUIRED and d.system_action == "monitor"


def test_demo_alert006_pass_mandatory_evidence_required():
    d = _pass(mandatory_review_reasons=("CRITICAL_EVIDENCE_MISSING",))
    assert d.routing is RR.REQUIRED and d.system_action is None
    assert d.reason_codes == ("CRITICAL_EVIDENCE_MISSING",)


def test_demo_alert007_mixed_required():
    d = route_review(ai_verification=AV.MIXED, policy_id="POL-A7")
    assert d.routing is RR.REQUIRED and d.system_action is None
    assert d.reason_codes[0] == "AI_VERIFICATION_MIXED"


# ── Fail-closed core behavior ────────────────────────────────────────────────
def test_pass_alone_does_not_become_not_required():
    d = _pass()  # no mandatory reasons, no auto action
    assert d.routing is RR.REQUIRED
    assert d.system_action is None
    assert d.reason_codes == ("NO_AUTO_DISPOSITION_POLICY",)


def test_fail_and_mixed_cannot_carry_auto_disposition():
    with pytest.raises(ReviewRoutingError):
        route_review(ai_verification=AV.FAIL, policy_id="POL-1", auto_disposition_action="monitor")
    with pytest.raises(ReviewRoutingError):
        route_review(ai_verification=AV.MIXED, policy_id="POL-1", auto_disposition_action="monitor")


def test_pass_with_mandatory_reasons_cannot_carry_auto_disposition():
    with pytest.raises(ReviewRoutingError):
        _pass(mandatory_review_reasons=("RULE_X",), auto_disposition_action="monitor")


def test_fail_and_mixed_reason_codes_prefix_and_preserve_mandatory():
    d = route_review(ai_verification=AV.FAIL, policy_id="POL-1",
                     mandatory_review_reasons=("EXTRA_REASON",))
    assert d.reason_codes == ("AI_VERIFICATION_FAIL", "EXTRA_REASON")
    d2 = route_review(ai_verification=AV.MIXED, policy_id="POL-1",
                      mandatory_review_reasons=("EXTRA_REASON",))
    assert d2.reason_codes == ("AI_VERIFICATION_MIXED", "EXTRA_REASON")


def test_not_evaluated_is_rejected():
    with pytest.raises(ReviewRoutingError):
        route_review(ai_verification=AV.NOT_EVALUATED, policy_id="POL-1")


# ── Input validation ─────────────────────────────────────────────────────────
def test_reject_non_enum_ai_verification():
    for bad in ("pass", "PASS", 1, True, 1.0, float("nan"), [], None):
        with pytest.raises(ReviewRoutingError):
            route_review(ai_verification=bad, policy_id="POL-1")


def test_reject_bad_policy_id():
    for bad in ("", "   ", " POL ", 5, 5.0, True, float("nan"), [], None):
        with pytest.raises(ReviewRoutingError):
            route_review(ai_verification=AV.FAIL, policy_id=bad)


def test_reject_bad_mandatory_review_reasons():
    # not a tuple
    with pytest.raises(ReviewRoutingError):
        _pass(mandatory_review_reasons=["RULE_X"])
    # blank / whitespace / surrounding-whitespace / non-str members
    for bad in (("",), ("   ",), (" RULE ",), (5,), (True,), ([],)):
        with pytest.raises(ReviewRoutingError):
            _pass(mandatory_review_reasons=bad)
    # duplicates
    with pytest.raises(ReviewRoutingError):
        _pass(mandatory_review_reasons=("RULE_X", "RULE_X"))


def test_reject_bad_auto_disposition_action():
    for bad in ("", "   ", " monitor ", 5, True, float("nan"), []):
        with pytest.raises(ReviewRoutingError):
            _pass(auto_disposition_action=bad)


# ── REQUIRED / NOT_REQUIRED system-action invariants ─────────────────────────
def test_required_decision_must_have_no_system_action():
    with pytest.raises(ReviewRoutingError):
        ReviewRoutingDecision(routing=RR.REQUIRED, policy_id="POL-1",
                              reason_codes=("X",), system_action="monitor")


def test_not_required_decision_must_have_nonempty_system_action():
    with pytest.raises(ReviewRoutingError):
        ReviewRoutingDecision(routing=RR.NOT_REQUIRED, policy_id="POL-1",
                              reason_codes=("X",), system_action=None)
    with pytest.raises(ReviewRoutingError):
        ReviewRoutingDecision(routing=RR.NOT_REQUIRED, policy_id="POL-1",
                              reason_codes=("X",), system_action="  ")


def test_undetermined_is_not_a_routing_outcome():
    with pytest.raises(ReviewRoutingError):
        ReviewRoutingDecision(routing=RR.UNDETERMINED, policy_id="POL-1",
                              reason_codes=("X",), system_action=None)


# ── Overrides are not routing inputs ─────────────────────────────────────────
def test_overrides_are_not_routing_inputs():
    params = set(inspect.signature(route_review).parameters)
    assert params == {"ai_verification", "policy_id", "mandatory_review_reasons", "auto_disposition_action"}
    assert not any("override" in p for p in params)
    with pytest.raises(TypeError):
        route_review(ai_verification=AV.PASS, policy_id="POL-1", override_status="pending")  # type: ignore[call-arg]


# ── Frozen / deterministic ───────────────────────────────────────────────────
def test_decision_is_frozen():
    d = _pass(auto_disposition_action="monitor")
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.routing = RR.REQUIRED  # type: ignore[misc]


def test_route_review_is_deterministic():
    for _ in range(5):
        assert route_review(ai_verification=AV.PASS, policy_id="POL-1",
                            auto_disposition_action="monitor").routing is RR.NOT_REQUIRED
        assert route_review(ai_verification=AV.FAIL, policy_id="POL-1").routing is RR.REQUIRED


def test_uses_shared_enums_not_duplicates():
    d = route_review(ai_verification=AV.FAIL, policy_id="POL-1")
    assert d.routing is RR.REQUIRED                       # identity: same enum object as case_lifecycle


# ── Purity ───────────────────────────────────────────────────────────────────
def test_module_is_pure_no_forbidden_imports():
    import src.review_routing as rr
    src = inspect.getsource(rr)
    forbidden = [
        "import os", "import socket", "import pandas", "import csv", "import datetime",
        "from datetime", "import time", "import pathlib", "from pathlib", "import requests",
        "import httpx", "import urllib", "import anthropic", "import streamlit", "streamlit",
        "import app", "from app", "open(", "os.environ", "read_csv", "to_csv", ".now(",
    ]
    for token in forbidden:
        assert token not in src, f"module unexpectedly references {token!r}"
    for name in ("os", "socket", "pd", "pandas", "csv", "datetime", "time",
                 "anthropic", "streamlit", "Path", "requests"):
        assert not hasattr(rr, name), f"module unexpectedly bound {name!r}"


def test_no_network_access_during_routing(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("network access attempted by the routing policy")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    route_review(ai_verification=AV.PASS, policy_id="POL-1", auto_disposition_action="monitor")
    route_review(ai_verification=AV.MIXED, policy_id="POL-1")
