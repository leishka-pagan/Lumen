"""Pure projector: actual verifier results + routing decision + HumanReview + override
state -> one canonical CaseLifecycle.

PURE and self-contained at the behavior level: no CSV, filesystem, network, environment,
Streamlit, SDK, clock, or audit access. It reads the real contracts of the deterministic
subsystems (src.verifier.VerificationResult, src.review_gate.evaluate_review,
src.review_routing.ReviewRoutingDecision, src.case_lifecycle) and normalizes legitimate
scalar CSV values into domain values. It never reruns verification, never invents review
decisions / final actions / provenance / timestamps / model IDs / routing, and fails
closed on anything malformed or contradictory.
"""

from __future__ import annotations

from src.case_lifecycle import (
    AIDraftSource, AIVerificationStatus, CaseLifecycle, DispositionSource,
    HumanReviewDecision, OverrideStatus, ProcessingStatus, ReviewGateStatus,
    ReviewRoutingStatus,
)
from src.review_gate import evaluate_review
from src.review_routing import ReviewRoutingDecision
from src.verifier import VerificationResult

__all__ = [
    "LifecycleProjectionError",
    "summarize_ai_verification",
    "project_processed_lifecycle",
]

# draft_disposition (the review's decision about the AI draft) -> HumanReviewDecision.
# final_action is a separate CASE action and is never sourced from this map.
_DECISION_MAP = {
    "accepted": HumanReviewDecision.ACCEPTED,
    "edited": HumanReviewDecision.EDITED,
    "rejected": HumanReviewDecision.REJECTED,
}


class LifecycleProjectionError(Exception):
    """Raised when projection inputs are malformed, unsupported, or contradictory.
    Fail-closed: no lifecycle is produced when this is raised. (CaseLifecycle's own
    ``LifecycleInvariantError`` may also surface for a record-level invariant.)"""


def _is_nan(value: object) -> bool:
    return isinstance(value, float) and value != value  # NaN is the only value != itself


def _norm_optional_str(value: object, field: str) -> str | None:
    """Normalize a scalar CSV value to a clean string or None. None / float NaN / empty /
    whitespace-only / a stringified ``"nan"`` -> None (the last matches review_gate's own
    missing-value rule). A clean string is stripped. bool/int/float/list -> reject."""
    if value is None or _is_nan(value):
        return None
    if type(value) is str:
        stripped = value.strip()
        if stripped == "" or stripped.lower() == "nan":
            return None
        return stripped
    raise LifecycleProjectionError(f"{field} has unsupported type {type(value).__name__}")


def _norm_required_str(value: object, field: str) -> str:
    normalized = _norm_optional_str(value, field)
    if normalized is None:
        raise LifecycleProjectionError(f"{field} is required but missing or blank")
    return normalized


def _norm_enum(value: object, enum_cls, field: str):
    """Accept an enum member or its (whitespace-tolerant, case-insensitive) string value;
    reject unknown values and unsupported types."""
    if isinstance(value, enum_cls):
        return value
    if type(value) is str:
        try:
            return enum_cls(value.strip().lower())
        except ValueError as exc:
            raise LifecycleProjectionError(
                f"{field}: unknown {enum_cls.__name__} value {value!r}"
            ) from exc
    raise LifecycleProjectionError(
        f"{field} must be a {enum_cls.__name__} member or its string value, "
        f"got {type(value).__name__}"
    )


def summarize_ai_verification(results) -> AIVerificationStatus:
    """Summarize real VerificationResults into an AIVerificationStatus.

    Requires >= 1 result; each must be a VerificationResult with status PASS or FAIL.
    All PASS -> PASS; all FAIL -> FAIL; a mix -> MIXED. Malformed/unsupported result
    objects (incl. an indeterminate NEEDS_REVIEW status the enum cannot represent) are
    rejected. Verification is NOT rerun here — only the reported statuses are read.
    """
    materialized = list(results)
    if not materialized:
        raise LifecycleProjectionError("at least one VerificationResult is required")
    has_pass = has_fail = False
    for result in materialized:
        if not isinstance(result, VerificationResult):
            raise LifecycleProjectionError(f"unsupported result object: {type(result).__name__}")
        status = result.status
        if status == "PASS":
            has_pass = True
        elif status == "FAIL":
            has_fail = True
        else:
            raise LifecycleProjectionError(f"unsupported verification status: {status!r}")
    if has_pass and has_fail:
        return AIVerificationStatus.MIXED
    if has_fail:
        return AIVerificationStatus.FAIL
    return AIVerificationStatus.PASS


def _map_decision(raw_draft_disposition: object) -> HumanReviewDecision:
    """Map the review's draft_disposition to a HumanReviewDecision. Missing/blank -> NONE
    (a review with no decision on file); a recognized value -> its decision; any other
    nonempty value -> reject (unknown draft_disposition)."""
    value = _norm_optional_str(raw_draft_disposition, "draft_disposition")
    if value is None:
        return HumanReviewDecision.NONE
    decision = _DECISION_MAP.get(value.lower())
    if decision is None:
        raise LifecycleProjectionError(f"unknown draft_disposition {value!r}")
    return decision


def project_processed_lifecycle(
    *,
    alert_id,
    processing_run_id,
    processed_at,
    ai_draft_source,
    ai_draft_reference,
    model_id,
    verification_results,
    routing_decision,
    human_review,
    override_status,
    override_request_id,
) -> CaseLifecycle:
    """Project one PROCESSED CaseLifecycle from real subsystem outputs.

    ai_verification is computed only from ``verification_results``. The supplied
    ``routing_decision`` must be compatible (FAIL/MIXED => REQUIRED). Overrides are a
    separate axis and never change routing. See the module docstring for the full
    fail-closed contract.
    """
    if not isinstance(routing_decision, ReviewRoutingDecision):
        raise LifecycleProjectionError("routing_decision must be a ReviewRoutingDecision")
    if human_review is not None and not hasattr(human_review, "get"):
        raise LifecycleProjectionError("human_review must be a mapping-like row or None")

    ai_verification = summarize_ai_verification(verification_results)

    # Routing compatibility (routing POLICY lives elsewhere; here we only validate it).
    routing = routing_decision.routing
    if ai_verification in (AIVerificationStatus.FAIL, AIVerificationStatus.MIXED):
        if routing is not ReviewRoutingStatus.REQUIRED:
            raise LifecycleProjectionError("FAIL/MIXED verification requires REQUIRED routing")
    elif ai_verification is AIVerificationStatus.PASS:
        if routing not in (ReviewRoutingStatus.REQUIRED, ReviewRoutingStatus.NOT_REQUIRED):
            raise LifecycleProjectionError("PASS routing must be REQUIRED or NOT_REQUIRED")

    routing_policy_id = _norm_required_str(routing_decision.policy_id, "routing_policy_id")

    # Normalize the processing / draft inputs (CaseLifecycle enforces draft-source coherence).
    norm_alert_id = _norm_required_str(alert_id, "alert_id")
    norm_run_id = _norm_required_str(processing_run_id, "processing_run_id")
    norm_processed_at = _norm_required_str(processed_at, "processed_at")
    norm_source = _norm_enum(ai_draft_source, AIDraftSource, "ai_draft_source")
    norm_draft_ref = _norm_optional_str(ai_draft_reference, "ai_draft_reference")
    norm_model_id = _norm_optional_str(model_id, "model_id")
    norm_override_status = _norm_enum(override_status, OverrideStatus, "override_status")
    norm_override_request_id = _norm_optional_str(override_request_id, "override_request_id")

    # Review-dependent fields.
    review_gate = ReviewGateStatus.NOT_EVALUATED
    human_review_decision = HumanReviewDecision.NONE
    human_review_id = None
    final_action = None
    disposition_source = DispositionSource.NONE
    disposition_reference = None

    if routing is ReviewRoutingStatus.NOT_REQUIRED:
        if human_review is not None:
            raise LifecycleProjectionError("NOT_REQUIRED routing cannot carry a HumanReview")
        review_gate = ReviewGateStatus.NOT_APPLICABLE
        final_action = _norm_required_str(routing_decision.system_action, "system_action")
        disposition_source = DispositionSource.SYSTEM_POLICY
        disposition_reference = routing_policy_id

    else:  # REQUIRED
        if human_review is None:
            review_gate = ReviewGateStatus.PENDING
        else:
            human_review_id = _norm_required_str(human_review.get("review_id"), "review_id")
            human_review_decision = _map_decision(human_review.get("draft_disposition"))
            gate = evaluate_review(human_review, enforce=True)  # real enforcement behavior
            if gate.blocked:
                review_gate = ReviewGateStatus.BLOCKED
                # preserve review_id + any valid decision; NO final action / provenance.
            else:
                review_gate = ReviewGateStatus.COMPLETE
                final_action = _norm_optional_str(human_review.get("final_action"), "final_action")
                if final_action is None:
                    raise LifecycleProjectionError("COMPLETE review is missing final_action")
                disposition_source = DispositionSource.HUMAN_REVIEW
                disposition_reference = human_review_id  # never draft_disposition

    # Construct the canonical record; CaseLifecycle enforces every remaining invariant.
    return CaseLifecycle(
        alert_id=norm_alert_id,
        processing_status=ProcessingStatus.PROCESSED,
        processing_run_id=norm_run_id,
        processed_at=norm_processed_at,
        ai_draft_source=norm_source,
        ai_draft_reference=norm_draft_ref,
        model_id=norm_model_id,
        ai_verification=ai_verification,
        review_routing=routing,
        review_gate=review_gate,
        human_review_decision=human_review_decision,
        human_review_id=human_review_id,
        override_status=norm_override_status,
        override_request_id=norm_override_request_id,
        final_action=final_action,
        disposition_source=disposition_source,
        disposition_reference=disposition_reference,
        routing_policy_id=routing_policy_id,
        error_code=None,
    )
