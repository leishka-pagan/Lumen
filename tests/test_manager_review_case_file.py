"""Manager Review → Case File routing — meaningful Streamlit AppTest coverage.

Proves the "Open Case File" control renders for a pending override and that
activating it opens the correct alert's Case File dialog. The pure guard helper
``app.case_target_for_override`` is retained because production genuinely uses it to
hide the control for blank/dangling alert IDs; it gets one focused unit test.
"""

from __future__ import annotations

import sys
from pathlib import Path

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app  # noqa: E402

APP = str(ROOT / "app.py")


def test_open_case_control_guard_rejects_blank_and_dangling():
    # Production calls this to decide whether to render the control at all.
    assert app.case_target_for_override({"alert_id": "ALERT001"}, ["ALERT001"]) == "ALERT001"
    assert app.case_target_for_override({"alert_id": ""}, ["ALERT001"]) is None
    assert app.case_target_for_override({"alert_id": "GHOST"}, ["ALERT001"]) is None


def test_manager_review_open_case_file_opens_correct_case():
    at = AppTest.from_file(APP, default_timeout=120)
    at.session_state["view_as"] = "Manager"
    at.run()
    assert not at.exception, f"Manager Review raised: {[str(e.value) for e in at.exception]}"
    # Pending overrides now live in the "Override Requests" subview; switch to it.
    at.segmented_control(key="manager_review_view").set_value("override_requests").run()
    assert not at.exception, f"Override Requests view raised: {[str(e.value) for e in at.exception]}"
    # 1. an "Open Case File" control renders for pending overrides
    oc = [b for b in at.button if b.key and b.key.startswith("oc_")]
    assert oc, "no 'Open Case File' control rendered for pending overrides"
    # 2 & 3. activating the control for the ALERT001 override (CHG-SEED-003) opens
    #        ALERT001's Case File — proven by the customer name, which appears only
    #        in the opened dialog, not on the override card.
    btn = next(b for b in at.button if b.key == "oc_CHG-SEED-003")
    btn.click().run()
    assert not at.exception, f"Open Case File raised: {[str(e.value) for e in at.exception]}"
    md = " ".join(m.value for m in at.markdown)
    assert "Dana Whitfield" in md, "Open Case File did not open the ALERT001 (CUST0001) case"
