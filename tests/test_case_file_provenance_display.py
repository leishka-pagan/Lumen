"""Case File must not expose internal AI-processing provenance to analysts.

The Case File's "AI Draft Verification" panel shows only the draft claim, its source
evidence, and the verdict. Model IDs, processing-run IDs, the captured_live enum,
raw output/claim IDs and the routing-policy ID stay INTERNAL: they remain on the
lifecycle record, in the CSVs, in the capture seed and in the audit log — they are
simply not rendered.

Render assertions drive the real app via AppTest; data assertions read the committed
files directly to prove nothing was deleted or rewritten.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT))

from src.case_lifecycle import AIDraftSource  # noqa: E402
from src.lifecycle_store import load_lifecycle  # noqa: E402

APP = str(ROOT / "app.py")
_SRC = (ROOT / "app.py").read_text(encoding="utf-8")

# Every alert that carries a real captured draft, across both capture runs.
CASE_ALERTS = ["ALERT001", "ALERT008", "ALERT022", "ALERT039"]

# Strings that must never reach an analyst-facing Case File.
FORBIDDEN = [
    "DRAFT PROVENANCE",
    "claude-haiku-4-5-20251001",
    "LIVE-CAPTURE-V1",
    "DEMO-CAPTURE-REMAINING-V1",
    "CAPTURED LIVE",
    "captured_live",
    "SYNTHETIC FIXTURE",
    "synthetic_fixture",
    "POL-REVIEW-ROUTING-V1",
]


def _run(**state):
    at = AppTest.from_file(APP, default_timeout=120)
    for k, v in state.items():
        at.session_state[k] = v
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def _md(at) -> str:
    return " ".join(m.value for m in at.markdown)


def _case(alert_id: str) -> str:
    return _md(_run(open_case=alert_id))


def _committed_hashes() -> dict:
    return {f.name: hashlib.sha256(f.read_bytes()).hexdigest()
            for f in sorted(DATA.rglob("*")) if f.is_file()}


# ── 1,2,3 — no model id, run id, or captured-live enum in a Case File ────────
@pytest.mark.parametrize("alert_id", CASE_ALERTS)
def test_case_file_hides_all_technical_provenance(alert_id):
    md = _case(alert_id)
    for token in FORBIDDEN:
        assert token not in md, f"{alert_id}: '{token}' leaked into the Case File"


@pytest.mark.parametrize("alert_id", CASE_ALERTS)
def test_case_file_hides_raw_output_and_claim_ids(alert_id):
    """Raw OUT-*/CLM-* identifiers are internal keys, not analyst content."""
    ao = pd.read_csv(DATA / "ai_outputs.csv", dtype=str).fillna("")
    rows = ao[ao["alert_id"] == alert_id]
    assert len(rows) >= 1
    md = _case(alert_id)
    for _, r in rows.iterrows():
        assert r["output_id"] not in md, f"{alert_id}: raw output_id {r['output_id']} rendered"
        assert r["claim_id"] not in md, f"{alert_id}: raw claim_id {r['claim_id']} rendered"


def test_provenance_line_removed_from_source():
    """The provenance string and its wrapper are gone from app.py entirely."""
    assert "DRAFT PROVENANCE" not in _SRC
    assert "_prov_html" not in _SRC
    # and no empty placeholder was left behind in the panel markup
    assert '{_prov_html}{_panel_body}' not in _SRC
    assert '<div style="padding:14px 16px;background:#f5f5f5;">{_panel_body}</div>' in _SRC


# ── 4,5,6 — claims, evidence and verdicts still render ───────────────────────
@pytest.mark.parametrize("alert_id", CASE_ALERTS)
def test_claims_evidence_and_verdicts_still_render(alert_id):
    md = _case(alert_id)
    assert "AI Draft Verification" in md          # section heading
    assert "Draft Claim" in md                    # 1. draft claim
    assert "Source Evidence" in md                # 2. source evidence
    assert "Verdict" in md                        # 3. verdict
    # every claim's verdict is a real verifier status
    assert ("PASS" in md) or ("FAIL" in md)


def test_every_committed_claim_is_still_displayed():
    """Removing the provenance line removed no claim rows."""
    ao = pd.read_csv(DATA / "ai_outputs.csv", dtype=str).fillna("")
    for alert_id in CASE_ALERTS:
        md = _case(alert_id)
        expected = len(ao[ao["alert_id"] == alert_id])
        assert md.count("① Draft Claim") == expected, f"{alert_id}: claim count changed"


def test_not_processed_explanation_path_unchanged():
    """The no-draft branch still explains itself (it never had a provenance line)."""
    lc = {r.alert_id: r for r in load_lifecycle(DATA / "case_lifecycle.csv")}
    assert all(r.ai_draft_source is AIDraftSource.CAPTURED_LIVE for r in lc.values())
    assert "been run through the LUMEN processing workflow" in _SRC


# ── 7,8,9 — internal data is untouched ───────────────────────────────────────
def test_lifecycle_records_still_carry_provenance():
    for r in load_lifecycle(DATA / "case_lifecycle.csv"):
        assert r.ai_draft_source is AIDraftSource.CAPTURED_LIVE, r.alert_id
        assert r.model_id == "claude-haiku-4-5-20251001", r.alert_id
        assert r.processing_run_id, r.alert_id
        assert r.ai_draft_reference, r.alert_id
        assert r.processed_at, r.alert_id
        assert r.routing_policy_id == "POL-REVIEW-ROUTING-V1", r.alert_id


def test_csv_columns_and_values_still_carry_provenance():
    ao = pd.read_csv(DATA / "ai_outputs.csv", dtype=str).fillna("")
    for col in ("output_id", "claim_id", "draft_source", "draft_reference",
                "model_id", "processing_run_id", "generated_at"):
        assert col in ao.columns, f"ai_outputs.csv lost column {col}"
    assert (ao["draft_source"] == "captured_live").all()
    assert (ao["model_id"] == "claude-haiku-4-5-20251001").all()
    assert ao["output_id"].is_unique and ao["claim_id"].is_unique
    lc = pd.read_csv(DATA / "case_lifecycle.csv", dtype=str).fillna("")
    assert (lc["ai_draft_source"] == "captured_live").all()
    assert (lc["model_id"] == "claude-haiku-4-5-20251001").all()


def test_capture_seed_and_audit_still_intact():
    seed = json.loads((DATA / "live_capture_seed.json").read_text(encoding="utf-8"))
    assert seed["model"] == "claude-haiku-4-5-20251001"
    assert len(seed["captures"]) == 39
    assert all(r["processing_run_id"] for r in seed["captures"])
    audit = pd.read_csv(DATA / "audit_log.csv", dtype=str).fillna("")
    assert len(audit) > 0 and "details_json" in audit.columns


# ── 10 — rendering a Case File is read-only ──────────────────────────────────
@pytest.mark.parametrize("alert_id", ["ALERT001", "ALERT039"])
def test_rendering_case_file_is_read_only(alert_id, monkeypatch):
    import src.audit as audit_mod
    calls: list = []
    monkeypatch.setattr(audit_mod, "log_event", lambda **kw: calls.append(kw) or {})
    before = _committed_hashes()
    md = _case(alert_id)
    assert "AI Draft Verification" in md
    assert calls == [], f"opening {alert_id} wrote audit events: {calls}"
    assert _committed_hashes() == before, "a committed data file changed"
