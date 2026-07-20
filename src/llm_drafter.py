"""LLM integration layer: draft structured AML claims via Anthropic tool use.

This module is the "structured claims first" step of the build principle. It
takes one alert plus the source rows for that alert's customer, calls the
Anthropic API forcing a single tool call, and turns the model's tool input into
claim dicts shaped exactly like an AiOutput row (see src/schema.py).

Strict separation of duties: this module DRAFTS claims. It never judges whether
a claim is true. That is verifier.py's job. No LLM judges another LLM here.

The model is constrained three ways:
1. tool_choice is forced, so the model must answer with a tool call, not prose.
2. The tool input schema restricts claim_type to the closed vocabulary.
3. After the response, every claim is re-validated in Python (claim_type known,
   asserted_value non-empty, evidence_refs present where required). Anything
   that fails is dropped and logged, never returned.

This module never raises to its caller. On any failure (missing API key,
network or API error, no tool calls, or all claims dropped) it returns an empty
list and logs the failure. pipeline.py must handle an empty list gracefully.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

import anthropic

from . import audit
from .capture_guardrails import CAPTURE_MODEL, MAX_OUTPUT_TOKENS_PER_REQUEST
from .verifier import KNOWN_CLAIM_TYPES, REQUIRES_EVIDENCE_REFS

# API configuration. The SDK reads ANTHROPIC_API_KEY from the environment.
MODEL = "claude-sonnet-4-20250514"
MAX_TOKENS = 1024
TOOL_NAME = "submit_aml_claims"

# One-line descriptions for each claim type, drawn from docs/claim_types.md.
# The set of legal types comes from KNOWN_CLAIM_TYPES (verifier.py) so the
# vocabulary stays defined in exactly one place. Any type listed here that is
# not in KNOWN_CLAIM_TYPES is ignored when the prompt is built.
CLAIM_TYPE_DESCRIPTIONS: dict[str, str] = {
    "prior_sar_history": "Customer has a prior suspicious activity report on record (prior_cases.prior_sar_count greater than zero).",
    "rapid_movement": "Funds moved in and back out within a short window (transactions in then out, similar amounts, same day).",
    "structuring": "Multiple deposits each just under the reporting threshold, aggregating over a short span of days.",
    "expected_activity_mismatch": "Observed activity is far above the customer's declared expected_monthly_volume.",
    "missing_kyc_data": "A required KYC evidence item for this alert is not available.",
    "stale_kyc_profile": "The KYC profile was not refreshed within the last 12 months.",
    "high_risk_country": "A transaction involves a counterparty country on the high-risk list.",
    "unusual_transaction_volume": "Aggregate transaction volume is well outside the customer's normal baseline.",
    "prior_alert_history": "The customer has triggered alerts before this one.",
}


def _build_tool() -> dict[str, Any]:
    """The single tool the model is allowed to call."""
    return {
        "name": TOOL_NAME,
        "description": "Submit every structured AML claim you want to make for this alert.",
        "input_schema": {
            "type": "object",
            "properties": {
                "claims": {
                    "type": "array",
                    "description": "Between 1 and 3 claims, each backed by evidence_refs.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "claim_type": {
                                "type": "string",
                                "enum": sorted(KNOWN_CLAIM_TYPES),
                            },
                            "asserted_value": {
                                "type": "string",
                                "description": "What the claim asserts, for example 'true' or a short numeric summary.",
                            },
                            "evidence_refs": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Source row references formatted as table_name.column=value.",
                            },
                        },
                        "required": ["claim_type", "asserted_value", "evidence_refs"],
                    },
                }
            },
            "required": ["claims"],
        },
    }


def _build_system_prompt() -> str:
    """System prompt: role, legal claim types, and hard output rules."""
    lines = [
        "You are an AML (anti money laundering) claim drafter.",
        "",
        "You may only assert claims from this closed list of types:",
    ]
    for claim_type in sorted(KNOWN_CLAIM_TYPES):
        desc = CLAIM_TYPE_DESCRIPTIONS.get(claim_type, "")
        lines.append(f"- {claim_type}: {desc}")
    lines += [
        "",
        f"You MUST respond by calling the {TOOL_NAME} tool with every claim you",
        "want to make. Do not return any text. Only the tool call.",
        "",
        "Include only claims you can back with evidence_refs that point at actual",
        "rows in the source data provided in the user message.",
        "Format each reference as the exact CSV table name, a dot, the column",
        "name, an equals sign, and the value. For example:",
        "prior_cases.customer_id=CUST0001 or transactions.txn_id=TXN00001.",
        "Legal table names are: customers, transactions, alerts, evidence_items,",
        "prior_cases, kyc_profile_status, ai_outputs, human_reviews.",
        "Do not invent table names.",
        "",
        "Emit between 1 and 3 claims for this alert. Not more.",
    ]
    return "\n".join(lines)


def _df_rows(source_tables: dict[str, pd.DataFrame], name: str) -> list[dict]:
    """Return a table's rows as a list of dicts, or empty if absent or empty."""
    df = source_tables.get(name)
    if df is None or len(df) == 0:
        return []
    return df.to_dict(orient="records")


def _summarize_source(alert: dict, source_tables: dict[str, pd.DataFrame]) -> str:
    """Build a clearly labeled plain-text summary of the relevant source rows.

    The data is presented as labeled sections, not raw JSON, so the model reads
    it the way an analyst would.
    """
    sections: list[str] = []

    customers = _df_rows(source_tables, "customers")
    if customers:
        c = customers[0]
        sections.append(
            "CUSTOMER PROFILE\n"
            f"  customer_id: {c.get('customer_id', '')}\n"
            f"  name: {c.get('name', '')}\n"
            f"  occupation: {c.get('occupation', '')}\n"
            f"  country: {c.get('country', '')}\n"
            f"  kyc_status: {c.get('kyc_status', '')}\n"
            f"  expected_monthly_volume: {c.get('expected_monthly_volume', '')}\n"
            f"  kyc_last_updated: {c.get('kyc_last_updated', '')}"
        )

    prior = _df_rows(source_tables, "prior_cases")
    if prior:
        p = prior[0]
        sections.append(
            "PRIOR SAR HISTORY\n"
            f"  prior_sar_count: {p.get('prior_sar_count', '')}\n"
            f"  last_sar_date: {p.get('last_sar_date', '') or 'none'}"
        )

    kyc = _df_rows(source_tables, "kyc_profile_status")
    if kyc:
        k = kyc[0]
        sections.append(
            "KYC PROFILE STATUS\n"
            f"  current_within_12mo: {k.get('current_within_12mo', '')}\n"
            f"  last_updated: {k.get('last_updated', '')}"
        )

    txns = _df_rows(source_tables, "transactions")
    if txns:
        lines = ["RECENT TRANSACTIONS"]
        for t in txns:
            lines.append(
                f"  txn_id={t.get('txn_id', '')} amount={t.get('amount', '')} "
                f"direction={t.get('direction', '')} "
                f"counterparty_country={t.get('counterparty_country', '')} "
                f"timestamp={t.get('timestamp', '')}"
            )
        sections.append("\n".join(lines))

    evidence = _df_rows(source_tables, "evidence_items")
    if evidence:
        lines = ["EVIDENCE ITEMS"]
        for e in evidence:
            lines.append(
                f"  item_type={e.get('item_type', '')} available={e.get('available', '')}"
            )
        sections.append("\n".join(lines))

    return "\n\n".join(sections)


def _build_user_message(alert: dict, source_tables: dict[str, pd.DataFrame]) -> str:
    header = (
        "ALERT\n"
        f"  alert_id: {alert.get('alert_id', '')}\n"
        f"  customer_id: {alert.get('customer_id', '')}\n"
        f"  rule_triggered: {alert.get('rule_triggered', '')}\n"
        f"  severity: {alert.get('severity', '')}"
    )
    return header + "\n\n" + _summarize_source(alert, source_tables)


def _extract_claims(response: Any) -> list[dict]:
    """Pull claim objects out of the model's tool calls.

    Reads the content blocks of a Messages response, keeps only tool_use blocks
    named submit_aml_claims, and concatenates their claims arrays.
    """
    raw: list[dict] = []
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == TOOL_NAME:
            tool_input = getattr(block, "input", None) or {}
            claims = tool_input.get("claims", []) if isinstance(tool_input, dict) else []
            if isinstance(claims, list):
                raw.extend(c for c in claims if isinstance(c, dict))
    return raw


def _build_output(alert: dict, claim_type: str, asserted_value: str, evidence_refs: list[str]) -> dict:
    """Construct one AiOutput-shaped dict. evidence_refs stays a Python list."""
    return {
        "output_id": "OUT-" + uuid.uuid4().hex[:8],
        "alert_id": alert.get("alert_id"),
        "claim_id": "CLM-" + uuid.uuid4().hex[:8],
        "claim_type": claim_type,
        "asserted_value": asserted_value,
        "evidence_refs": evidence_refs,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


class CaptureResponseError(Exception):
    """Raised by the CAPTURE adapter when a provider response is not acceptable (zero
    valid claims, or more than three). Intentionally non-sensitive: its message carries
    only a count — never a prompt, a raw response, or customer data. Normal
    ``draft_claims`` never raises this (it swallows and returns an empty list)."""


def build_capture_request(alert: dict, source_tables: dict[str, pd.DataFrame]) -> dict[str, Any]:
    """The EXACT ``messages.create`` kwargs for a one-time CAPTURE request.

    Reuses the existing single forced tool, the existing prompts, and the same
    source-data path as ``draft_claims`` — but pins the fixed capture model and the
    capture output-token cap (both imported from ``capture_guardrails``; never
    redefined). Extended thinking and any model fallback are simply ABSENT here, which
    is how they stay disabled. Pure: builds a dict, opens no client and no network.
    """
    return {
        "model": CAPTURE_MODEL,
        "max_tokens": MAX_OUTPUT_TOKENS_PER_REQUEST,
        "system": _build_system_prompt(),
        "tools": [_build_tool()],
        "tool_choice": {"type": "any"},   # forced single structured tool; no web/tool use
        "messages": [{"role": "user", "content": _build_user_message(alert, source_tables)}],
    }


def draft_claims_for_capture(alert: dict, source_tables: dict[str, pd.DataFrame], client: Any):
    """Capture-specific single-request adapter for the guarded capture runner.

    Issues EXACTLY ONE forced-tool request with the INJECTED ``client`` (the runner
    constructs it with timeout/retries per the guardrails; tests inject a fake), then
    returns ``(normalized_claim_rows, input_tokens, output_tokens)``. It reuses the
    same tool schema, prompts, extraction, and per-claim validation as ``draft_claims``,
    but returns the provider ``usage`` and — unlike ``draft_claims`` — neither swallows
    errors nor writes an audit event: the capture runner must see a failure to halt, and
    it owns its own durable ledger. ``draft_claims`` and every normal caller are
    unchanged.
    """
    response = client.messages.create(**build_capture_request(alert, source_tables))
    valid: list[dict] = []
    for claim in _extract_claims(response):
        claim_type = claim.get("claim_type")
        asserted_value = claim.get("asserted_value")
        evidence_refs = claim.get("evidence_refs", [])
        if claim_type not in KNOWN_CLAIM_TYPES:
            continue
        if not isinstance(asserted_value, str) or not asserted_value.strip():
            continue
        # Capture is stricter than normal drafting: evidence_refs must be a list and
        # EVERY supplied item must be a nonempty string (a malformed element rejects the
        # whole claim, rather than being silently coerced away).
        if not isinstance(evidence_refs, list):
            continue
        if any(not isinstance(ref, str) or not ref.strip() for ref in evidence_refs):
            continue
        if claim_type in REQUIRES_EVIDENCE_REFS and len(evidence_refs) == 0:
            continue
        valid.append(_build_output(alert, claim_type, asserted_value, list(evidence_refs)))
    # A capture response is successful ONLY with 1..3 valid normalized claims. Zero
    # (nothing usable) or more than three (fail closed) is a rejected response; the
    # message carries only a count, never response content or customer data.
    if not (1 <= len(valid) <= 3):
        raise CaptureResponseError(
            f"capture response yielded {len(valid)} valid claims; expected 1..3"
        )
    usage = getattr(response, "usage", None)
    return valid, getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None)


def draft_claims(
    alert: dict,
    source_tables: dict[str, pd.DataFrame],
    log_path: Path | None = None,
) -> list[dict]:
    """Draft structured AML claims for one alert using the Anthropic API.

    alert: one alerts.csv row as a dict.
    source_tables: table name to DataFrame, already filtered to the alert's
        customer. Expected keys: customers, transactions, prior_cases,
        kyc_profile_status, evidence_items.
    log_path: forwarded to audit.log_event so tests can redirect the log.

    Returns a list of AiOutput-shaped dicts. evidence_refs is a Python list of
    strings, not a JSON string; the caller serializes it. Returns an empty list
    on any failure and never raises, so pipeline.py can treat an empty result as
    "no claims drafted" without special error handling.
    """
    alert_id = alert.get("alert_id")
    audit.log_event(actor="llm_drafter", action="draft_started", alert_id=alert_id, log_path=log_path)

    # Call the API. Any failure (missing key, network, API error) is caught and
    # turned into an empty result.
    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=_build_system_prompt(),
            tools=[_build_tool()],
            tool_choice={"type": "any"},
            messages=[{"role": "user", "content": _build_user_message(alert, source_tables)}],
        )
    except Exception as exc:  # noqa: BLE001  intentional: never raise to caller
        audit.log_event(
            actor="llm_drafter",
            action="draft_failed",
            alert_id=alert_id,
            details={"reason": "api_call_failed", "error": f"{type(exc).__name__}: {exc}"},
            log_path=log_path,
        )
        return []

    raw_claims = _extract_claims(response)
    if not raw_claims:
        audit.log_event(
            actor="llm_drafter",
            action="draft_failed",
            alert_id=alert_id,
            details={"reason": "no_tool_calls_returned"},
            log_path=log_path,
        )
        return []

    def _drop(claim_type: Any, reason: str) -> None:
        audit.log_event(
            actor="llm_drafter",
            action="claim_dropped",
            alert_id=alert_id,
            details={"claim_type": claim_type, "reason": reason},
            log_path=log_path,
        )

    valid: list[dict] = []
    for claim in raw_claims:
        claim_type = claim.get("claim_type")
        asserted_value = claim.get("asserted_value")
        evidence_refs = claim.get("evidence_refs", [])

        # Gate 1: claim_type must be in the closed vocabulary.
        if claim_type not in KNOWN_CLAIM_TYPES:
            _drop(claim_type, "unknown_claim_type")
            continue

        # Gate 2: asserted_value must be a non-empty string.
        if not isinstance(asserted_value, str) or not asserted_value.strip():
            _drop(claim_type, "empty_asserted_value")
            continue

        # Gate 3: evidence_refs must be a non-empty list for types that need it.
        if not isinstance(evidence_refs, list):
            evidence_refs = []
        if claim_type in REQUIRES_EVIDENCE_REFS and len(evidence_refs) == 0:
            _drop(claim_type, "missing_evidence_refs")
            continue

        valid.append(_build_output(alert, claim_type, asserted_value, list(evidence_refs)))

    if not valid:
        audit.log_event(
            actor="llm_drafter",
            action="draft_failed",
            alert_id=alert_id,
            details={"reason": "all_claims_dropped"},
            log_path=log_path,
        )
        return []

    audit.log_event(
        actor="llm_drafter",
        action="draft_completed",
        alert_id=alert_id,
        details={"claim_count": len(valid)},
        log_path=log_path,
    )
    return valid
