"""The disposition form is gated on routing: it appears only where review is REQUIRED.

NOT_REQUIRED means the deterministic routing policy already authorized a system
disposition, so offering a review form there would contradict the routing decision the
gate exists to enforce.

These tests assert behaviour, not rendering. The production change is a five-line diff
reviewable on its own; the job here is to pin the rule, not to re-prove that unrelated
markup is unchanged. Nothing below compares whole renders, asserts styling, or depends
on repository history — all of which would break during the upcoming visual redesign.

The NOT_REQUIRED set is derived at runtime from ``app.lifecycle_index``, the very index
``show_case_dialog`` reads, so the tests follow the data instead of hard-coding it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
from src.case_lifecycle import ReviewRoutingStatus  # noqa: E402

APP = str(ROOT / "app.py")

SUBMIT_LABEL = "Record disposition"
REQUIRED_ALERT = "ALERT008"          # REQUIRED routing, no stored review


def _not_required_alerts() -> list[str]:
    """Alerts whose canonical routing is NOT_REQUIRED, from the app's own lifecycle index."""
    return sorted(a for a, r in app.lifecycle_index.items()
                  if r.review_routing is ReviewRoutingStatus.NOT_REQUIRED)


def _open(alert_id: str, role: str = "Analyst") -> AppTest:
    at = AppTest.from_file(APP, default_timeout=120)
    at.session_state["view_as"] = role
    at.session_state["open_case"] = alert_id
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def _has_form(at: AppTest) -> bool:
    return any(b.label == SUBMIT_LABEL for b in at.button)


def _md(at: AppTest) -> str:
    return " ".join(m.value for m in at.markdown)


NOT_REQUIRED_ALERTS = _not_required_alerts()


# ── the set under test comes from the app's own lifecycle index ──────────────
def test_not_required_set_comes_from_the_apps_lifecycle_index():
    assert NOT_REQUIRED_ALERTS, "no NOT_REQUIRED alerts found — gating would be untested"
    for alert_id in NOT_REQUIRED_ALERTS:
        record = app.lifecycle_index[alert_id]
        assert record.review_routing is ReviewRoutingStatus.NOT_REQUIRED


def test_alert003_and_alert005_are_not_required():
    assert {"ALERT003", "ALERT005"} <= set(NOT_REQUIRED_ALERTS)


# ── NOT_REQUIRED alerts offer no disposition form ───────────────────────────
@pytest.mark.parametrize("alert_id", NOT_REQUIRED_ALERTS)
def test_not_required_alert_offers_no_disposition_form(alert_id):
    at = _open(alert_id)
    assert not _has_form(at), f"{alert_id} is NOT_REQUIRED but exposed the submit control"
    assert SUBMIT_LABEL not in _md(at)
    assert not [w for w in at.text_area
                if getattr(w, "form_id", "").startswith("disposition_form")]


@pytest.mark.parametrize("alert_id", NOT_REQUIRED_ALERTS)
def test_not_required_alert_keeps_its_not_applicable_gate(alert_id):
    """Smallest stable marker that the existing NOT_REQUIRED state still renders."""
    assert "HUMAN-REVIEW GATE: NOT APPLICABLE" in _md(_open(alert_id))


def test_pending_override_does_not_reveal_the_form():
    """ALERT005 carries a pending override; that must not re-open the analyst path."""
    assert app.lifecycle_index["ALERT005"].override_status.value == "pending"
    assert not _has_form(_open("ALERT005"))


# ── positive counterpart: REQUIRED routing still shows the form ──────────────
def test_required_alert_without_stored_review_still_shows_the_form():
    assert app.lifecycle_index[REQUIRED_ALERT].review_routing is ReviewRoutingStatus.REQUIRED
    assert _has_form(_open(REQUIRED_ALERT))
