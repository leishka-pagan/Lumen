"""Regression tests for the Manager Review → Case File routing (corrective pass).

Covers the new pure helper ``app.case_target_for_override`` (which decides which
alert's Case File the per-card "Open Case File" control opens) and the end-to-end
linkage that a pending override routes to its OWN alert, which resolves to the
expected verification (Hero A → FAIL).

``import app`` runs the Streamlit script once in bare mode (harmless warnings, no
writes); the default view is Analyst so the Manager Review card code is not
executed at import time.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
from src.verifier import verify_prior_sar_history  # noqa: E402


def _load(name: str) -> pd.DataFrame:
    return pd.read_csv(DATA / f"{name}.csv", dtype=str, keep_default_na=False)


# ── the pure routing helper ────────────────────────────────────────────────

def test_target_is_the_overrides_own_alert():
    valid = ["ALERT001", "ALERT002", "ALERT007"]
    assert app.case_target_for_override({"alert_id": "ALERT002"}, valid) == "ALERT002"


def test_target_none_for_dangling_alert():
    assert app.case_target_for_override({"alert_id": "ALERT999"}, ["ALERT001"]) is None


def test_target_none_for_blank_alert_id():
    assert app.case_target_for_override({"alert_id": ""}, ["ALERT001"]) is None


def test_target_accepts_a_pandas_series_row():
    row = pd.Series({"alert_id": "ALERT007", "change_id": "CHG-SEED-003"})
    assert app.case_target_for_override(row, ["ALERT001", "ALERT007"]) == "ALERT007"


# ── end-to-end linkage against the seeded records ──────────────────────────

def test_every_pending_override_routes_to_a_real_alert():
    overrides = _load("pending_overrides")
    valid = _load("alerts")["alert_id"].tolist()
    pending = overrides[overrides["status"] == "pending"]
    assert len(pending) >= 1, "expected at least one pending override to route"
    for _, r in pending.iterrows():
        assert app.case_target_for_override(r.to_dict(), valid) == r["alert_id"]


def test_alert001_override_opens_a_case_that_fails_verification():
    # Manager Review routing to Hero A: the ALERT001 override opens ALERT001's
    # Case File, where prior_sar_history verifies FAIL (prior_sar_count=0).
    src = {n: _load(n) for n in ("prior_cases", "alerts")}
    claim = {"claim_type": "prior_sar_history", "alert_id": "ALERT001", "evidence_refs": ["x"]}
    assert verify_prior_sar_history(claim, src).status == "FAIL"
