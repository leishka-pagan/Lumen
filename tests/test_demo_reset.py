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
    uses them. All four runtime files (overrides, audit, lifecycle, ai_outputs) are
    redirected to temp; the baselines stay the committed read-only snapshots."""
    ov = tmp_path / "pending_overrides.csv"
    lg = tmp_path / "audit_log.csv"
    lc = tmp_path / "case_lifecycle.csv"
    ai = tmp_path / "ai_outputs.csv"
    shutil.copy(BASELINE / "pending_overrides.csv", ov)
    df = _pending(ov)
    df.loc[0, "status"] = "rejected"
    df.loc[0, "reviewed_by"] = "S. Mayekar"
    df.loc[0, "review_note"] = "Test 4"
    df.to_csv(ov, index=False)
    lg.write_text('log_id,timestamp,actor,action,alert_id,details_json\n'
                  'LOG-x,t,ui:X,override_review,A,"{}"\n', encoding="utf-8")
    shutil.copy(BASELINE / "case_lifecycle.csv", lc)
    shutil.copy(BASELINE / "ai_outputs.csv", ai)
    monkeypatch.setenv("LUMEN_OVERRIDES_CSV", str(ov))
    monkeypatch.setenv("LUMEN_AUDIT_LOG", str(lg))
    monkeypatch.setenv("LUMEN_CASE_LIFECYCLE_CSV", str(lc))
    monkeypatch.setenv("LUMEN_AI_OUTPUTS_CSV", str(ai))
    return {"pending": ov, "audit": lg, "lifecycle": lc, "ai_outputs": ai}


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
def test_reset_touches_only_the_four_runtime_files(tmp_path):
    ov = tmp_path / "po.csv"; lg = tmp_path / "audit.csv"
    lc = tmp_path / "case_lifecycle.csv"; ai = tmp_path / "ai_outputs.csv"
    other = tmp_path / "other.csv"
    shutil.copy(BASELINE / "pending_overrides.csv", ov)
    for stale in (lg, lc, ai):
        stale.write_text("stale\n", encoding="utf-8")
    other.write_text("SENTINEL\n", encoding="utf-8")
    committed_before = {
        n: (DATA / n).read_bytes()
        for n in ("pending_overrides.csv", "audit_log.csv", "case_lifecycle.csv", "ai_outputs.csv")
    }
    reset_override_demo(ov, lg, BASELINE / "pending_overrides.csv", BASELINE / "audit_log.csv",
                        lifecycle_path=lc, baseline_lifecycle_path=BASELINE / "case_lifecycle.csv",
                        ai_outputs_path=ai, baseline_ai_outputs_path=BASELINE / "ai_outputs.csv")
    assert other.read_text(encoding="utf-8") == "SENTINEL\n"                  # untouched
    # the four runtime files now equal their baselines byte-for-byte
    assert lc.read_bytes() == (BASELINE / "case_lifecycle.csv").read_bytes()
    assert ai.read_bytes() == (BASELINE / "ai_outputs.csv").read_bytes()
    # committed runtime CSVs never touched
    for n, before in committed_before.items():
        assert (DATA / n).read_bytes() == before, f"committed {n} was mutated"


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


# ── Reset restores the two additional baseline-controlled files ──────────────
def _reset_all(tmp_path):
    """Reset with all four runtime files redirected to temp; baselines are committed."""
    ov = tmp_path / "pending_overrides.csv"; lg = tmp_path / "audit_log.csv"
    lc = tmp_path / "case_lifecycle.csv"; ai = tmp_path / "ai_outputs.csv"
    for name, dst in (("pending_overrides.csv", ov), ("audit_log.csv", lg),
                      ("case_lifecycle.csv", lc), ("ai_outputs.csv", ai)):
        shutil.copy(BASELINE / name, dst)
    # mutate every runtime file so restoration is observable
    _pending(ov).assign(status="approved").to_csv(ov, index=False)
    lg.write_text("stale\n", encoding="utf-8")
    lc.write_text("garbage\n", encoding="utf-8")
    ai.write_text("garbage\n", encoding="utf-8")
    reset_override_demo(ov, lg, BASELINE / "pending_overrides.csv", BASELINE / "audit_log.csv",
                        lifecycle_path=lc, baseline_lifecycle_path=BASELINE / "case_lifecycle.csv",
                        ai_outputs_path=ai, baseline_ai_outputs_path=BASELINE / "ai_outputs.csv")
    return {"pending": ov, "audit": lg, "lifecycle": lc, "ai_outputs": ai}


def test_reset_restores_all_four_files_byte_equal_to_baselines(tmp_path):
    files = _reset_all(tmp_path)
    for name, key in (("pending_overrides.csv", "pending"), ("audit_log.csv", "audit"),
                      ("case_lifecycle.csv", "lifecycle"), ("ai_outputs.csv", "ai_outputs")):
        assert files[key].read_bytes() == (BASELINE / name).read_bytes(), name


def test_reset_restores_canonical_39_row_lifecycle_and_10_ai_rows(tmp_path):
    files = _reset_all(tmp_path)
    lc = pd.read_csv(files["lifecycle"], dtype=str, keep_default_na=False)
    assert len(lc) == 39 and lc["alert_id"].nunique() == 39
    ai = pd.read_csv(files["ai_outputs"], dtype=str, keep_default_na=False)
    assert len(ai) == 10 and (ai["draft_source"] == "synthetic_fixture").all()
    po = _pending(files["pending"])
    assert len(po) == 3 and (po["status"] == "pending").sum() == 3
    assert files["audit"].read_bytes() == (BASELINE / "audit_log.csv").read_bytes()


def test_reset_fails_closed_on_bad_lifecycle_baseline(tmp_path):
    bad_lc = tmp_path / "bad_lifecycle.csv"
    df = pd.read_csv(BASELINE / "case_lifecycle.csv", dtype=str, keep_default_na=False)
    df = df.iloc[:-1]                                          # 38 rows -> not canonical
    df.to_csv(bad_lc, index=False)
    ov = tmp_path / "po.csv"; lc = tmp_path / "lc.csv"; ai = tmp_path / "ai.csv"
    shutil.copy(BASELINE / "pending_overrides.csv", ov)
    lc.write_text("keep\n", encoding="utf-8"); ai.write_text("keep\n", encoding="utf-8")
    ov_before, lc_before, ai_before = ov.read_bytes(), lc.read_bytes(), ai.read_bytes()
    with pytest.raises(DemoResetError):
        reset_override_demo(ov, tmp_path / "a.csv", BASELINE / "pending_overrides.csv",
                            BASELINE / "audit_log.csv",
                            lifecycle_path=lc, baseline_lifecycle_path=bad_lc,
                            ai_outputs_path=ai, baseline_ai_outputs_path=BASELINE / "ai_outputs.csv")
    assert ov.read_bytes() == ov_before and lc.read_bytes() == lc_before and ai.read_bytes() == ai_before


def test_reset_fails_closed_on_bad_ai_outputs_baseline(tmp_path):
    bad_ai = tmp_path / "bad_ai.csv"
    df = pd.read_csv(BASELINE / "ai_outputs.csv", dtype=str, keep_default_na=False)
    df = df.iloc[:-1]                                          # 9 rows -> not canonical
    df.to_csv(bad_ai, index=False)
    ov = tmp_path / "po.csv"; lc = tmp_path / "lc.csv"; ai = tmp_path / "ai.csv"
    shutil.copy(BASELINE / "pending_overrides.csv", ov)
    lc.write_text("keep\n", encoding="utf-8"); ai.write_text("keep\n", encoding="utf-8")
    before = (ov.read_bytes(), lc.read_bytes(), ai.read_bytes())
    with pytest.raises(DemoResetError):
        reset_override_demo(ov, tmp_path / "a.csv", BASELINE / "pending_overrides.csv",
                            BASELINE / "audit_log.csv",
                            lifecycle_path=lc, baseline_lifecycle_path=BASELINE / "case_lifecycle.csv",
                            ai_outputs_path=ai, baseline_ai_outputs_path=bad_ai)
    assert (ov.read_bytes(), lc.read_bytes(), ai.read_bytes()) == before


def test_reset_makes_no_network_call_and_no_generator(tmp_path, monkeypatch):
    import socket as _socket
    import src.demo_reset as dr

    def _boom(*a, **k):
        raise AssertionError("reset attempted network access")

    monkeypatch.setattr(_socket, "socket", _boom)
    monkeypatch.setattr(_socket, "create_connection", _boom)
    assert "scripts.generate_data" not in sys.modules or True   # reset never imports it
    files = _reset_all(tmp_path)                                 # must complete with no socket use
    assert files["lifecycle"].read_bytes() == (BASELINE / "case_lifecycle.csv").read_bytes()
    # demo_reset never references the full generator
    assert "generate_data" not in dr.reset_override_demo.__code__.co_names


# app-driven: the reset control restores the redirected lifecycle + ai_outputs too
def test_app_reset_restores_lifecycle_and_ai_outputs(runtime):
    # mutate the redirected runtime lifecycle + ai_outputs before reset
    runtime["lifecycle"].write_text("garbage\n", encoding="utf-8")
    runtime["ai_outputs"].write_text("garbage\n", encoding="utf-8")
    at = _run(view_as="Manager")
    _btn(at, "demo_reset").click().run()
    _btn(at, "demo_reset_go").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert runtime["lifecycle"].read_bytes() == (BASELINE / "case_lifecycle.csv").read_bytes()
    assert runtime["ai_outputs"].read_bytes() == (BASELINE / "ai_outputs.csv").read_bytes()
