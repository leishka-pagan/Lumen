"""End-to-end orchestration for one alert.

The flow embodies the build principle:
  Structured claims first. Deterministic verification second. Human approval last.

Step 1 (structured claims) supports two modes:
  Default: load the deterministic seeded claims from data/ai_outputs.csv. This
  is the default because the demo must not depend on network, API latency, or
  nondeterministic model output.
  Live: call llm_drafter.draft_claims, but only when explicitly enabled via the
  use_live_llm argument or the LUMEN_USE_LIVE_LLM=1 environment variable. Live
  mode never crashes the pipeline: on empty output or any failure it falls back
  to the seeded claims so the alert still gets processed.

Step 2 (verification) is deterministic (src/verifier.py). Step 3 (human approval)
runs the anti-rubber-stamp gate: the alert's human review is evaluated through the
shared, pure rule src/review_gate.evaluate_review. A review missing any required
field is blocked and fails closed (no final disposition), and exactly one structured
audit event is written for an actual pipeline evaluation (review_blocked, or
review_bypassed when enforcement is explicitly disabled).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pandas as pd

from . import audit, llm_drafter, verifier
from .review_gate import evaluate_review

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA = PROJECT_ROOT / "data"

# Tables the verifier reads (passed in full; the verifier filters internally).
SOURCE_TABLE_NAMES = [
    "customers", "transactions", "prior_cases",
    "kyc_profile_status", "evidence_items", "alerts",
]


def _read_table(name: str) -> pd.DataFrame:
    """Load one data table as strings, matching the verifier/drafter contract."""
    return pd.read_csv(DATA / f"{name}.csv", keep_default_na=False, dtype=str)


def _load_source_tables() -> dict[str, pd.DataFrame]:
    """Full source tables for the verifier loop."""
    return {name: _read_table(name) for name in SOURCE_TABLE_NAMES}


def _load_review(alert_id: str) -> dict[str, Any] | None:
    """Load the human review for an alert via the pipeline's table abstraction.

    Kept out of the gate itself: ``evaluate_review`` stays a pure function over a
    review mapping, while data loading uses the same ``_read_table`` abstraction as
    the rest of the pipeline. Returns None when no review is on file for the alert.
    """
    try:
        hr = _read_table("human_reviews")
    except FileNotFoundError:
        return None
    rows = hr[hr["alert_id"] == alert_id]
    return rows.iloc[0].to_dict() if len(rows) else None


def _load_seeded_claims(alert_id: str) -> list[dict[str, Any]]:
    """Deterministic claims for an alert, parsed from data/ai_outputs.csv.

    Returns the same list[dict] shape the verifier loop expects, with
    evidence_refs parsed from its JSON string back into a Python list.
    """
    try:
        ai = _read_table("ai_outputs")
    except FileNotFoundError:
        return []
    rows = ai[ai["alert_id"] == alert_id]
    claims: list[dict[str, Any]] = []
    for _, r in rows.iterrows():
        try:
            refs = json.loads(r["evidence_refs"]) if r["evidence_refs"] else []
        except (ValueError, TypeError):
            refs = []
        claims.append(
            {
                "output_id": r["output_id"],
                "alert_id": r["alert_id"],
                "claim_id": r["claim_id"],
                "claim_type": r["claim_type"],
                "asserted_value": r["asserted_value"],
                "evidence_refs": refs,
                "generated_at": r["generated_at"],
            }
        )
    return claims


def _build_live_inputs(alert_id: str):
    """Build (alert dict, filtered source_tables) for llm_drafter.draft_claims.

    Returns (None, None) if the alert_id is not found. source_tables is filtered
    to the alert's customer (evidence_items is filtered by alert_id), per the
    drafter contract.
    """
    alerts_df = _read_table("alerts")
    arow = alerts_df[alerts_df["alert_id"] == alert_id]
    if len(arow) == 0:
        return None, None
    arow = arow.iloc[0]
    alert = {
        "alert_id": arow["alert_id"],
        "customer_id": arow["customer_id"],
        "rule_triggered": arow["rule_triggered"],
        "severity": arow["severity"],
        "triggered_at": arow["triggered_at"],
        "status": arow["status"],
    }
    cid = arow["customer_id"]
    customers = _read_table("customers")
    transactions = _read_table("transactions")
    prior_cases = _read_table("prior_cases")
    kyc = _read_table("kyc_profile_status")
    evidence = _read_table("evidence_items")
    source_tables = {
        "customers": customers[customers["customer_id"] == cid],
        "transactions": transactions[transactions["customer_id"] == cid],
        "prior_cases": prior_cases[prior_cases["customer_id"] == cid],
        "kyc_profile_status": kyc[kyc["customer_id"] == cid],
        "evidence_items": evidence[evidence["alert_id"] == alert_id],
    }
    return alert, source_tables


def process_alert(
    alert_id: str,
    source: Any = None,
    use_live_llm: bool = False,
    human_review: Any = None,
    enforce_gate: bool = True,
    log_path: Path | None = None,
) -> dict[str, Any]:
    """Run one alert through the pipeline.

    alert_id: the alert to process.
    source: full source tables for verification. Loaded from data/ if not given.
    use_live_llm: when True (or env LUMEN_USE_LIVE_LLM=1), draft claims with the
        live LLM instead of the seeded data. Defaults to False (demo-safe).
    human_review: the reviewer's disposition for this alert. If None it is loaded
        via the pipeline's own table abstraction (never read inside the gate).
    enforce_gate: whether the anti-rubber-stamp gate is enforced (HERO CASE B).
        Defaults to True. When False an incomplete review is allowed through as an
        explicit, audited governance bypass.
    log_path: audit destination override, forwarded to every audit event so tests
        redirect the trail away from data/audit_log.csv.
    """
    live = use_live_llm or os.getenv("LUMEN_USE_LIVE_LLM") == "1"

    audit.log_event(actor="pipeline", action="alert_processing_started",
                    alert_id=alert_id, log_path=log_path)

    # The verifier needs the full source tables. Load them if the caller did not
    # supply them, so verification is real rather than defaulting to NEEDS_REVIEW.
    if source is None:
        source = _load_source_tables()

    # Step 1: STRUCTURED CLAIMS FIRST.
    if live:
        try:
            alert_row, drafter_source = _build_live_inputs(alert_id)
            drafted = (
                llm_drafter.draft_claims(alert_row, drafter_source)
                if alert_row is not None
                else []
            )
            if drafted:
                claims = drafted
            else:
                # Live mode returned nothing usable. Fall back to seeded claims.
                audit.log_event(actor="pipeline", action="draft_empty",
                                alert_id=alert_id, log_path=log_path)
                claims = _load_seeded_claims(alert_id)
        except Exception as exc:  # never crash the pipeline on a drafter or data failure
            audit.log_event(
                actor="pipeline",
                action="draft_failed",
                alert_id=alert_id,
                details={"reason": f"{type(exc).__name__}: {exc}"},
                log_path=log_path,
            )
            claims = _load_seeded_claims(alert_id)
    else:
        # Default deterministic path: seeded claims, no network, no LLM.
        claims = _load_seeded_claims(alert_id)

    # Step 2: DETERMINISTIC VERIFICATION SECOND.
    # Each claim is checked against the source data by the deterministic verifier.
    results = []
    for claim in claims:
        result = verifier.verify_claim(claim, source)
        results.append(result)
        audit.log_event(
            actor="verifier",
            action="claim_verified",
            alert_id=alert_id,
            details={
                "claim_id": claim.get("claim_id"),
                "claim_type": claim.get("claim_type"),
                "status": result.status,
                "reason": result.reason,
            },
            log_path=log_path,
        )

    # Step 3: HUMAN APPROVAL LAST — anti-rubber-stamp gate (HERO CASE B).
    # Evaluate the human review (explicit input, else loaded via the pipeline's own
    # table abstraction) through the shared deterministic gate. Fail closed: a
    # blocked review yields NO final disposition and is never treated as finalized.
    review = human_review if human_review is not None else _load_review(alert_id)
    gate = evaluate_review(review, enforce=enforce_gate)
    disposition = gate.disposition  # None unless the gate allowed a finalized disposition

    if review is not None:
        if gate.blocked:
            # Exactly one structured block event per actual pipeline evaluation.
            audit.log_event(
                actor="review_gate",
                action="review_blocked",
                alert_id=alert_id,
                details={
                    "review_id": gate.review_id,
                    "missing": list(gate.missing),
                    "enforcement_enabled": gate.enforcement_enabled,
                },
                log_path=log_path,
            )
        elif not gate.enforcement_enabled and gate.missing:
            # Enforcement intentionally disabled AND the review would otherwise have
            # failed: record one explicit governance-bypass event.
            audit.log_event(
                actor="review_gate",
                action="review_bypassed",
                alert_id=alert_id,
                details={
                    "review_id": gate.review_id,
                    "missing_bypassed": list(gate.missing),
                    "enforcement_enabled": False,
                },
                log_path=log_path,
            )

    audit.log_event(
        actor="pipeline",
        action="alert_processing_finished",
        alert_id=alert_id,
        details={"claim_count": len(claims), "disposition": disposition,
                 "review_blocked": gate.blocked},
        log_path=log_path,
    )

    return {
        "alert_id": alert_id,
        "results": results,
        "disposition": disposition,
        "review_gate": gate,
    }
