"""ALERT004 = Hero Moment 3, the fully-ready positive control.

After the approved readiness correction (its five previously-unavailable evidence
items are now on file), ALERT004 reads as 100% ready with no missing evidence,
while still showing the same verified structuring claim and completed human review.
ALERT006 remains the sole readiness-blocked (50%) demo case.

These tests exercise the REAL runtime functions — app.case_readiness_pct,
app.get_case_detail, the Case File render (Streamlit AppTest), and the generator's
build_dataset — not hard-coded constants.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT))

import app  # noqa: E402

APP = str(ROOT / "app.py")
TABLES = ["alerts", "customers", "prior_cases", "kyc_profile_status",
          "transactions", "evidence_items", "ai_outputs", "human_reviews"]


def _source() -> dict:
    return {t: pd.read_csv(DATA / f"{t}.csv", dtype=str, keep_default_na=False) for t in TABLES}


def _evidence(alert_id: str) -> pd.DataFrame:
    ev = pd.read_csv(DATA / "evidence_items.csv", dtype=str, keep_default_na=False)
    return ev[ev["alert_id"] == alert_id]


def _available(df: pd.DataFrame) -> int:
    return int((df["available"].str.lower() == "true").sum())


def _open_md(alert_id: str) -> str:
    at = AppTest.from_file(APP, default_timeout=120)
    at.session_state["open_case"] = alert_id
    at.run()
    assert not at.exception, f"{alert_id} Case File raised: {[str(e.value) for e in at.exception]}"
    return " ".join(m.value for m in at.markdown)


def _pair(label: str, value: str) -> str:
    return f'case-summary-label">{label}</div><div class="case-summary-value">{value}</div>'


# 1
def test_alert004_has_exactly_ten_evidence_rows():
    assert len(_evidence("ALERT004")) == 10


# 2
def test_alert004_all_ten_evidence_rows_available():
    assert _available(_evidence("ALERT004")) == 10


# 3
def test_alert004_readiness_is_100_percent():
    assert app.case_readiness_pct("ALERT004", _source()) == 100


# 4
def test_alert004_has_no_missing_evidence():
    assert app.get_case_detail("ALERT004", _source())["missing"] == []


# 5
def test_alert004_summary_stays_pass_complete_edited():
    md = _open_md("ALERT004")
    assert _pair("AI VERIFICATION", "PASS") in md
    assert _pair("REVIEW REQUIREMENTS", "COMPLETE") in md
    assert _pair("RECORDED DISPOSITION", "EDITED") in md


# 6
def test_alert004_case_file_renders_no_missing_evidence_warning():
    md = _open_md("ALERT004")
    assert "Missing evidence:" not in md


# 7
def test_alert006_remains_five_of_ten_and_50_percent():
    ev = _evidence("ALERT006")
    assert len(ev) == 10
    assert _available(ev) == 5
    assert app.case_readiness_pct("ALERT006", _source()) == 50


# 8
def test_alert006_is_the_sole_readiness_blocked_case():
    src = _source()
    blocked = [a for a in src["alerts"]["alert_id"] if app.case_readiness_pct(a, src) == 50]
    assert blocked == ["ALERT006"], f"expected only ALERT006 blocked at 50%, got {blocked}"


# 9
def test_structuring_claim_and_four_transactions_unchanged():
    src = _source()
    claims = app.get_case_detail("ALERT004", src)["ai_claims"]
    # the real verifier still returns the single supported structuring PASS
    assert [(c["type"], c["result"]) for c in claims] == [("structuring", "PASS")]
    t = src["transactions"]
    sub = t[t["customer_id"] == "CUST0004"].copy()
    sub["amt"] = sub["amount"].astype(float)
    deposits = sub[(sub["amt"] >= 9000) & (sub["amt"] < 10000)]
    assert len(deposits) == 4
    assert sorted(deposits["amt"].tolist()) == [9200.0, 9400.0, 9500.0, 9700.0]


# 10
def test_generator_reproduces_alert004_and_alert006_readiness():
    # build_dataset does no disk I/O (only main() writes) — safe to run in-process.
    sys.path.insert(0, str(ROOT / "scripts"))
    import generate_data  # noqa: E402
    gen = generate_data.build_dataset()["evidence_items"].copy()
    gen["available"] = gen["available"].astype(str)

    def counts(aid):
        r = gen[gen["alert_id"] == aid]
        return len(r), _available(r)

    assert counts("ALERT004") == (10, 10)
    assert counts("ALERT006") == (10, 5)
