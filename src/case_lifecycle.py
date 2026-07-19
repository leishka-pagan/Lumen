"""Canonical Case Lifecycle domain model + invariant enforcement for LUMEN.

PURE and self-contained: no application/Streamlit import, no CSV, no filesystem, no
network, no SDK, no environment, and no clock access. It imports only ``dataclasses``
and ``enum``. It models one alert's governance lifecycle as a frozen, fully-validated
record, and derives the queue status from that record. It creates no lifecycle records
for real alerts, performs no persistence, and implements no review-routing *policy*
(that decision — REQUIRED vs NOT_REQUIRED — is a separate module).

Central domain distinction (confirmed against data/human_reviews.csv):
  * ``human_review_decision`` (ACCEPTED / EDITED / REJECTED) is the reviewer's decision
    ABOUT THE AI DRAFT. It is NEVER the recorded disposition.
  * ``final_action`` (e.g. "monitor", "escalate") is the actual case disposition.
  The recorded disposition is therefore ``final_action`` with explicit provenance
  (``disposition_source`` + ``disposition_reference``), never the review decision.

A ``CaseLifecycle`` cannot be constructed in an invalid state: every invariant is
checked in ``__post_init__`` and a violation raises ``LifecycleInvariantError``.
``derive_queue_status`` is a pure, deterministic function of a (valid) record.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class LifecycleInvariantError(Exception):
    """Raised when a CaseLifecycle record violates a domain invariant. Fail-closed:
    an invalid record can never be constructed."""


# ── Enums ────────────────────────────────────────────────────────────────────
class ProcessingStatus(Enum):
    NOT_PROCESSED = auto()
    PROCESSED = auto()
    ERROR = auto()


class AIDraftSource(Enum):
    NONE = auto()
    CAPTURED_LIVE = auto()
    SYNTHETIC_FIXTURE = auto()


class AIVerificationStatus(Enum):
    NOT_EVALUATED = auto()
    PASS = auto()
    MIXED = auto()
    FAIL = auto()


class ReviewRoutingStatus(Enum):
    UNDETERMINED = auto()
    REQUIRED = auto()
    NOT_REQUIRED = auto()


class ReviewGateStatus(Enum):
    NOT_EVALUATED = auto()
    NOT_APPLICABLE = auto()
    PENDING = auto()
    COMPLETE = auto()
    BLOCKED = auto()


class HumanReviewDecision(Enum):
    NONE = auto()
    ACCEPTED = auto()
    EDITED = auto()
    REJECTED = auto()


class OverrideStatus(Enum):
    NONE = auto()
    PENDING = auto()
    APPROVED = auto()
    REJECTED = auto()


class DispositionSource(Enum):
    NONE = auto()
    SYSTEM_POLICY = auto()
    HUMAN_REVIEW = auto()


class QueueStatus(Enum):
    NOT_PROCESSED = auto()
    PROCESSING_ERROR = auto()
    AWAITING_MANAGER = auto()
    AWAITING_REVIEW = auto()
    BLOCKED = auto()
    CLOSED = auto()


__all__ = [
    "LifecycleInvariantError",
    "ProcessingStatus",
    "AIDraftSource",
    "AIVerificationStatus",
    "ReviewRoutingStatus",
    "ReviewGateStatus",
    "HumanReviewDecision",
    "OverrideStatus",
    "DispositionSource",
    "QueueStatus",
    "CaseLifecycle",
    "derive_queue_status",
]

# Words reserved for the review DECISION vocabulary; ``final_action`` (a case action)
# may never be one of them, so a review-decision placeholder cannot masquerade as the
# recorded disposition.
_REVIEW_DECISION_WORDS = frozenset(m.name.lower() for m in HumanReviewDecision)

# Optional string fields: each must be either None or a nonempty string (never "").
_OPTIONAL_STRING_FIELDS = (
    "processing_run_id", "processed_at", "ai_draft_reference", "model_id",
    "human_review_id", "override_request_id", "final_action", "disposition_reference",
    "routing_policy_id", "error_code",
)


def _blank(value: object) -> bool:
    """True for an empty/whitespace-only string (an invalid optional value)."""
    return isinstance(value, str) and value.strip() == ""


@dataclass(frozen=True)
class CaseLifecycle:
    """One alert's canonical governance lifecycle. Frozen and fully validated.

    Nullable values use ``None`` (never ""). Queue status is NOT stored here — it is
    derived by ``derive_queue_status``.
    """

    alert_id: str
    processing_status: ProcessingStatus
    processing_run_id: str | None = None
    processed_at: str | None = None
    ai_draft_source: AIDraftSource = AIDraftSource.NONE
    ai_draft_reference: str | None = None
    model_id: str | None = None
    ai_verification: AIVerificationStatus = AIVerificationStatus.NOT_EVALUATED
    review_routing: ReviewRoutingStatus = ReviewRoutingStatus.UNDETERMINED
    review_gate: ReviewGateStatus = ReviewGateStatus.NOT_EVALUATED
    human_review_decision: HumanReviewDecision = HumanReviewDecision.NONE
    human_review_id: str | None = None
    override_status: OverrideStatus = OverrideStatus.NONE
    override_request_id: str | None = None
    final_action: str | None = None
    disposition_source: DispositionSource = DispositionSource.NONE
    disposition_reference: str | None = None
    routing_policy_id: str | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        _validate(self)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise LifecycleInvariantError(message)


def _validate(r: CaseLifecycle) -> None:
    # ── General ──────────────────────────────────────────────────────────────
    _require(isinstance(r.alert_id, str) and r.alert_id.strip() != "",
             "alert_id must be a nonempty string")
    for name in _OPTIONAL_STRING_FIELDS:
        _require(not _blank(getattr(r, name)),
                 f"{name} must be None or a nonempty string; empty string is invalid")
    # final_action is a case action, never a review-decision word.
    if r.final_action is not None:
        _require(r.final_action.strip().lower() not in _REVIEW_DECISION_WORDS,
                 f"final_action {r.final_action!r} is a review decision, not a case action")

    # ── Override (global; kept separate from review routing) ─────────────────
    if r.override_status is OverrideStatus.NONE:
        _require(r.override_request_id is None,
                 "override_request_id must be None when override_status is NONE")
    else:
        _require(r.override_request_id is not None,
                 f"override_request_id is required when override_status is {r.override_status.name}")

    # ── Global disposition-provenance coherence ──────────────────────────────
    # A final action requires provenance; provenance requires a final action; a
    # reference requires both. (The routing rules below are stricter still.)
    if r.final_action is not None:
        _require(r.disposition_source is not DispositionSource.NONE,
                 "final_action requires a disposition_source (never NONE)")
        _require(r.disposition_reference is not None,
                 "final_action requires a disposition_reference")
    if r.disposition_source is not DispositionSource.NONE:
        _require(r.final_action is not None, "disposition_source requires a final_action")
        _require(r.disposition_reference is not None, "disposition_source requires a disposition_reference")
    if r.disposition_reference is not None:
        _require(r.disposition_source is not DispositionSource.NONE,
                 "disposition_reference requires a disposition_source")

    # ── Processing-status branches ───────────────────────────────────────────
    ps = r.processing_status
    if ps is ProcessingStatus.NOT_PROCESSED:
        _require(r.processing_run_id is None, "NOT_PROCESSED: processing_run_id must be None")
        _require(r.processed_at is None, "NOT_PROCESSED: processed_at must be None")
        _require(r.ai_draft_source is AIDraftSource.NONE, "NOT_PROCESSED: ai_draft_source must be NONE")
        _require(r.ai_draft_reference is None, "NOT_PROCESSED: ai_draft_reference must be None")
        _require(r.model_id is None, "NOT_PROCESSED: model_id must be None")
        _require(r.ai_verification is AIVerificationStatus.NOT_EVALUATED,
                 "NOT_PROCESSED: ai_verification must be NOT_EVALUATED")
        _require(r.review_routing is ReviewRoutingStatus.UNDETERMINED,
                 "NOT_PROCESSED: review_routing must be UNDETERMINED")
        _require(r.review_gate is ReviewGateStatus.NOT_EVALUATED,
                 "NOT_PROCESSED: review_gate must be NOT_EVALUATED")
        _require(r.human_review_decision is HumanReviewDecision.NONE,
                 "NOT_PROCESSED: human_review_decision must be NONE")
        _require(r.human_review_id is None, "NOT_PROCESSED: human_review_id must be None")
        _require(r.final_action is None, "NOT_PROCESSED: final_action must be None")
        _require(r.disposition_source is DispositionSource.NONE,
                 "NOT_PROCESSED: disposition_source must be NONE")
        _require(r.disposition_reference is None, "NOT_PROCESSED: disposition_reference must be None")
        _require(r.error_code is None, "NOT_PROCESSED: error_code must be None")

    elif ps is ProcessingStatus.ERROR:
        _require(r.error_code is not None, "ERROR: error_code is required")
        _require(r.final_action is None, "ERROR: no final_action permitted")
        _require(r.disposition_source is DispositionSource.NONE, "ERROR: disposition_source must be NONE")
        _require(r.disposition_reference is None, "ERROR: disposition_reference must be None")

    elif ps is ProcessingStatus.PROCESSED:
        _require(r.processing_run_id is not None, "PROCESSED: processing_run_id is required")
        _require(r.processed_at is not None, "PROCESSED: processed_at is required")
        _require(r.ai_draft_source is not AIDraftSource.NONE, "PROCESSED: ai_draft_source cannot be NONE")
        _require(r.ai_draft_reference is not None, "PROCESSED: ai_draft_reference is required")
        _require(r.model_id is not None, "PROCESSED: model_id is required")
        _require(r.ai_verification is not AIVerificationStatus.NOT_EVALUATED,
                 "PROCESSED: ai_verification cannot be NOT_EVALUATED")
        _require(r.review_routing is not ReviewRoutingStatus.UNDETERMINED,
                 "PROCESSED: review_routing cannot be UNDETERMINED")
        _require(r.error_code is None, "PROCESSED: error_code must be None")

        # AI verification → routing constraints (routing POLICY is a separate module;
        # here we only enforce that FAIL/MIXED can never be NOT_REQUIRED).
        if r.ai_verification in (AIVerificationStatus.FAIL, AIVerificationStatus.MIXED):
            _require(r.review_routing is ReviewRoutingStatus.REQUIRED,
                     "AI verification FAIL/MIXED must route REQUIRED")

        if r.review_routing is ReviewRoutingStatus.REQUIRED:
            _require(r.review_gate not in (ReviewGateStatus.NOT_APPLICABLE, ReviewGateStatus.NOT_EVALUATED),
                     "REQUIRED routing: review_gate cannot be NOT_APPLICABLE or NOT_EVALUATED")
            if r.review_gate in (ReviewGateStatus.PENDING, ReviewGateStatus.BLOCKED):
                _require(r.final_action is None, "REQUIRED PENDING/BLOCKED: no final_action")
                _require(r.disposition_source is DispositionSource.NONE,
                         "REQUIRED PENDING/BLOCKED: disposition_source must be NONE")
                _require(r.disposition_reference is None,
                         "REQUIRED PENDING/BLOCKED: no disposition_reference")
            elif r.review_gate is ReviewGateStatus.COMPLETE:
                _require(r.human_review_decision is not HumanReviewDecision.NONE,
                         "REQUIRED COMPLETE: human_review_decision cannot be NONE")
                _require(r.human_review_id is not None, "REQUIRED COMPLETE: human_review_id is required")
                _require(r.final_action is not None, "REQUIRED COMPLETE: final_action is required")
                _require(r.disposition_source is DispositionSource.HUMAN_REVIEW,
                         "REQUIRED COMPLETE: disposition_source must be HUMAN_REVIEW")
                _require(r.disposition_reference == r.human_review_id,
                         "REQUIRED COMPLETE: disposition_reference must equal human_review_id")

        elif r.review_routing is ReviewRoutingStatus.NOT_REQUIRED:
            _require(r.ai_verification is AIVerificationStatus.PASS,
                     "NOT_REQUIRED routing: ai_verification must be PASS")
            _require(r.review_gate is ReviewGateStatus.NOT_APPLICABLE,
                     "NOT_REQUIRED routing: review_gate must be NOT_APPLICABLE")
            _require(r.human_review_decision is HumanReviewDecision.NONE,
                     "NOT_REQUIRED routing: human_review_decision must be NONE")
            _require(r.human_review_id is None, "NOT_REQUIRED routing: human_review_id must be None")
            _require(r.final_action is not None, "NOT_REQUIRED routing: final_action is required")
            _require(r.disposition_source is DispositionSource.SYSTEM_POLICY,
                     "NOT_REQUIRED routing: disposition_source must be SYSTEM_POLICY")
            _require(r.routing_policy_id is not None, "NOT_REQUIRED routing: routing_policy_id is required")
            _require(r.disposition_reference == r.routing_policy_id,
                     "NOT_REQUIRED routing: disposition_reference must equal routing_policy_id")

    # ── BLOCKED (wherever it appears): no final action or provenance ─────────
    if r.review_gate is ReviewGateStatus.BLOCKED:
        _require(r.final_action is None, "BLOCKED review_gate: no final_action")
        _require(r.disposition_source is DispositionSource.NONE,
                 "BLOCKED review_gate: disposition_source must be NONE")
        _require(r.disposition_reference is None, "BLOCKED review_gate: no disposition_reference")


def derive_queue_status(record: CaseLifecycle) -> QueueStatus:
    """Pure, deterministic queue status for a valid lifecycle record.

    Priority (first match wins), per the domain model:
      1. NOT_PROCESSED           -> NOT_PROCESSED
      2. ERROR                   -> PROCESSING_ERROR
      3. PENDING override        -> AWAITING_MANAGER
      4. BLOCKED review gate     -> BLOCKED
      5. REQUIRED + PENDING gate -> AWAITING_REVIEW
      6. final_action w/ SYSTEM_POLICY or HUMAN_REVIEW provenance -> CLOSED
    No other combination derives CLOSED.
    """
    if record.processing_status is ProcessingStatus.NOT_PROCESSED:
        return QueueStatus.NOT_PROCESSED
    if record.processing_status is ProcessingStatus.ERROR:
        return QueueStatus.PROCESSING_ERROR
    if record.override_status is OverrideStatus.PENDING:
        return QueueStatus.AWAITING_MANAGER
    if record.review_gate is ReviewGateStatus.BLOCKED:
        return QueueStatus.BLOCKED
    if (record.review_routing is ReviewRoutingStatus.REQUIRED
            and record.review_gate is ReviewGateStatus.PENDING):
        return QueueStatus.AWAITING_REVIEW
    if (record.final_action is not None
            and record.disposition_source in (DispositionSource.SYSTEM_POLICY, DispositionSource.HUMAN_REVIEW)):
        return QueueStatus.CLOSED
    # Unreachable for a valid record; defensive so the function is total.
    raise LifecycleInvariantError("no queue status is derivable for this lifecycle combination")
