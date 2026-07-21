"""AI-draft accuracy is a DISPLAY relabelling, never a semantic change.

The visible vocabulary was corrected so an evidence-match result cannot be read as an
AML clearance decision:

    alert level   pass -> VERIFIED    mixed -> MIXED    fail -> UNSUPPORTED
                  not_evaluated -> NOT EVALUATED
    claim level   PASS -> SUPPORTED   FAIL  -> CONTRADICTED   NEEDS_REVIEW -> NEEDS REVIEW

Everything underneath is untouched: the AIVerificationStatus enum values, the verifier's
PASS/FAIL results, every comparison against them, queue derivation, review routing, the
review gate and the recorded disposition. These tests pin BOTH halves — the internal
values that must not move and the visible words that must.

Render assertions drive the real app through AppTest; internal assertions read the
committed CSVs and call the real derivation functions.
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
from src.case_lifecycle import AIVerificationStatus, derive_queue_status  # noqa: E402
from src.lifecycle_store import load_lifecycle  # noqa: E402

APP = str(ROOT / "app.py")
_SRC = (ROOT / "app.py").read_text(encoding="utf-8")

TABLES = ["alerts", "customers", "prior_cases", "kyc_profile_status",
          "transactions", "evidence_items", "ai_outputs", "human_reviews"]

# Canonical demo anchors, asserted against the committed lifecycle below so a data
# change breaks these tests loudly instead of silently weakening them.
PASS_ALERT = "ALERT007"    # internal pass  -> VERIFIED, all three claims PASS
MIXED_ALERT = "ALERT002"   # internal mixed -> MIXED, PASS + FAIL claims together
FAIL_ALERT = "ALERT033"    # internal fail  -> UNSUPPORTED, both claims FAIL

RETIRED_HEADINGS = ["AI VERIFICATION", "AI Verification",
                    "AI Draft Verification", "AI Claim Verification"]
RETIRED_COPY = ["failed verification", "passed verification"]


# ── helpers ──────────────────────────────────────────────────────────────────
def _source() -> dict:
    return {t: pd.read_csv(DATA / f"{t}.csv", dtype=str, keep_default_na=False) for t in TABLES}


def _lifecycle() -> dict:
    return {r.alert_id: r for r in load_lifecycle(DATA / "case_lifecycle.csv")}


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


def _pair(label: str, value: str) -> str:
    return f'case-summary-label">{label}</div><div class="case-summary-value">{value}</div>'


def _verdicts(md: str) -> list[str]:
    return re.findall(r'hero-verdict">([A-Z ]+)</span>', md)


def _committed_hashes() -> dict:
    return {f.name: hashlib.sha256(f.read_bytes()).hexdigest()
            for f in sorted(DATA.rglob("*")) if f.is_file()}


# ── 1-4 — the internal enum values did not move ──────────────────────────────
def test_internal_ai_verification_enum_values_unchanged():
    """1,2,3,4 — PASS / MIXED / FAIL / NOT_EVALUATED still exist under their own names."""
    assert AIVerificationStatus.PASS.value == "pass"
    assert AIVerificationStatus.MIXED.value == "mixed"
    assert AIVerificationStatus.FAIL.value == "fail"
    assert AIVerificationStatus.NOT_EVALUATED.value == "not_evaluated"
    # and no display word leaked into the enum
    values = {s.value for s in AIVerificationStatus}
    assert values == {"pass", "mixed", "fail", "not_evaluated"}
    for word in ("verified", "unsupported", "supported", "contradicted"):
        assert word not in values


def test_derive_ai_verification_still_returns_internal_tokens():
    """The derivation function is untouched: it yields PASS/MIXED/FAIL, not display words."""
    src = _source()
    derived = {a: app.derive_ai_verification(app.get_case_detail(a, src)["ai_claims"])
               for a in src["alerts"]["alert_id"]}
    assert set(derived.values()) <= {"PASS", "MIXED", "FAIL", "NOT EVALUATED"}
    assert derived[PASS_ALERT] == "PASS"
    assert derived[MIXED_ALERT] == "MIXED"
    assert derived[FAIL_ALERT] == "FAIL"


def test_internal_claim_results_are_still_pass_and_fail():
    """The verifier's per-claim result stays PASS/FAIL — only the rendered word changed."""
    src = _source()
    seen = {c["result"] for a in src["alerts"]["alert_id"]
            for c in app.get_case_detail(a, src)["ai_claims"]}
    assert seen <= {"PASS", "FAIL", "NEEDS_REVIEW"}
    assert {"PASS", "FAIL"} <= seen
    assert [c["result"] for c in app.get_case_detail(MIXED_ALERT, src)["ai_claims"]] \
        == ["PASS", "FAIL", "PASS"]
    assert [c["result"] for c in app.get_case_detail(FAIL_ALERT, src)["ai_claims"]] \
        == ["FAIL", "FAIL"]


# ── 5 — stored values are unchanged ──────────────────────────────────────────
def test_stored_lifecycle_values_are_still_internal_tokens():
    lc = pd.read_csv(DATA / "case_lifecycle.csv", dtype=str).fillna("")
    assert set(lc["ai_verification"]) <= {"pass", "mixed", "fail", "not_evaluated"}
    for word in ("VERIFIED", "UNSUPPORTED", "SUPPORTED", "CONTRADICTED",
                 "verified", "unsupported"):
        assert word not in set(lc["ai_verification"]), f"display word {word} stored in CSV"
    # the three anchors are the states this task relabels
    idx = _lifecycle()
    assert idx[PASS_ALERT].ai_verification is AIVerificationStatus.PASS
    assert idx[MIXED_ALERT].ai_verification is AIVerificationStatus.MIXED
    assert idx[FAIL_ALERT].ai_verification is AIVerificationStatus.FAIL


def test_no_display_vocabulary_written_into_any_committed_csv():
    for f in sorted(DATA.rglob("*.csv")):
        text = f.read_text(encoding="utf-8", errors="replace")
        for word in ("UNSUPPORTED", "CONTRADICTED"):
            assert word not in text, f"{f.name} contains display word {word}"


# ── 6,7,8 — alert-level rail renders the new vocabulary ──────────────────────
@pytest.mark.parametrize("alert_id, internal, visible", [
    (PASS_ALERT, "pass", "VERIFIED"),
    (MIXED_ALERT, "mixed", "MIXED"),
    (FAIL_ALERT, "fail", "UNSUPPORTED"),
])
def test_alert_level_rail_maps_internal_to_visible(alert_id, internal, visible):
    idx = _lifecycle()
    assert idx[alert_id].ai_verification.value == internal      # internal, unchanged
    md = _case(alert_id)
    assert _pair("AI DRAFT ACCURACY", visible) in md            # visible, relabelled
    # the retired alert-level word never appears in the rail
    retired = {"pass": "PASS", "fail": "FAIL"}.get(internal)
    if retired:
        assert _pair("AI DRAFT ACCURACY", retired) not in md


def test_ai_verif_label_map_is_exactly_the_authorized_mapping():
    assert app.AI_VERIF_LABELS == {"pass": "VERIFIED", "mixed": "MIXED",
                                   "fail": "UNSUPPORTED", "not_evaluated": "NOT EVALUATED"}
    for status in AIVerificationStatus:
        assert status.value in app.AI_VERIF_LABELS, f"{status} has no display label"


def test_not_evaluated_still_renders_not_evaluated():
    """4 — the neutral state keeps its wording and its neutral style."""
    assert app.AI_VERIF_LABELS["not_evaluated"] == "NOT EVALUATED"
    assert "background:#F4F6F8;border:1px solid #D0D5DD;color:#667085;" in _SRC


# ── 9,10 — claim-level verdicts render the new vocabulary ────────────────────
def test_claim_verdict_label_maps_internal_to_visible():
    assert app.claim_verdict_label("PASS") == "SUPPORTED"
    assert app.claim_verdict_label("FAIL") == "CONTRADICTED"
    assert app.claim_verdict_label("NEEDS_REVIEW") == "NEEDS REVIEW"
    assert app.claim_verdict_label("NEEDS REVIEW") == "NEEDS REVIEW"
    # unknown values pass through rather than being silently mislabelled
    assert app.claim_verdict_label("SOMETHING_ELSE") == "SOMETHING_ELSE"


def test_all_supported_case_renders_only_supported_verdicts():
    """9 — every internal PASS claim renders SUPPORTED."""
    src = _source()
    results = [c["result"] for c in app.get_case_detail(PASS_ALERT, src)["ai_claims"]]
    assert results == ["PASS", "PASS", "PASS"]
    assert _verdicts(_case(PASS_ALERT)) == ["SUPPORTED", "SUPPORTED", "SUPPORTED"]


def test_all_contradicted_case_renders_only_contradicted_verdicts():
    """10 — every internal FAIL claim renders CONTRADICTED."""
    assert _verdicts(_case(FAIL_ALERT)) == ["CONTRADICTED", "CONTRADICTED"]


def test_mixed_case_renders_supported_and_contradicted_side_by_side():
    """9+10 in one Case File, in claim order, with no retired word between them."""
    md = _case(MIXED_ALERT)
    assert _verdicts(md) == ["SUPPORTED", "CONTRADICTED", "SUPPORTED"]
    assert 'hero-verdict">PASS<' not in md
    assert 'hero-verdict">FAIL<' not in md


def test_rendered_verdicts_match_the_verifier_for_every_alert():
    """The display map is applied uniformly — no alert renders a stale verdict."""
    src = _source()
    for alert_id in (PASS_ALERT, MIXED_ALERT, FAIL_ALERT, "ALERT001", "ALERT039"):
        expected = [app.claim_verdict_label(c["result"])
                    for c in app.get_case_detail(alert_id, src)["ai_claims"]]
        assert _verdicts(_case(alert_id)) == expected, alert_id


# ── 11,12 — the retired vocabulary is gone ───────────────────────────────────
def test_no_retired_heading_anywhere_in_app_source():
    """11 — proven at the source, so no unvisited branch can still render it."""
    for heading in RETIRED_HEADINGS:
        assert heading not in _SRC, f"retired heading still in app.py: {heading}"


def test_no_retired_verification_copy_anywhere_in_app_source():
    """12 — 'failed verification' / 'passed verification' are gone."""
    for phrase in RETIRED_COPY:
        assert phrase not in _SRC, f"retired copy still in app.py: {phrase}"


@pytest.mark.parametrize("alert_id", [PASS_ALERT, MIXED_ALERT, FAIL_ALERT])
def test_no_retired_vocabulary_renders_in_a_case_file(alert_id):
    md = _case(alert_id)
    for token in RETIRED_HEADINGS + RETIRED_COPY:
        assert token not in md, f"{alert_id}: retired wording rendered: {token}"


def test_no_retired_vocabulary_renders_on_the_manager_review_tab():
    md = _md(_run(view_as="Manager"))
    for token in RETIRED_HEADINGS + RETIRED_COPY:
        assert token not in md, f"Manager Review: retired wording rendered: {token}"


def test_contradicted_warning_copy_is_grammatical():
    """12 — the replacement copy, singular and plural, with the trigger untouched."""
    assert "AI-drafted claim is contradicted by the source evidence." in _SRC
    assert "AI-drafted claims are contradicted by the source evidence." in _SRC
    # the warning still keys off the INTERNAL verifier result, not the visible word
    assert '_fails = [cl for cl in case["ai_claims"] if cl["result"] == "FAIL"]' in _SRC
    md = _case(FAIL_ALERT)
    assert "2 AI-drafted claims are contradicted by the source evidence." in md
    md1 = _case(MIXED_ALERT)
    assert "1 AI-drafted claim is contradicted by the source evidence." in md1


def test_no_contradiction_warning_when_no_claim_fails():
    md = _case(PASS_ALERT)
    assert "contradicted by the source evidence" not in md


# ── 13 — AI DRAFT ACCURACY reaches every summary surface ─────────────────────
@pytest.mark.parametrize("alert_id", [PASS_ALERT, MIXED_ALERT, FAIL_ALERT, "ALERT001"])
def test_case_file_summary_rail_uses_ai_draft_accuracy(alert_id):
    assert 'case-summary-label">AI DRAFT ACCURACY</div>' in _case(alert_id)


def test_manager_review_oversight_cards_use_ai_draft_accuracy():
    """13 — the requires-attention and completed-review cards on the Manager tab."""
    md = _md(_run(view_as="Manager"))
    assert "AI DRAFT ACCURACY" in md
    labels = {app.lifecycle_ai_verification_label(r) for r in _lifecycle().values()}
    assert labels <= {"VERIFIED", "MIXED", "UNSUPPORTED", "NOT EVALUATED"}
    # ALERT007 requires attention; ALERT001 + ALERT004 are the completed reviews
    assert md.count("AI DRAFT ACCURACY") >= 3, "expected the dimension on every card"
    assert "AI DRAFT ACCURACY: VERIFIED" in md


def test_accuracy_badges_are_scoped_to_the_accuracy_dimension_only():
    """The new badge palette applies to AI accuracy alone — other dimensions keep theirs."""
    assert 'if dim == "AI DRAFT ACCURACY":' in _SRC
    for cls in ("badge-acc-verified", "badge-acc-mixed",
                "badge-acc-unsupported", "badge-acc-none"):
        assert f".{cls}{{" in _SRC


# ── 14,15 — the evidence-check heading and its one-time explanation ──────────
@pytest.mark.parametrize("alert_id", [PASS_ALERT, MIXED_ALERT, FAIL_ALERT, "ALERT001"])
def test_case_file_shows_the_evidence_check_heading(alert_id):
    assert "AI Draft Evidence Check" in _case(alert_id)


@pytest.mark.parametrize("alert_id", [PASS_ALERT, MIXED_ALERT, FAIL_ALERT, "ALERT001"])
def test_explanatory_sentence_appears_exactly_once_per_case_file(alert_id):
    """15 — exactly once, regardless of how many claims the case carries."""
    md = _case(alert_id)
    assert md.count(app.EVIDENCE_CHECK_NOTE) == 1, "explanation must appear exactly once"


def test_explanatory_sentence_is_verbatim_and_sits_under_the_heading():
    assert app.EVIDENCE_CHECK_NOTE == (
        "This checks whether the AI-generated draft matches the source evidence. "
        "It is not an AML clearance decision."
    )
    md = _case(MIXED_ALERT)
    i_head = md.index("AI Draft Evidence Check")
    i_note = md.index(app.EVIDENCE_CHECK_NOTE)
    i_claim = md.index("Draft Claim")
    assert i_head < i_note < i_claim, "note must sit between the heading and the first claim"


def test_explanatory_sentence_style_is_the_authorized_style():
    block = re.search(r"\.evidence-check-note\{([^}]*)\}", _SRC, re.S)
    assert block, ".evidence-check-note rule missing"
    css = block.group(1).replace("\n", "").replace(" ", "")
    for decl in ("background:transparent", "border:none", "color:#667085",
                 "padding:10px14px0", "margin:0", "font-size:12px",
                 "font-weight:500", "line-height:1.4", "border-radius:0",
                 "box-shadow:none"):
        assert decl in css, f"missing declaration: {decl}"


def test_not_processed_branch_shows_no_evidence_check_note():
    """The note belongs to the evidence-check panel, so it cannot leak elsewhere."""
    assert _SRC.count("EVIDENCE_CHECK_NOTE") == 2      # the definition + one render site


# ── 16 — queue, routing, gate and disposition are unaffected ─────────────────
def test_queue_status_derivation_is_unchanged_for_all_39():
    src = app.load_source_tables(app.mtimes_key())
    idx = _lifecycle()
    for _, arow in src["alerts"].iterrows():
        row = app.build_queue_row(arow, src, idx)
        assert row["status"] == app.QUEUE_STATUS_LABELS[derive_queue_status(idx[arow["alert_id"]])]


def test_queue_labels_carry_no_accuracy_vocabulary():
    labels = set(app.QUEUE_STATUS_LABELS.values())
    assert labels & {"VERIFIED", "UNSUPPORTED", "SUPPORTED", "CONTRADICTED"} == set()


@pytest.mark.parametrize("alert_id, req, disp", [
    ("ALERT001", "COMPLETE", "MONITOR"),
    ("ALERT004", "COMPLETE", "ESCALATE"),
    (PASS_ALERT, "BLOCKED", "NONE"),
    (MIXED_ALERT, "PENDING", "NONE"),
])
def test_review_requirements_and_disposition_rails_are_untouched(alert_id, req, disp):
    md = _case(alert_id)
    assert _pair("REVIEW REQUIREMENTS", req) in md
    assert _pair("RECORDED DISPOSITION", disp) in md


def test_review_gate_wording_is_untouched():
    assert "HUMAN-REVIEW GATE: BLOCKED" in _case(PASS_ALERT)
    assert "REVIEW REQUIREMENTS: COMPLETE" in _case("ALERT001")


def test_consistency_guard_compares_internal_values_not_display_labels():
    """The recorded-vs-derived guard must not fire just because the label changed."""
    assert '_recorded_internal = lc.ai_verification.value.upper().replace("_", " ")' in _SRC
    assert 'derive_ai_verification(case["ai_claims"]) != _recorded_internal' in _SRC
    for alert_id in (PASS_ALERT, MIXED_ALERT, FAIL_ALERT):
        assert "CURRENT VERIFICATION DIFFERS" not in _case(alert_id), alert_id


# ── 17 — rendering mutates nothing ───────────────────────────────────────────
@pytest.mark.parametrize("alert_id", [PASS_ALERT, MIXED_ALERT, FAIL_ALERT])
def test_rendering_a_case_file_writes_no_audit_and_mutates_no_csv(alert_id, monkeypatch):
    import src.audit as audit_mod
    calls: list = []
    monkeypatch.setattr(audit_mod, "log_event", lambda **kw: calls.append(kw) or {})
    before = _committed_hashes()
    md = _case(alert_id)
    assert "AI Draft Evidence Check" in md                 # it really rendered
    assert calls == [], f"opening {alert_id} wrote audit events: {calls}"
    assert _committed_hashes() == before, "a committed data file changed"
