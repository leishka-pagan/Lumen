"""Session-preserving Alert Queue click transport (JS-only st.components.v2 component).

The queue keeps its EXACT single HTML table; only the per-cell navigation attributes
change from a session-destroying ``?alert=`` link to ``href="#"`` + ``data-alert-id``,
captured by a JS-only component that returns the clicked id as a trigger. Python
validates the browser-supplied id against the canonical ``alerts_df`` before routing
through the existing ``_open_case_file``.

The click itself executes in the browser, so AppTest cannot simulate it — the transport
is pinned statically here and verified manually in the browser. The DESTINATION
(``open_case`` -> Case File) and the Reset Demo clearing ARE exercised through AppTest.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT))

import app  # noqa: E402

APP = str(ROOT / "app.py")
_SRC = (ROOT / "app.py").read_text(encoding="utf-8")

DISP_PREFIXES = ("disp_evidence_", "disp_draft_", "disp_reason_", "disp_note_", "disp_action_")


def _run(**state):
    at = AppTest.from_file(APP, default_timeout=120)
    for k, v in state.items():
        at.session_state[k] = v
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def _queue_table(at) -> str:
    return next(m.value for m in at.markdown if "lv-table" in m.value)


def _has(at, key) -> bool:
    try:
        at.session_state[key]
        return True
    except KeyError:
        return False


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    """Redirect the runtime CSVs Reset Demo writes to temp copies, so a real Reset Demo
    never mutates a committed data file."""
    for name, env in (("case_lifecycle.csv", "LUMEN_CASE_LIFECYCLE_CSV"),
                      ("pending_overrides.csv", "LUMEN_OVERRIDES_CSV"),
                      ("audit_log.csv", "LUMEN_AUDIT_LOG"),
                      ("ai_outputs.csv", "LUMEN_AI_OUTPUTS_CSV")):
        dst = tmp_path / name
        shutil.copy(DATA / name, dst)
        monkeypatch.setenv(env, str(dst))
    return tmp_path


# ── the session-destroying transport is gone ─────────────────────────────────
def test_no_raw_query_param_navigation_in_source():
    for bad in ('href="?alert=', 'target="_self"',
                'st.query_params.get("alert")', 'st.query_params.pop("alert"'):
        assert bad not in _SRC, f"session-destroying nav still present: {bad}"


def test_queue_anchors_use_hash_href_and_data_alert_id():
    table = _queue_table(_run())
    assert 'href="#"' in table
    assert 'data-alert-id="ALERT001"' in table
    assert 'data-alert-id="ALERT008"' in table
    assert "?alert=" not in table
    assert 'target="_self"' not in table


# ── the JS-only component is wired exactly as specified ──────────────────────
def test_component_registered_once_js_only_with_trigger_and_stable_key():
    assert _SRC.count("st.components.v2.component(") == 1
    assert "js=_QUEUE_CLICK_JS" in _SRC
    for needle in ("setTriggerValue('open_alert'", "preventDefault()",
                   "stopPropagation()", "a[data-alert-id]"):
        assert needle in _SRC, f"component JS missing: {needle}"
    assert 'key="lumen_queue_click_capture"' in _SRC


def test_trigger_is_validated_against_alerts_df_before_routing():
    # browser-supplied id checked against the canonical alerts_df, then routed through the
    # existing _open_case_file — never trusted directly, and selected_alert is preserved.
    assert 'alerts_df["alert_id"].values' in _SRC
    assert "_open_case_file(_clicked_alert)" in _SRC
    assert "st.session_state.selected_alert = _clicked_alert" in _SRC


def test_app_loads_with_the_component_mounted():
    # the v2 component mount does not raise under a full app run
    at = _run()
    assert any("lv-table" in m.value for m in at.markdown)


# ── destination unchanged: the existing open_case path opens the Case File ───
def test_open_case_still_opens_the_case_file():
    at = _run(open_case="ALERT001")
    assert any("case-summary-rail" in m.value for m in at.markdown)


# ── Reset Demo clears session dispositions + the five disp_ families ──────────
def test_reset_demo_clears_session_dispositions_and_disp_widgets(runtime):
    disp = {"evidence_reviewed": True, "draft_disposition": "edited",
            "decision_reason": "r", "final_note": "n", "final_action": "monitor",
            "reviewer": "session:EMP-003"}
    at = _run(view_as="Manager", session_dispositions={"ALERT008": dict(disp)})
    for p in DISP_PREFIXES:                       # seed the five widget-key families
        at.session_state[f"{p}ALERT008"] = "x"
    risk_before = dict(at.session_state["risk_settings"])

    next(b for b in at.button if b.key == "demo_reset").click().run()
    next(b for b in at.button if b.key == "demo_reset_go").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    assert at.session_state["session_dispositions"] == {}
    for p in DISP_PREFIXES:
        assert not _has(at, f"{p}ALERT008"), f"{p} widget state survived reset"
    # unrelated state is untouched
    assert at.session_state["view_as"] == "Manager"
    assert dict(at.session_state["risk_settings"]) == risk_before


def test_reset_demo_copy_states_dispositions_are_cleared():
    assert "session analyst dispositions" in _SRC
