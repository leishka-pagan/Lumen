"""Deterministic review-routing and system-disposition policy for LUMEN.

PURE and self-contained: no network, filesystem, environment, CSV, Streamlit, SDK, or
clock access. It imports only ``dataclasses`` and the two enums it reasons over from
``src.case_lifecycle`` (it does NOT redefine them).

Given a completed AI-verification result plus explicit, deterministic policy inputs, it
decides whether a human review is REQUIRED or NOT_REQUIRED, and — only when NOT_REQUIRED
— which system action disposes the case. The decision is fail-closed:

  * FAIL or MIXED always route REQUIRED and can never carry an auto-disposition.
  * PASS with any mandatory review reason routes REQUIRED (reasons preserved).
  * PASS with no mandatory reasons routes NOT_REQUIRED only when an auto-disposition
    action is explicitly supplied; otherwise it fails closed to REQUIRED.
  * NOT_EVALUATED is rejected — routing may only run after verification completes.

AI PASS alone never silently becomes NOT_REQUIRED. Overrides are deliberately absent
from this API: override routing is a separate lifecycle axis.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.case_lifecycle import AIVerificationStatus, ReviewRoutingStatus

__all__ = [
    "ReviewRoutingError",
    "ReviewRoutingDecision",
    "route_review",
]

# Stable reason-code vocabulary emitted by this policy.
_REASON_FAIL = "AI_VERIFICATION_FAIL"
_REASON_MIXED = "AI_VERIFICATION_MIXED"
_REASON_AUTO_AUTHORIZED = "AUTO_DISPOSITION_AUTHORIZED"
_REASON_NO_AUTO_POLICY = "NO_AUTO_DISPOSITION_POLICY"


class ReviewRoutingError(Exception):
    """Raised when a routing input is invalid or a policy invariant is violated.
    Fail-closed: no decision is produced when this is raised."""


def _clean_str(value: object) -> bool:
    """True iff value is an EXACT ``str`` (not bool/enum/subclass), nonempty, with no
    surrounding whitespace. NaN/float/int/bool/list all fail."""
    return type(value) is str and value != "" and value == value.strip()


@dataclass(frozen=True)
class ReviewRoutingDecision:
    """Immutable routing decision. REQUIRED decisions never carry a system action;
    NOT_REQUIRED decisions always carry a nonempty one."""

    routing: ReviewRoutingStatus
    policy_id: str
    reason_codes: tuple[str, ...]
    system_action: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.routing, ReviewRoutingStatus):
            raise ReviewRoutingError("routing must be a ReviewRoutingStatus member")
        if not _clean_str(self.policy_id):
            raise ReviewRoutingError("policy_id must be a nonempty string with no surrounding whitespace")
        if type(self.reason_codes) is not tuple or not all(_clean_str(rc) for rc in self.reason_codes):
            raise ReviewRoutingError("reason_codes must be a tuple of nonempty strings")
        if self.routing is ReviewRoutingStatus.REQUIRED:
            if self.system_action is not None:
                raise ReviewRoutingError("REQUIRED routing must have system_action=None")
        elif self.routing is ReviewRoutingStatus.NOT_REQUIRED:
            if not _clean_str(self.system_action):
                raise ReviewRoutingError("NOT_REQUIRED routing must have a nonempty system_action")
        else:  # UNDETERMINED (or anything else) is not a routing OUTCOME
            raise ReviewRoutingError("routing outcome must be REQUIRED or NOT_REQUIRED")


def route_review(
    *,
    ai_verification: AIVerificationStatus,
    policy_id: str,
    mandatory_review_reasons: tuple[str, ...] = (),
    auto_disposition_action: str | None = None,
) -> ReviewRoutingDecision:
    """Decide review routing deterministically from a completed verification result.

    See the module docstring for the fail-closed policy. Raises ReviewRoutingError on
    any invalid input, on NOT_EVALUATED, or when FAIL/MIXED/PASS-with-mandatory-reasons
    is paired with an auto-disposition action.
    """
    # ── Input validation ────────────────────────────────────────────────────
    if not isinstance(ai_verification, AIVerificationStatus):
        raise ReviewRoutingError(
            f"ai_verification must be an AIVerificationStatus member, got {type(ai_verification).__name__}"
        )
    if not _clean_str(policy_id):
        raise ReviewRoutingError("policy_id must be a nonempty string with no surrounding whitespace")
    if type(mandatory_review_reasons) is not tuple:
        raise ReviewRoutingError("mandatory_review_reasons must be a tuple")
    for reason in mandatory_review_reasons:
        if not _clean_str(reason):
            raise ReviewRoutingError(
                "each mandatory_review_reason must be a nonempty string with no surrounding whitespace"
            )
    if len(set(mandatory_review_reasons)) != len(mandatory_review_reasons):
        raise ReviewRoutingError("mandatory_review_reasons must be unique")
    if auto_disposition_action is not None and not _clean_str(auto_disposition_action):
        raise ReviewRoutingError(
            "auto_disposition_action must be None or a nonempty string with no surrounding whitespace"
        )

    # ── NOT_EVALUATED: routing may not run before verification completes ─────
    if ai_verification is AIVerificationStatus.NOT_EVALUATED:
        raise ReviewRoutingError("routing may not run before verification completes (NOT_EVALUATED)")

    # ── FAIL / MIXED: always REQUIRED, never an auto-disposition ─────────────
    if ai_verification is AIVerificationStatus.FAIL:
        if auto_disposition_action is not None:
            raise ReviewRoutingError("FAIL verification cannot carry an auto_disposition_action")
        return ReviewRoutingDecision(
            routing=ReviewRoutingStatus.REQUIRED, policy_id=policy_id,
            reason_codes=(_REASON_FAIL, *mandatory_review_reasons), system_action=None,
        )
    if ai_verification is AIVerificationStatus.MIXED:
        if auto_disposition_action is not None:
            raise ReviewRoutingError("MIXED verification cannot carry an auto_disposition_action")
        return ReviewRoutingDecision(
            routing=ReviewRoutingStatus.REQUIRED, policy_id=policy_id,
            reason_codes=(_REASON_MIXED, *mandatory_review_reasons), system_action=None,
        )

    # ── PASS ─────────────────────────────────────────────────────────────────
    if ai_verification is AIVerificationStatus.PASS:
        if mandatory_review_reasons:
            if auto_disposition_action is not None:
                raise ReviewRoutingError(
                    "PASS with mandatory review reasons cannot carry an auto_disposition_action"
                )
            return ReviewRoutingDecision(
                routing=ReviewRoutingStatus.REQUIRED, policy_id=policy_id,
                reason_codes=tuple(mandatory_review_reasons), system_action=None,
            )
        if auto_disposition_action is not None:
            return ReviewRoutingDecision(
                routing=ReviewRoutingStatus.NOT_REQUIRED, policy_id=policy_id,
                reason_codes=(_REASON_AUTO_AUTHORIZED,), system_action=auto_disposition_action,
            )
        # No mandatory reason and no authorized auto-disposition: fail closed.
        return ReviewRoutingDecision(
            routing=ReviewRoutingStatus.REQUIRED, policy_id=policy_id,
            reason_codes=(_REASON_NO_AUTO_POLICY,), system_action=None,
        )

    # Defensive: every AIVerificationStatus member is handled above.
    raise ReviewRoutingError(f"unhandled ai_verification {ai_verification!r}")
