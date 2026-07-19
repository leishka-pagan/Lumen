"""Safe "Reset Demo" control: restores ONLY pending_overrides.csv and audit_log.csv
from committed baseline snapshots, atomically, behind a confirmation dialog.

Isolation: the app's runtime CSVs are redirected to temp files via env vars (the
`runtime` fixture); the baseline stays the committed read-only snapshot; unit tests
pass explicit temp paths. No committed CSV is ever mutated.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
BASELINE = DATA / "demo_baseline"
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
from src.demo_reset import reset_override_demo, validate_baseline_pending, DemoResetError  # noqa: E402

APP = str(ROOT / "app.py")


def _pending(path):
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def _btn(at, key):
    return next(b for b in at.button if b.key == key)


def _run(**state):
    at = AppTest.from_file(APP, default_timeout=120)
    for k, v in state.items():
        at.session_state[k] = v
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    """Temp runtime CSVs seeded with a MUTATED decision; env-redirected so the app
    uses them. The baseline stays the committed read-only snapshot."""
    ov = tmp_path / "pending_overrides.csv"
    lg = tmp_path / "audit_log.csv"
    shutil.copy(BASELINE / "pending_overrides.csv", ov)
    df = _pending(ov)
    df.loc[0, "status"] = "rejected"
    df.loc[0, "reviewed_by"] = "S. Mayekar"
    df.loc[0, "review_note"] = "Test 4"
    df.to_csv(ov, index=False)
    lg.write_text('log_id,timestamp,actor,action,alert_id,details_json\n'
                  'LOG-x,t,ui:X,override_review,A,"{}"\n', encoding="utf-8")
    monkeypatch.setenv("LUMEN_OVERRIDES_CSV", str(ov))
    monkeypatch.setenv("LUMEN_AUDIT_LOG", str(lg))
    return {"pending": ov, "audit": lg}


# 1
def test_reset_button_renders_in_yellow_banner(runtime):
    at = _run()
    assert "demo_reset" in {b.key for b in at.button if b.key}
    md = " ".join(m.value for m in at.markdown)
    assert "role-bar" in md and "Demo mode" in md          # rendered with the yellow banner


# 2
def test_clicking_opens_dialog_and_changes_no_data(runtime):
    at = _run()
    before = runtime["pending"].read_bytes()
    _btn(at, "demo_reset").click().run()
    assert at.session_state["demo_reset_confirm"] is True
    md = " ".join(m.value for m in at.markdown)
    assert "restores the three override requests to Pending" in md
    assert {"demo_reset_cancel", "demo_reset_go"} <= {b.key for b in at.button if b.key}
    assert runtime["pending"].read_bytes() == before        # opening writes nothing


# 3
def test_cancel_changes_no_data_or_audit(runtime):
    at = _run()
    before_p, before_a = runtime["pending"].read_bytes(), runtime["audit"].read_bytes()
    _btn(at, "demo_reset").click().run()
    _btn(at, "demo_reset_cancel").click().run()
    assert at.session_state["demo_reset_confirm"] is False   # dialog closed
    assert runtime["pending"].read_bytes() == before_p
    assert runtime["audit"].read_bytes() == before_a


# 4 + 5
def test_confirm_restores_three_pending_blank_fields(runtime):
    at = _run()
    assert (_pending(runtime["pending"])["status"] == "pending").sum() == 2   # seeded mutated
    _btn(at, "demo_reset").click().run()
    _btn(at, "demo_reset_go").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    df = _pending(runtime["pending"])
    assert len(df) == 3 and (df["status"] == "pending").sum() == 3
    assert (df["reviewed_by"] == "").all()
    assert (df["reviewed_at"] == "").all()
    assert (df["review_note"] == "").all()


# 6
def test_confirm_restores_baseline_audit_removing_test_events(runtime):
    at = _run()
    _btn(at, "demo_reset").click().run()
    _btn(at, "demo_reset_go").click().run()
    assert runtime["audit"].read_bytes() == (BASELINE / "audit_log.csv").read_bytes()
    assert "Test" not in runtime["audit"].read_text(encoding="utf-8")


# 7
def test_reset_touches_only_the_two_runtime_files(tmp_path):
    ov = tmp_path / "po.csv"; lg = tmp_path / "audit.csv"; other = tmp_path / "other.csv"
    shutil.copy(BASELINE / "pending_overrides.csv", ov)
    lg.write_text("stale\n", encoding="utf-8")
    other.write_text("SENTINEL\n", encoding="utf-8")
    reset_override_demo(ov, lg, BASELINE / "pending_overrides.csv", BASELINE / "audit_log.csv")
    assert other.read_text(encoding="utf-8") == "SENTINEL\n"                  # untouched
    # committed runtime CSVs never touched
    assert (pd.read_csv(DATA / "pending_overrides.csv", dtype=str, keep_default_na=False)["status"] == "pending").all()


# 8
def test_manager_role_preserved(runtime):
    at = _run(view_as="Manager")
    _btn(at, "demo_reset").click().run()
    _btn(at, "demo_reset_go").click().run()
    assert at.session_state["view_as"] == "Manager"


# 9
def test_view_becomes_override_requests(runtime):
    at = _run(view_as="Manager")
    _btn(at, "demo_reset").click().run()
    _btn(at, "demo_reset_go").click().run()
    assert at.session_state["manager_review_view"] == "override_requests"


# 10
def test_stale_dialog_and_case_state_cleared(runtime):
    at = _run(view_as="Manager",
              pending_override={"change_id": "CHG-SEED-001", "decision": "approved"},
              selected_alert="ALERT002")
    _btn(at, "demo_reset").click().run()
    _btn(at, "demo_reset_go").click().run()
    assert at.session_state["pending_override"] is None
    assert at.session_state["selected_alert"] is None
    assert at.session_state["open_case"] is None


# 11
def test_success_toast_text_exact(runtime):
    at = _run(view_as="Manager")
    _btn(at, "demo_reset").click().run()
    _btn(at, "demo_reset_go").click().run()
    assert any(t.value == "Demo reset complete: 3 override requests restored." for t in at.toast)


# validation: baseline is validated and the reset fails closed (nothing replaced)
def test_reset_validates_baseline_and_fails_closed(tmp_path):
    bad = tmp_path / "bad_baseline.csv"
    df = _pending(BASELINE / "pending_overrides.csv")
    df.loc[0, "status"] = "approved"
    df.to_csv(bad, index=False)
    with pytest.raises(DemoResetError):
        validate_baseline_pending(bad)
    ov = tmp_path / "po.csv"
    shutil.copy(BASELINE / "pending_overrides.csv", ov)
    before = ov.read_bytes()
    with pytest.raises(DemoResetError):
        reset_override_demo(ov, tmp_path / "a.csv", bad, BASELINE / "audit_log.csv")
    assert ov.read_bytes() == before                          # runtime untouched on invalid baseline
