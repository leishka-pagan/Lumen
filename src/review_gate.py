"""Deterministic anti-rubber-stamp human-review gate (Hero Case B).

Pure and side-effect free: no I/O, no audit, no Streamlit, no CSV reads. Given one
human-review record it decides whether the review is complete enough to finalize a
disposition, or whether it is a rubber stamp that must be blocked. The decision
pipeline (``src/pipeline.py`` Step 3) and the Case File UI evaluate reviews through
this one shared rule so enforcement and display never diverge.

Audit writes and data loading are the caller's job, never this module's. That
separation is deliberate: rendering the Case File must be able to evaluate a stored
review for display without producing any audit event.

Required fields for an acceptable (non-rubber-stamp) review, per ``src/schema.py``
``HumanReview`` and the project's governance model:
  - ``evidence_reviewed`` is True
  - ``draft_disposition`` is one of {accepted, edited, rejected}
  - ``decision_reason`` is non-empty
  - ``final_note`` is non-empty
  - ``final_action`` is non-empty
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

VALID_DISPOSITIONS: frozenset[str] = frozenset({"accepted", "edited", "rejected"})


def _is_true(value: Any) -> bool:
    """True for the boolean True or the strings 'true'/'True' (CSV-loaded booleans)."""
    if isinstance(value, bool):
        return value is True
    return str(value).strip().lower() == "true"


def _nonempty(value: Any) -> bool:
    return bool(str(value).strip()) and str(value).strip().lower() != "nan"


@dataclass(frozen=True)
class ReviewGateResult:
    """Immutable outcome of evaluating one human review through the gate."""

    review_id: str | None
    alert_id: str | None
    enforcement_enabled: bool
    allowed: bool
    blocked: bool
    missing: tuple[str, ...]      # every unmet/invalid requirement, reported together
    disposition: str | None       # the finalized disposition ONLY when allowed; else None


def evaluate_review(review: Any, enforce: bool = True) -> ReviewGateResult:
    """Evaluate one human-review record. Pure: no I/O, no audit, no Streamlit.

    review: a HumanReview-shaped mapping (dict or pandas Series; both expose ``.get``).
        ``None`` (no review on file) is treated as fully incomplete.
    enforce: whether the anti-rubber-stamp gate is enabled. When False the review is
        allowed through even if incomplete (an intentional governance bypass); the
        returned ``missing`` still lists what WOULD have failed, for transparency, and
        ``enforcement_enabled`` is False so the caller can audit the bypass.

    Fail closed: a blocked review has ``disposition is None`` and ``allowed is False`` —
    nothing is finalized. Every missing/invalid requirement is reported together; the
    check never stops at the first defect.
    """
    get = review.get if hasattr(review, "get") else (lambda _k, _d=None: None)
    review_id = get("review_id")
    alert_id = get("alert_id")
    disposition_raw = get("draft_disposition")
    disposition_valid = str(disposition_raw).strip().lower() in VALID_DISPOSITIONS

    missing: list[str] = []
    if not _is_true(get("evidence_reviewed")):
        missing.append("evidence_reviewed")
    if not disposition_valid:
        missing.append("draft_disposition")
    if not _nonempty(get("decision_reason")):
        missing.append("decision_reason")
    if not _nonempty(get("final_note")):
        missing.append("final_note")
    if not _nonempty(get("final_action")):
        missing.append("final_action")

    complete = not missing

    if not enforce:
        # Governance bypass: not blocked. Finalize the disposition when one is present.
        final = disposition_raw if disposition_valid else None
        return ReviewGateResult(review_id, alert_id, False, True, False, tuple(missing), final)

    if complete:
        return ReviewGateResult(review_id, alert_id, True, True, False, (), disposition_raw)

    # Blocked (fail closed): no finalized disposition.
    return ReviewGateResult(review_id, alert_id, True, False, True, tuple(missing), None)
