"""
Lumen Verify AML Decision Workbench — UI.

Reads the project's data/ CSVs and writes audit entries through
src.audit.log_event. Run from the project root:

    streamlit run app.py
"""

import sys
from pathlib import Path

import streamlit as st
import pandas as pd
from datetime import datetime, timezone

import json
import os


# Project root is the parent of lumen_ui/. Add it to sys.path so `src` can be
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src import audit, verifier, lifecycle_store  # noqa: E402
from src.review_gate import evaluate_review  # noqa: E402
from src.demo_reset import reset_override_demo  # noqa: E402
from src.case_lifecycle import (  # noqa: E402
    derive_queue_status, AIDraftSource, OverrideStatus, ProcessingStatus,
    ReviewGateStatus, ReviewRoutingStatus, QueueStatus,
)

st.set_page_config(
    page_title="Lumen Verify | AML Workbench",
    layout="wide",
    page_icon="",
)

# ── Paths ─────────────────────────────────────────────────────────────────────
DATA_DIR        = PROJECT_ROOT / "data"
# Tests point these at temp files (env vars) so they never touch the committed CSVs.
OVERRIDES_CSV   = Path(os.environ.get("LUMEN_OVERRIDES_CSV") or (DATA_DIR / "pending_overrides.csv"))
AUDIT_CSV       = Path(os.environ.get("LUMEN_AUDIT_LOG") or (DATA_DIR / "audit_log.csv"))
# Committed baseline snapshots restored by the "Reset Demo" control (read-only here).
BASELINE_PENDING_CSV = Path(os.environ.get("LUMEN_BASELINE_PENDING") or (DATA_DIR / "demo_baseline" / "pending_overrides.csv"))
BASELINE_AUDIT_CSV   = Path(os.environ.get("LUMEN_BASELINE_AUDIT") or (DATA_DIR / "demo_baseline" / "audit_log.csv"))
# Canonical workflow source of truth: the running UI derives every queue/Case File
# status from this, never from the legacy alerts.status trigger metadata.
LIFECYCLE_CSV   = Path(os.environ.get("LUMEN_CASE_LIFECYCLE_CSV") or (DATA_DIR / "case_lifecycle.csv"))
ALERTS_CSV      = DATA_DIR / "alerts.csv"
CUSTOMERS_CSV   = DATA_DIR / "customers.csv"
TRANSACTIONS_CSV = DATA_DIR / "transactions.csv"
EVIDENCE_CSV    = DATA_DIR / "evidence_items.csv"
PRIOR_CASES_CSV = DATA_DIR / "prior_cases.csv"
KYC_STATUS_CSV  = DATA_DIR / "kyc_profile_status.csv"
AI_OUTPUTS_CSV  = DATA_DIR / "ai_outputs.csv"
HUMAN_REVIEWS_CSV = DATA_DIR / "human_reviews.csv"
AUDIT_LOG_CSV   = DATA_DIR / "audit_log.csv"

REQUIRED = [ALERTS_CSV, CUSTOMERS_CSV, TRANSACTIONS_CSV, EVIDENCE_CSV,
            PRIOR_CASES_CSV, KYC_STATUS_CSV, AI_OUTPUTS_CSV, HUMAN_REVIEWS_CSV]

# ── Guard: check data files exist ────────────────────────────────────────────
missing = [p for p in REQUIRED if not p.exists()]
if missing:
    st.error(
        "Data files not found: "
        + ", ".join(str(p.relative_to(PROJECT_ROOT)) for p in missing)
        + ". Run the app from the project root: `streamlit run app.py`, "
        "and make sure data/ has been generated."
    )
    st.stop()

ANALYSTS = {
    "EMP-003": {"id": "EMP-003", "name": "S. Mayekar", "rank": "Analyst"},
    "EMP-001": {"id": "EMP-001", "name": "L. Pagan", "rank": "Lead Analyst"},
    "EMP-006": {"id": "EMP-006", "name": "M. Chen", "rank": "Compliance Manager"},
}

SEVERITY_LABELS = {"high": "High", "med": "Medium", "low": "Low"}

# Every enum vocabulary an override's field_changed can touch, so History
# never shows a mix of "Medium" (mapped) next to "open" (raw).
FIELD_ENUM_LABELS = {
    "severity":    {"high": "High", "med": "Medium", "medium": "Medium", "low": "Low"},
    "risk_rating": {"high": "High", "medium": "Medium", "low": "Low"},
    "disposition": {
        "open": "Open", "escalate": "Escalate", "accepted": "Accepted",
        "edited": "Edited", "rejected": "Rejected", "monitor": "Monitor",
    },
}

def sev_label(field: str, value) -> str:
    """Human label for an override's old/new value, mapped through its field's enum."""
    return FIELD_ENUM_LABELS.get(field, {}).get(str(value).lower(), str(value))
STATUS_LABELS = {"open": "Pending Review", "in_review": "In Progress", "closed": "Closed"}

def write_log(action: str, details: dict, alert_id: str | None = None) -> dict:
    emp = st.session_state.get("current_user", {})
    return audit.log_event(
        actor=f"ui:{emp.get('id', 'UNKNOWN')}",
        action=action,
        alert_id=alert_id,
        details=details,
    )


# DISPLAY-ONLY override request identifiers. The seeded demo requests carry internal
# change_ids that read as scaffolding; these are the intentional identifiers shown to a
# user. This map is presentation only — it is NEVER written to a CSV, an audit event, a
# widget key, session state, or a lookup. Every callback and every persisted record keeps
# the internal change_id. Any id not listed here (a live request, or historical data)
# displays unchanged.
OVERRIDE_DISPLAY_IDS = {
    "CHG-SEED-001": "OVR-001",
    "CHG-SEED-002": "OVR-002",
    "CHG-SEED-003": "OVR-003",
}


def override_display_id(change_id) -> str:
    """The identifier to SHOW for an override request. Display only — never persisted,
    never used for lookup, callbacks, or keys. Unmapped ids pass through untouched."""
    if change_id is None:
        return ""
    return OVERRIDE_DISPLAY_IDS.get(str(change_id), str(change_id))


def load_overrides() -> pd.DataFrame:
    cols = [
        "change_id", "alert_id", "field_changed", "old_value", "new_value",
        "changed_by_id", "changed_by_name", "changed_at", "reason",
        "status", "reviewed_by", "reviewed_at", "review_note",
    ]
    if OVERRIDES_CSV.exists():
        return pd.read_csv(OVERRIDES_CSV, dtype=str, keep_default_na=False)
    return pd.DataFrame(columns=cols)


def save_override(alert_id, field, old_val, new_val, reason) -> str:
    df  = load_overrides()
    emp = st.session_state.current_user
    ts  = datetime.now(timezone.utc).isoformat()
    cid = f"CHG-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{alert_id}"
    row = {
        "change_id":       cid,
        "alert_id":        alert_id,
        "field_changed":   field,
        "old_value":       old_val,
        "new_value":       new_val,
        "changed_by_id":   emp["id"],
        "changed_by_name": emp["name"],
        "changed_at":      ts,
        "reason":          reason,
        "status":          "pending",
        "reviewed_by":     "",
        "reviewed_at":     "",
    }
    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    df.to_csv(OVERRIDES_CSV, index=False)
    write_log("field_override", {
        "field_changed":   field,
        "old_value":       str(old_val),
        "new_value":       str(new_val),
        "reason":          reason,
        "override_status": "pending_manager_review",
    }, alert_id=alert_id)
    return cid


def update_override_status(change_id: str, status: str, reviewer: str) -> None:
    df = load_overrides()
    mask = df["change_id"] == change_id
    alert_id = df.loc[mask, "alert_id"].iloc[0] if mask.any() else None
    df.loc[mask, "status"]      = status
    df.loc[mask, "reviewed_by"] = reviewer
    df.loc[mask, "reviewed_at"] = datetime.now(timezone.utc).isoformat()
    df.to_csv(OVERRIDES_CSV, index=False)
    write_log("override_review", {
        "change_id": change_id,
        "decision":  status,
        "reviewer":  reviewer,
    }, alert_id=alert_id)


def record_override_decision(change_id, decision, reviewer, rationale, actor,
                             overrides_path=None, log_path=None, lifecycle_path=None):
    """Persist one manager Approve/Reject decision on a pending override, synchronize
    the canonical case lifecycle, and write a single audit event. Paths are injectable
    so tests never touch committed CSVs (env-var defaults keep production writing to
    data/). Returns the alert_id.

    Lifecycle-first: the updated CaseLifecycle for this alert is validated and atomically
    persisted BEFORE pending_overrides.csv or the audit log is touched. If lifecycle
    validation fails (no record, override-request mismatch, or invariant violation),
    this raises and leaves pending_overrides.csv and audit_log.csv unchanged.

    Updates status / reviewed_by / reviewed_at / review_note on the matching row.
    Audit details include change_id, decision, rationale, old_value, new_value.
    """
    opath = Path(overrides_path) if overrides_path else OVERRIDES_CSV
    lpath = (Path(log_path) if log_path
             else (Path(os.environ["LUMEN_AUDIT_LOG"]) if os.environ.get("LUMEN_AUDIT_LOG") else None))
    df = pd.read_csv(opath, dtype=str, keep_default_na=False)
    if "review_note" not in df.columns:
        df["review_note"] = ""
    mask = df["change_id"] == change_id
    if not mask.any():
        return None
    row = df.loc[mask].iloc[0].to_dict()

    # Lifecycle-first: validate + atomically persist the updated CaseLifecycle for this
    # alert before mutating the override CSV or the audit log. Any failure raises here,
    # leaving both workflow files untouched.
    lifecycle_store.apply_override_decision(
        alert_id=row.get("alert_id"),
        change_id=change_id,
        decision=decision,
        field_changed=row.get("field_changed"),
        new_value=row.get("new_value"),
        path=lifecycle_path,
    )

    df.loc[mask, "status"]      = decision
    df.loc[mask, "reviewed_by"] = reviewer
    df.loc[mask, "reviewed_at"] = datetime.now(timezone.utc).isoformat()
    df.loc[mask, "review_note"] = rationale
    df.to_csv(opath, index=False)
    audit.log_event(
        actor=actor,
        action="override_review",
        alert_id=row.get("alert_id"),
        details={
            "change_id": change_id,
            "decision":  decision,
            "rationale": rationale,
            "old_value": row.get("old_value"),
            "new_value": row.get("new_value"),
            "reviewer":  reviewer,
        },
        log_path=lpath,
    )
    return row.get("alert_id")


OVERRIDE_RATIONALE_MAX = 500


def override_rationale_valid(text) -> bool:
    """Valid = non-empty after trimming whitespace (max length is enforced by the
    textarea's max_chars). No arbitrary minimum length."""
    return bool(str(text or "").strip())


def _close_override_dialog():
    st.session_state.pending_override = None
    st.rerun()


def _render_override_decision(decision: str):
    """Shared body for the Approve/Reject dialogs. The rationale and BOTH actions live
    inside one st.form, so the typed value is submitted together with the click. This
    fixes the real-browser bug where a disabled= button never saw the typed value (the
    counter never updated and the button never enabled). No disabled= on the submit
    buttons — they are always clickable; validation happens on submit."""
    po = st.session_state.get("pending_override")
    if not po:
        return
    change_id = po.get("change_id")
    match = load_overrides()
    match = match[match["change_id"] == change_id]
    if match.empty:
        _close_override_dialog()
        return
    row = match.iloc[0].to_dict()
    confirm_label = "Approve Override" if decision == "approved" else "Reject Override"
    with st.container(key="override_decision_dialog"):
        st.markdown(
            '<div class="ovd-summary">'
            f'<div class="ovd-row"><span class="ovd-lbl">Request ID</span><span class="ovd-val">{override_display_id(row.get("change_id")) or "—"}</span></div>'
            f'<div class="ovd-row"><span class="ovd-lbl">Alert ID</span><span class="ovd-val">{row.get("alert_id","—")}</span></div>'
            f'<div class="ovd-row"><span class="ovd-lbl">Requested change</span>'
            f'<span class="ovd-val">{sev_label(row["field_changed"], row["old_value"])} → {sev_label(row["field_changed"], row["new_value"])}</span></div>'
            f'<div class="ovd-row"><span class="ovd-lbl">Submitted by</span>'
            f'<span class="ovd-val">{row.get("changed_by_name","—")} ({row.get("changed_by_id","—")})</span></div>'
            f'<div class="ovd-row"><span class="ovd-lbl">Analyst request reason</span>'
            f'<span class="ovd-val">{row.get("reason","—")}</span></div>'
            '</div>',
            unsafe_allow_html=True,
        )
        with st.form(key=f"override_decision_form_{change_id}_{decision}",
                     clear_on_submit=False, border=False):
            rationale = st.text_area(
                "Manager decision rationale",
                key=f"override_rationale_{change_id}_{decision}",
                placeholder="Document why this override should be approved or rejected…",
                height=120,
                max_chars=OVERRIDE_RATIONALE_MAX,
            )
            st.markdown(
                '<div class="ovd-help">Required • Maximum 500 characters</div>',
                unsafe_allow_html=True,
            )
            cancel_col, confirm_col = st.columns(2)
            with cancel_col:
                cancelled = st.form_submit_button(
                    "Cancel", type="secondary", use_container_width=True,
                    key=f"override_submit_cancel_{change_id}_{decision}",
                )
            with confirm_col:
                confirmed = st.form_submit_button(
                    confirm_label, type="primary", use_container_width=True,
                    key=f"override_submit_confirm_{change_id}_{decision}",
                )
        if cancelled:
            _close_override_dialog()
        elif confirmed:
            _trimmed = str(rationale or "").strip()
            if not _trimmed:
                st.error("Manager decision rationale is required.")
            else:
                emp = st.session_state.current_user
                record_override_decision(
                    change_id, decision, reviewer=emp["name"],
                    rationale=_trimmed, actor=f"ui:{emp['id']}",
                )
                st.session_state.override_toast = decision
                st.session_state._force_override_view = True   # deferred: keep this subview
                _close_override_dialog()


@st.dialog("Approve Override Request")
def _approve_override_dialog():
    _render_override_decision("approved")


@st.dialog("Reject Override Request")
def _reject_override_dialog():
    _render_override_decision("rejected")


# ─────────────────────────────────────────────────────────────────────────────
# Dialog-open helpers. Streamlit raises "Only one dialog is allowed to be opened
# at the same time" if two @st.dialog functions are invoked in one rerun, so every
# action that opens a modal clears the OTHER modals' state before setting its own.
# Combined with the single if/elif/elif coordinator (end of file), this guarantees
# at most one dialog per run. These set state only — they neither read nor write
# any CSV and emit no audit event. (The Approve/Reject decision lives inside
# `pending_override["decision"]`, so clearing pending_override clears it too.)
# ─────────────────────────────────────────────────────────────────────────────
def _open_case_file(alert_id):
    """Route every Case File open through here: clear override + reset state first."""
    st.session_state.pending_override = None
    st.session_state.demo_reset_confirm = False
    st.session_state.open_case = alert_id


def _open_override_decision(change_id, decision):
    """Open the Approve/Reject decision dialog, clearing Case File + reset state."""
    st.session_state.open_case = None
    st.session_state.selected_alert = None
    st.session_state.demo_reset_confirm = False
    st.session_state.pending_override = {"change_id": change_id, "decision": decision}


def _open_demo_reset():
    """Open the Reset Demo confirmation, clearing Case File + override state."""
    st.session_state.open_case = None
    st.session_state.selected_alert = None
    st.session_state.pending_override = None
    st.session_state.demo_reset_confirm = True


def get_approved_overrides() -> dict:
    df = load_overrides()
    if df.empty:
        return {}
    result = {}
    for _, row in df[df["status"] == "approved"].iterrows():
        result.setdefault(row["alert_id"], {})[row["field_changed"]] = row["new_value"]
    return result


@st.cache_data
def load_source_tables(cache_key: float) -> dict:
    def read(path):
        return pd.read_csv(path, dtype=str, keep_default_na=False)

    return {
        "customers": read(CUSTOMERS_CSV),
        "transactions": read(TRANSACTIONS_CSV),
        "alerts": read(ALERTS_CSV),
        "evidence_items": read(EVIDENCE_CSV),
        "prior_cases": read(PRIOR_CASES_CSV),
        "kyc_profile_status": read(KYC_STATUS_CSV),
        "ai_outputs": read(AI_OUTPUTS_CSV),
        "human_reviews": read(HUMAN_REVIEWS_CSV),
    }


def mtimes_key() -> float:
    return sum(p.stat().st_mtime for p in REQUIRED)


def case_readiness_pct(alert_id: str, source: dict) -> int:
    ev = source["evidence_items"]
    rows = ev[ev["alert_id"] == alert_id]
    if len(rows) == 0:
        return 0
    available = sum(1 for v in rows["available"] if str(v).strip().lower() == "true")
    return round(100 * available / len(rows))


# ── Canonical lifecycle → display mappings (single source of truth for the UI) ──
# Queue status label per derive_queue_status outcome.
QUEUE_STATUS_LABELS = {
    QueueStatus.NOT_PROCESSED:    "Not Processed",
    QueueStatus.PROCESSING_ERROR: "Processing Error",
    QueueStatus.AWAITING_MANAGER: "Awaiting Manager",
    QueueStatus.AWAITING_REVIEW:  "Awaiting Review",
    QueueStatus.BLOCKED:          "Blocked",
    QueueStatus.CLOSED:           "Closed",
}
# AI-verification rail label per lifecycle.ai_verification value.
AI_VERIF_LABELS = {"not_evaluated": "NOT EVALUATED", "pass": "PASS",
                   "mixed": "MIXED", "fail": "FAIL"}


def lifecycle_queue_label(lc) -> str:
    return QUEUE_STATUS_LABELS[derive_queue_status(lc)]


def lifecycle_ai_verification_label(lc) -> str:
    return AI_VERIF_LABELS.get(lc.ai_verification.value, lc.ai_verification.value.upper())


def lifecycle_review_requirements_label(lc) -> str:
    """Review-requirements rail value derived from the canonical lifecycle."""
    if lc.processing_status is ProcessingStatus.NOT_PROCESSED:
        return "NOT PROCESSED"
    if lc.processing_status is ProcessingStatus.ERROR:
        return "PROCESSING ERROR"
    if lc.review_routing is ReviewRoutingStatus.NOT_REQUIRED:
        return "NOT REQUIRED"
    if lc.review_gate is ReviewGateStatus.PENDING:
        return "PENDING"
    if lc.review_gate is ReviewGateStatus.COMPLETE:
        return "COMPLETE"
    if lc.review_gate is ReviewGateStatus.BLOCKED:
        return "BLOCKED"
    return "—"


def lifecycle_recorded_disposition_label(lc) -> str:
    """Recorded disposition from lifecycle.final_action (never the review decision)."""
    return lc.final_action.upper() if lc.final_action else "NONE"


def lifecycle_draft_source_label(lc) -> str:
    """Alert Inventory DRAFT column — derived ONLY from lifecycle.ai_draft_source (never
    ai_outputs presence, verification, processing status, or alerts.status).
    synthetic_fixture -> FIXTURE; captured_live (with a model id) -> LIVE AI; none -> ''
    (rendered as an em dash). A fixture never shows LIVE AI; LIVE AI requires a real
    captured-live draft with a model id on a valid record."""
    if lc.ai_draft_source is AIDraftSource.SYNTHETIC_FIXTURE:
        return "FIXTURE"
    if lc.ai_draft_source is AIDraftSource.CAPTURED_LIVE and lc.model_id:
        return "LIVE AI"
    return ""


def load_lifecycle_index(alert_ids) -> dict:
    """Load + validate the canonical lifecycle and index by alert_id. Fail closed:
    any missing/invalid/duplicate record, or an alert with no lifecycle record, is an
    explicit application error. NEVER falls back to alerts.status."""
    try:
        records = lifecycle_store.load_lifecycle(LIFECYCLE_CSV)
    except Exception as exc:  # missing file, invalid row, duplicate alert_id, bad schema
        st.error(
            "Case lifecycle data could not be loaded, so the workbench cannot derive "
            f"workflow status ({type(exc).__name__}: {exc}). Restore "
            "data/case_lifecycle.csv (or use Reset Demo) and reload — the UI will not "
            "fall back to legacy alert status."
        )
        st.stop()
    index = {r.alert_id: r for r in records}
    missing = [a for a in alert_ids if a not in index]
    if missing:
        st.error(
            "Case lifecycle is missing records for: " + ", ".join(missing) +
            ". Every alert must have a canonical lifecycle record; the workbench will "
            "not infer a processed state from other data."
        )
        st.stop()
    return index


def build_queue_row(alert_row: pd.Series, source: dict, lifecycle_index: dict) -> dict:
    alert_id = alert_row["alert_id"]
    customers = source["customers"]
    crow = customers[customers["customer_id"] == alert_row["customer_id"]]
    customer_name = crow.iloc[0]["name"] if len(crow) else alert_row["customer_id"]

    review_rows = source["human_reviews"][source["human_reviews"]["alert_id"] == alert_id]
    analyst = review_rows.iloc[0]["reviewer"] if len(review_rows) else "Unassigned"

    lc = lifecycle_index[alert_id]
    return {
        "alert_id": alert_id,
        "customer_id": alert_row["customer_id"],
        "customer": customer_name,
        "rule": alert_row["rule_triggered"],
        "severity": SEVERITY_LABELS.get(alert_row["severity"], alert_row["severity"]),
        "readiness": case_readiness_pct(alert_id, source),
        # DRAFT source — canonical lifecycle only (never ai_outputs presence).
        "draft": lifecycle_draft_source_label(lc),
        # Canonical workflow status — derived from case_lifecycle.csv, never alerts.status.
        "status": lifecycle_queue_label(lc),
        "analyst": analyst,
    }


def case_target_for_override(override_row, valid_alert_ids) -> str | None:
    """Alert whose Case File the Manager Review 'Open Case File' action opens.

    Returns the override's OWN alert_id when it refers to a real alert, so the
    manager inspects the evidence behind that specific override before approving
    or rejecting it. Returns None when the alert_id is missing or dangling, so no
    broken 'Open Case File' control is shown. Pure and testable: no Streamlit, no
    I/O. Accepts a dict or a pandas Series (both expose .get).
    """
    aid = override_row.get("alert_id")
    return aid if aid in set(valid_alert_ids) else None


def derive_ai_verification(ai_claims: list) -> str:
    """One-line AI-verification result for an alert's claims. Mirrors the Case File
    summary derivation exactly (display-only; reads results, changes nothing):
    PASS only -> PASS; FAIL only -> FAIL; PASS and FAIL -> MIXED; unresolved
    without FAIL -> NEEDS REVIEW; no claims -> NOT EVALUATED.
    """
    results = [cl["result"] for cl in ai_claims]
    if not results:
        return "NOT EVALUATED"
    if "FAIL" in results:
        return "MIXED" if "PASS" in results else "FAIL"
    if any(r not in ("PASS", "FAIL") for r in results):
        return "NEEDS REVIEW"
    return "PASS"


def get_case_detail(alert_id: str, source: dict) -> dict:
    alerts = source["alerts"]
    arow = alerts[alerts["alert_id"] == alert_id].iloc[0]
    customer_id = arow["customer_id"]

    customers = source["customers"]
    crow = customers[customers["customer_id"] == customer_id]
    crow = crow.iloc[0] if len(crow) else None

    prior = source["prior_cases"]
    prow = prior[prior["customer_id"] == customer_id]
    prior_sar = int(prow.iloc[0]["prior_sar_count"]) if len(prow) else 0

    kyc = source["kyc_profile_status"]
    krow = kyc[kyc["customer_id"] == customer_id]
    kyc_current = krow.iloc[0]["current_within_12mo"] if len(krow) else "unknown"

    txns = source["transactions"]
    trows = txns[txns["customer_id"] == customer_id].to_dict("records")

    ev = source["evidence_items"]
    erows = ev[ev["alert_id"] == alert_id].to_dict("records")
    missing = [e["item_type"] for e in erows if str(e["available"]).strip().lower() != "true"]

    claims = source["ai_outputs"][source["ai_outputs"]["alert_id"] == alert_id].to_dict("records")
    ai_claims = []
    for c in claims:
        c = dict(c)
        c["evidence_refs"] = json.loads(c["evidence_refs"]) if c.get("evidence_refs") else []
        result = verifier.verify_claim(c, source)
        ai_claims.append({
            "id": c["claim_id"],
            "type": c["claim_type"],
            "asserted_value": c["asserted_value"],
            "result": result.status,
            "note": result.reason,
        })

    review_rows = source["human_reviews"][source["human_reviews"]["alert_id"] == alert_id]
    review = review_rows.iloc[0].to_dict() if len(review_rows) else None

    return {
        "alert": arow.to_dict(),
        "customer": crow.to_dict() if crow is not None else {},
        "prior_sar": prior_sar,
        "kyc_current_within_12mo": kyc_current,
        "transactions": trows,
        "evidence_items": erows,
        "missing": missing,
        "ai_claims": ai_claims,
        "review": review,
        "readiness": case_readiness_pct(alert_id, source),
    }


# ─────────────────────────────────────────────────────────────────────────────
# LOAD DATA
# ─────────────────────────────────────────────────────────────────────────────
source = load_source_tables(mtimes_key())
alerts_df = source["alerts"]

# Canonical workflow state — loaded FRESH each run (not cached) so an override decision
# or Reset Demo is reflected on the next rerun. Fail-closed if it cannot be loaded.
lifecycle_index = load_lifecycle_index(alerts_df["alert_id"].tolist())

queue_df = pd.DataFrame([build_queue_row(r, source, lifecycle_index) for _, r in alerts_df.iterrows()])
if queue_df.empty:
    st.warning("No alerts to display. Check that data/alerts.csv has content.")

approved = get_approved_overrides()
display_df = queue_df.copy()
for aid, fields in approved.items():
    for field, val in fields.items():
        if field == "severity":
            val = SEVERITY_LABELS.get(val, val)
        display_df.loc[display_df["alert_id"] == aid, field] = val

# ─────────────────────────────────────────────────────────────────────────────
# SESSION STATE
# ─────────────────────────────────────────────────────────────────────────────
if "session_id" not in st.session_state:
    st.session_state.session_id = f"SESSION-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"

if "current_user" not in st.session_state:
    st.session_state.current_user = ANALYSTS["EMP-003"]

if "view_as"           not in st.session_state: st.session_state.view_as           = "Analyst"
if "edit_mode"         not in st.session_state: st.session_state.edit_mode         = False
if "staged_edits"      not in st.session_state: st.session_state.staged_edits      = {}
if "edit_row"          not in st.session_state: st.session_state.edit_row          = None
if "selected_alert"    not in st.session_state: st.session_state.selected_alert    = None
if "case_search"       not in st.session_state: st.session_state.case_search       = None
if "open_case"         not in st.session_state: st.session_state.open_case         = None
if "settings_cl"       not in st.session_state: st.session_state.settings_cl       = []

_qp_alert = st.query_params.get("alert")
if _qp_alert and _qp_alert in alerts_df["alert_id"].values:
    st.session_state.selected_alert = _qp_alert
    _open_case_file(_qp_alert)          # clears conflicting modal state, then sets open_case
    st.query_params.pop("alert", None)

if "risk_settings" not in st.session_state:
    st.session_state.risk_settings = {
        "high_threshold":             80,
        "medium_threshold":           50,
        "kyc_staleness_months":       12,
        "txn_history_days":           90,
        "require_counterparty_id":    True,
        "require_prior_sar_check":    True,
        "ai_draft_requires_readiness":True,
        "block_rubber_stamp":         True,
    }

if "keywords" not in st.session_state:
    st.session_state.keywords = {
        "Rapid Movement":             ["same-day transfer", "pass-through", "wire in wire out"],
        "Structuring":                ["just under 10k", "smurfing", "CTR avoidance", "split deposits"],
        "Expected Activity Mismatch": ["unexpected wire", "income inconsistent", "student account"],
        "High-Risk Jurisdiction":     ["sanctioned country", "OFAC", "high-risk region"],
        "KYC Drift":                  ["stale profile", "no update", "4 year gap"],
        "Unusual Volume":             ["volume spike", "sudden increase", "above monthly avg"],
        "Prior SAR History":          ["prior SAR", "previous filing", "repeat subject"],
    }

# ─────────────────────────────────────────────────────────────────────────────
# CSS
# ─────────────────────────────────────────────────────────────────────────────
st.html("""
<style>
*,html,body,[class*="css"]{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif !important;box-sizing:border-box;}
.stApp{background:#F4F6F8;color:#17202A;}
.stMainBlockContainer{padding:0 !important;max-width:100% !important;}
header[data-testid="stHeader"]{display:none !important;}
div[data-testid="stToolbar"]{display:none !important;}
#MainMenu{display:none !important;}

/* OBSIDIAN PLUM header — brand row (.id-bar), context row (.sub-nav), pending pill.
   Styling only; the accessible non-h1 brand, stColumn scoping and mobile structure
   from 66dbb92 are preserved unchanged. */
.id-bar{background:linear-gradient(135deg,#111827 0%,#1F2937 62%,#26313F 100%);color:#FFFFFF;
  min-height:92px;padding:0 30px;margin:0;gap:28px;
  display:flex;align-items:center;justify-content:space-between;
  border-bottom:1px solid #334155;border-radius:0;box-shadow:0 10px 28px rgba(17,24,39,0.24);}
.id-bar-logo{color:#FFFFFF;background:transparent;font-size:38px;font-weight:850;line-height:1;
  letter-spacing:-1px;padding:0;margin:0;border:none;border-radius:0;box-shadow:none;white-space:nowrap;}
.id-bar-logo span{color:#CBD5E1;font-size:inherit;font-weight:850;}
.id-bar-right{display:flex;align-items:center;gap:12px;}
.id-bar-right a{color:#F8FAFC;text-decoration:none;}
.id-bar-right a:hover{text-decoration:underline;}
.id-bar-user{background:rgba(255,255,255,0.06);border:1px solid #475569;color:#F8FAFC;padding:9px 14px;margin:0;
  border-radius:10px;box-shadow:inset 0 1px 0 rgba(255,255,255,0.08),0 4px 12px rgba(0,0,0,0.14);
  font-size:13px;font-weight:750;line-height:1.2;letter-spacing:0.1px;white-space:nowrap;}

.sub-nav{background:#1F2937;color:#CBD5E1;min-height:46px;height:auto;padding:0 30px;margin:0;gap:16px;
  display:flex;align-items:center;justify-content:space-between;
  border-bottom:1px solid #334155;border-radius:0;box-shadow:none;overflow:visible;}
.sub-nav-left{color:#F8FAFC;font-size:13px;font-weight:750;line-height:1.3;letter-spacing:0.2px;white-space:nowrap;}
.sub-nav-right{color:#CBD5E1;font-size:12px;font-weight:600;line-height:1.3;gap:12px;
  display:flex;align-items:center;justify-content:flex-end;white-space:nowrap;font-variant-numeric:tabular-nums;}
.hdr-pending{display:inline-flex;align-items:center;gap:6px;background:#F5E9E2;border:1px solid #C58B6C;
  color:#6A3D2A;font-size:12px;font-weight:800;line-height:1.1;letter-spacing:0.1px;
  padding:5px 10px;margin:0;border-radius:999px;box-shadow:none;}
.hdr-pending .dot{width:6px;height:6px;border-radius:50%;background:#9B5438;border:none;box-shadow:none;display:inline-block;}

.page-body{padding:12px 26px 20px 26px;}
.section-h{font-size:18px !important;font-weight:850 !important;color:#17202A !important;letter-spacing:-0.2px !important;margin:0 0 12px 0 !important;padding:0 !important;}
.section-h .section-count{color:#667085;font-weight:600;font-size:14px;}

/* MIDNIGHT LUMEN — demo-role banner (informational; wording unchanged) */
.role-bar{background:#FFFFFF;border:1px solid #D0D5DD;border-left:5px solid #475569;color:#334155;
  min-height:48px;padding:11px 15px;margin:0;gap:8px;border-radius:11px;box-shadow:0 5px 14px rgba(17,24,39,0.10);
  font-size:13px;font-weight:550;line-height:1.35;display:flex;align-items:center;}
.role-bar b,.role-bar strong{color:#17202A;font-weight:800;}
.pending-badge{display:inline-flex;align-items:center;gap:6px;background:#fde8e8;border:1px solid #d99;border-radius:5px;padding:10px 14px;font-size:14px;font-weight:700;color:#a01818;justify-content:center;}

.metric-strip{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:18px;}
.metric-cell{background:#FFFFFF;padding:16px 20px;border:1px solid #D0D5DD;border-top:4px solid #475569;border-radius:6px;box-shadow:0 1px 2px rgba(17,24,39,.05);}
.metric-cell-label{font-size:12px;font-weight:700;color:#4a5560;letter-spacing:.06em;text-transform:uppercase;margin-bottom:6px;}
.metric-cell-value{font-size:34px;font-weight:800;line-height:1;color:#1a1a1a;font-variant-numeric:tabular-nums;}
.metric-cell-value.danger{color:#a01818;}
.metric-cell-value.warn{color:#8a5600;}
.metric-cell-value.info{color:#3F4C5E;}
.metric-cell-sub{font-size:12px;color:#5a6570;margin-top:5px;font-weight:500;}

.panel{background:#fff;border:1px solid #D0D5DD;border-radius:6px;margin-bottom:16px;overflow:hidden;}
.panel-header{background:#F4F6F8;border-bottom:1px solid #D0D5DD;padding:11px 18px;display:flex;justify-content:space-between;align-items:center;}
.panel-title{font-size:17px;font-weight:700;color:#17202A;}
.panel-subtitle{font-size:13px;color:#5a6570;}

.data-table{width:100%;border-collapse:collapse;font-size:13px;}
.data-table thead tr{background:linear-gradient(180deg,#374151 0%,#2B3139 100%);}
.data-table th{padding:8px 12px;text-align:left;font-size:12px;font-weight:850;color:#FFFFFF;letter-spacing:.2px;border-right:1px solid #46515F;border-bottom:none;white-space:nowrap;}
.data-table th:last-child{border-right:none;}
.data-table td{padding:8px 12px;border-bottom:1px solid #e8e8e8;border-right:1px solid #f0f0f0;color:#1a1a1a;vertical-align:middle;}
.data-table td:last-child{border-right:none;}
.data-table tbody tr:nth-child(even) td{background:#F4F6F8;}
.data-table tbody tr:nth-child(odd) td{background:#fff;}
.data-table tbody tr:hover td{background:#ddeeff !important;cursor:pointer;}
.data-table tbody tr.selected td{background:#EEF1F4 !important;border-left:3px solid #475569;}
.data-table tbody tr.has-pending td{border-left:3px solid #f0c040;}

.badge{display:inline-block;padding:2px 8px;font-size:11px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;border-radius:3px;border:1px solid;}
.b-high{background:#fde8e8;color:#7b0000;border-color:#c88;}
.b-medium{background:#fef3e2;color:#6b3800;border-color:#dba;}
.b-low{background:#e8f5e8;color:#1a5c1a;border-color:#9c9;}
.b-pending{background:#e8eeff;color:#1a2e8c;border-color:#99a;}
.b-progress{background:#e8f5e8;color:#1a5c1a;border-color:#9c9;}
.b-closed{background:#f0f0f0;color:#555;border-color:#bbb;}
.b-staged{background:#fff8e1;color:#7d4e00;border-color:#f0c040;}

.rb{display:flex;align-items:center;gap:6px;}
.rb-t{width:70px;height:8px;background:#ddd;border:1px solid #bbb;border-radius:1px;overflow:hidden;}
.rb-v{font-size:11px;font-weight:700;color:#333;min-width:30px;font-variant-numeric:tabular-nums;}

.edit-form{background:#F4F6F8;border:1px solid #D0D5DD;border-left:4px solid #475569;padding:14px 16px;margin:6px 0 10px 0;}
.edit-form-title{font-size:13px;font-weight:800;color:#334155;letter-spacing:.08em;text-transform:uppercase;margin-bottom:12px;}

.case-panel{background:#fff;border:1px solid #b0b0b0;margin-top:14px;}
.case-panel-hdr{background:linear-gradient(180deg,#374151 0%,#2B3139 100%);padding:8px 14px;display:flex;justify-content:space-between;align-items:center;}
.case-panel-title{font-size:13px;font-weight:700;color:#fff;}
.case-panel-id{font-size:11px;color:#a8c4e0;}
.case-grid{display:grid;grid-template-columns:1fr 1fr;}
.case-section{padding:14px 16px;border-right:1px solid #e8e8e8;border-bottom:1px solid #e8e8e8;}
.case-section:nth-child(even){border-right:none;}
.case-section-title{font-size:11px;font-weight:800;color:#334155;letter-spacing:.1em;text-transform:uppercase;margin-bottom:10px;padding-bottom:5px;border-bottom:1px solid #D0D5DD;}
.field-row{display:flex;justify-content:space-between;margin-bottom:6px;font-size:13px;}
.field-lbl{color:#555;font-weight:500;}
.field-val{color:#1a1a1a;font-weight:600;text-align:right;}
.verify-row{display:flex;justify-content:space-between;align-items:center;padding:6px 10px;margin:3px 0;background:#f8f8f8;border:1px solid #e8e8e8;font-size:13px;}
.v-pass{color:#1a5c1a;font-weight:700;font-size:11px;background:#e8f5e8;padding:2px 6px;border:1px solid #9c9;border-radius:2px;}
.v-fail{color:#7b0000;font-weight:700;font-size:11px;background:#fde8e8;padding:2px 6px;border:1px solid #c88;border-radius:2px;}
.v-review{color:#6b3800;font-weight:700;font-size:11px;background:#fef3e2;padding:2px 6px;border:1px solid #dba;border-radius:2px;}
.warn-box{background:#fff8e1;border:1px solid #f0c040;border-left:4px solid #f0c040;padding:12px 16px;font-size:14px;color:#5d4000;margin:8px 0 0 0;}

.claim-card{background:#fff;border:1px solid #d8d8d8;border-left:5px solid #999;margin:0 0 12px 0;}
.claim-card.fail{border-left-color:#b03a2e;}
.claim-card.pass{border-left-color:#1e8449;}
.claim-card.review{border-left-color:#b9770e;}
.claim-line{display:flex;gap:12px;padding:8px 14px;font-size:13px;align-items:baseline;border-bottom:1px solid #ececec;}
.claim-line .claim-tag{font-size:11px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:#777;min-width:110px;flex-shrink:0;}
.claim-line .claim-val{color:#1a1a1a !important;font-weight:600;}
.claim-result-line{display:flex;justify-content:space-between;align-items:center;padding:9px 14px;background:#f2f2f2;}
.claim-result-line .claim-tag{font-size:11px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:#555;}

.review-meta{display:flex;gap:28px;flex-wrap:wrap;padding-bottom:12px;margin-bottom:12px;border-bottom:1px solid #e6e6e6;}
.review-meta>div{display:flex;flex-direction:column;gap:2px;}
.review-lbl{font-size:11px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:#777;}
.review-val{font-size:14px;font-weight:600;color:#1a1a1a;}
.review-field{margin-top:10px;}
.review-text{margin:3px 0 0 0;font-size:13px;line-height:1.5;color:#2a2a2a;}

.settings-section-title{font-size:13px;font-weight:800;color:#334155;letter-spacing:.06em;text-transform:uppercase;margin-bottom:10px;}
.field-desc-txt{font-size:13px;color:#333;margin:2px 0 8px 0;line-height:1.45;}
.kw-chip{display:inline-block;padding:3px 9px;background:#e8eeff;color:#1a2e8c;border:1px solid #99aacc;border-radius:3px;font-size:13px;font-weight:500;margin:2px;}

/* FIX 4: log-tbl and badges — all bumped to 13px for readability */
.log-tbl{width:100%;border-collapse:collapse;font-size:13px;border:1px solid #D0D5DD;}
.log-tbl th{padding:10px 14px;background:linear-gradient(180deg,#374151 0%,#2B3139 100%);border-bottom:none;font-size:12px;font-weight:850;color:#FFFFFF;text-transform:uppercase;letter-spacing:.05em;text-align:left;}
.log-tbl td{padding:9px 14px;border-bottom:1px solid #e8e8e8;color:#2a2a2a;font-size:13px;}
.log-tbl tbody tr:nth-child(even) td{background:#f7f9fc;}
.lt-change{background:#fef3e2;color:#6b3800;border:1px solid #dba;padding:3px 9px;border-radius:3px;font-size:13px;font-weight:700;}
.lt-add{background:#e8f5e8;color:#1a5c1a;border:1px solid #9c9;padding:3px 9px;border-radius:3px;font-size:13px;font-weight:700;}
.lt-remove{background:#fde8e8;color:#7b0000;border:1px solid #c88;padding:3px 9px;border-radius:3px;font-size:13px;font-weight:700;}

div[data-testid="stTabs"]>div:first-child{background:transparent !important;border-bottom:none !important;padding:0 !important;gap:0 !important;}
div[data-testid="stTabs"]>div:nth-child(2){background:transparent !important;}
div[data-testid="stNumberInput"] input{background:#FFFFFF !important;border:1px solid #98A2B3 !important;border-radius:8px !important;font-size:13px !important;color:#17202A !important;font-weight:600 !important;}
div[data-testid="stTextInput"] input{background:#FFFFFF !important;border:1px solid #98A2B3 !important;border-radius:8px !important;font-size:13px !important;color:#17202A !important;}
div[data-testid="stNumberInput"] input:hover,div[data-testid="stTextInput"] input:hover{border-color:#66788A !important;}
div[data-testid="stNumberInput"] input:focus,div[data-testid="stTextInput"] input:focus{
  border-color:#475569 !important;box-shadow:0 0 0 3px #DCE3EA !important;outline:none !important;}
/* Selectbox COLLAPSED control (Search, Typology, Remove Keyword) — stable BaseWeb
   selectors; the visible box is div[data-baseweb="select"]>div. Padding/width/height
   left to BaseWeb (preserved). */
div[data-testid="stSelectbox"] div[data-baseweb="select"]>div{background:#FFFFFF !important;border:1px solid #98A2B3 !important;border-radius:8px !important;box-shadow:none !important;font-size:13px !important;font-weight:400 !important;color:#17202A !important;}
div[data-testid="stSelectbox"] input[role="combobox"]{color:#17202A !important;-webkit-text-fill-color:#17202A !important;font-size:13px !important;}
div[data-testid="stSelectbox"] input[role="combobox"]::placeholder{color:#667085 !important;-webkit-text-fill-color:#667085 !important;opacity:1 !important;}
div[data-testid="stSelectbox"] svg[data-baseweb="icon"]{fill:#475569 !important;}
div[data-testid="stSelectbox"] div[data-baseweb="select"]:hover>div{border-color:#66788A !important;cursor:pointer !important;}
div[data-testid="stSelectbox"] div[data-baseweb="select"]:focus-within>div{border:1px solid #475569 !important;box-shadow:0 0 0 3px #DCE3EA !important;outline:none !important;}
div[data-testid="stMultiSelect"] div[data-baseweb="select"]>div{font-size:14px !important;}
.stCheckbox label{font-size:13px !important;color:#1a1a1a !important;}
.stCheckbox label p{color:#1a1a1a !important;}
label[data-testid="stWidgetLabel"],
label[data-testid="stWidgetLabel"] *,
label[data-testid="stWidgetLabel"] p{
  color:#1a1a1a !important;
  -webkit-text-fill-color:#1a1a1a !important;
  font-weight:700 !important;
  font-size:13px !important;
  opacity:1 !important;
}
div[data-testid="stMultiSelect"] div[data-baseweb="select"]>div{background:#fff !important;color:#111 !important;border:1px solid #999 !important;}
/* (dropdown menu styling consolidated below — see "BaseWeb dropdown MENU") */
div[data-testid="stDataFrame"]{background:#fff !important;}
div[data-testid="stDataFrame"] [data-testid="stTable"]{background:#fff !important;}
.stButton>button{border-radius:4px !important;font-size:13px !important;font-weight:700 !important;letter-spacing:.02em !important;padding:8px 16px !important;border:1px solid !important;}
.stButton>button[kind="primary"]{background:linear-gradient(180deg,#526276 0%,#475569 100%) !important;color:#fff !important;border-color:#334155 !important;}
.stButton>button[kind="secondary"]{background:linear-gradient(to bottom,#f0f0f0,#e0e0e0) !important;color:#333 !important;border-color:#aaa !important;}

/* FIX 5: Switch button — scoped to the role-bar column, smaller and proportionate */
div[data-testid="stColumn"]:last-of-type .stButton>button{
  font-size:12px !important;
  padding:6px 12px !important;
  font-weight:600 !important;
  white-space:nowrap !important;
}

/* Alert Queue filter bar — one aligned panel, not scattered controls */
div[data-testid="stVerticalBlockBorderWrapper"]:has(.filter-bar-labels){
  padding:14px 16px 16px 16px !important;background:#F4F6F8;
  border:1px solid #D0D5DD !important;border-radius:11px !important;
  box-shadow:0 3px 10px rgba(17,24,39,0.06) !important;}
.filter-bar-labels{display:grid;grid-template-columns:2.1fr 1.6fr 2.3fr;
  gap:1rem;margin-bottom:6px;}
.filter-bar-labels span{font-size:11px;font-weight:800;color:#334155;letter-spacing:0.35px;
  letter-spacing:.05em;text-transform:uppercase;}
div[data-testid="stVerticalBlockBorderWrapper"]:has(.filter-bar-labels) div[data-testid="stSelectbox"]{
  margin-top:2px;}
/* Never let a chip row wrap to a second line — that's what broke alignment */
div[data-testid="stVerticalBlockBorderWrapper"]:has(.filter-bar-labels) div[data-testid="stButtonGroup"]{
  flex-wrap:nowrap !important;width:100%;}
div[data-testid="stVerticalBlockBorderWrapper"]:has(.filter-bar-labels) div[data-testid="stButtonGroup"] button{
  white-space:nowrap !important;flex:1 1 auto;}

/* Manager Review — pending override cards: static, NOT clickable (no whole-card
   hover / translate / border shift / pointer). Content and font sizes preserved. */
.ov-card{background:#ffffff;border:1px solid #D0D5DD;border-left:5px solid #C58B6C;
  border-radius:8px;padding:16px 18px;margin:0 0 16px 0;
  box-shadow:0 4px 12px rgba(23,52,83,.12);}
/* CORRECTION 1: whole-card hover removed — the card must NOT read as clickable.
   Interactivity lives only in the per-card "Open Case File" button. */
.ov-card-top{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:10px;}
.ov-card-id{font-size:15px;font-weight:700;color:#17202A;}
.ov-card-ts{font-size:13px;color:#5a6570;font-variant-numeric:tabular-nums;}
.ov-card-body{font-size:14px;color:#1a1a1a;margin-bottom:8px;line-height:1.5;}
.ov-card-meta{font-size:14px;color:#4a5560;margin-bottom:6px;}
.ov-card-reason{font-size:14px;color:#1a1a1a;line-height:1.5;}
/* Manager Review — Override History panel wrapper (keyed st.container). Boxes only
   this panel; Change Log and Audit Trail are unaffected. */
.st-key-override_history_panel{background:#ffffff;border:1px solid #D0D5DD;
  border-radius:8px;box-shadow:0 4px 12px rgba(23,52,83,.10);overflow:hidden;
  margin-top:18px;margin-bottom:16px;}
/* Manager Review — responsive action row (keyed horizontal st.container). Layout
   and sizing ONLY; button colors/gradients/borders/shadows/hover/active/focus are
   defined by the .st-key-oc_/apr_/rej_ rules and are left exactly unchanged. */
div[class*="st-key-ov_actions_"]{
  display:flex !important;align-items:center !important;justify-content:flex-start !important;
  gap:12px !important;flex-wrap:wrap !important;margin-top:10px !important;margin-bottom:14px !important;}
div[class*="st-key-ov_actions_"] .stButton>button{
  white-space:nowrap !important;min-height:38px !important;font-weight:600 !important;}
div[class*="st-key-ov_actions_"] div[class*="st-key-oc_"] .stButton>button{min-width:132px !important;}
div[class*="st-key-ov_actions_"] div[class*="st-key-apr_"] .stButton>button{min-width:112px !important;}
div[class*="st-key-ov_actions_"] div[class*="st-key-rej_"] .stButton>button{min-width:102px !important;}
@media (max-width:700px){
  div[class*="st-key-ov_actions_"]{gap:8px !important;flex-wrap:wrap !important;}
}
.ov-old{color:#8b0000;font-weight:700;}
.ov-new{color:#1a5c1a;font-weight:700;}

/* Approve / Reject button styling moved to the UI CLOSE-OUT PASS 1 block below (Task 2). */

div[data-testid="stButtonGroup"] button{font-size:13px !important;font-weight:700 !important;padding:7px 14px !important;}
/* Anchor spans exist only so the CSS below can target the *next* sibling —
   pull them out of flex flow entirely so they don't add a gap and push
   Severity/Status below Search/Sort (flex `gap` counts a 0-height item too). */
div[data-testid="stElementContainer"]:has(.sev-filter-anchor),
div[data-testid="stElementContainer"]:has(.sta-filter-anchor){
  position:absolute !important;width:0 !important;height:0 !important;
  margin:0 !important;padding:0 !important;overflow:hidden !important;}
div[data-testid="stElementContainer"]:has(.sev-filter-anchor)+div[data-testid="stElementContainer"] button:nth-of-type(1){color:#7b0000 !important;border-color:#c88 !important;}
div[data-testid="stElementContainer"]:has(.sev-filter-anchor)+div[data-testid="stElementContainer"] button:nth-of-type(1)[kind="segmented_controlActive"]{background:#fde8e8 !important;}
div[data-testid="stElementContainer"]:has(.sev-filter-anchor)+div[data-testid="stElementContainer"] button:nth-of-type(2){color:#6b3800 !important;border-color:#dba !important;}
div[data-testid="stElementContainer"]:has(.sev-filter-anchor)+div[data-testid="stElementContainer"] button:nth-of-type(2)[kind="segmented_controlActive"]{background:#fef3e2 !important;}
div[data-testid="stElementContainer"]:has(.sev-filter-anchor)+div[data-testid="stElementContainer"] button:nth-of-type(3){color:#1a5c1a !important;border-color:#9c9 !important;}
div[data-testid="stElementContainer"]:has(.sev-filter-anchor)+div[data-testid="stElementContainer"] button:nth-of-type(3)[kind="segmented_controlActive"]{background:#e8f5e8 !important;}
div[data-testid="stElementContainer"]:has(.sta-filter-anchor)+div[data-testid="stElementContainer"] button:nth-of-type(1){color:#1a2e8c !important;border-color:#99a !important;}
div[data-testid="stElementContainer"]:has(.sta-filter-anchor)+div[data-testid="stElementContainer"] button:nth-of-type(1)[kind="segmented_controlActive"]{background:#e8eeff !important;}
div[data-testid="stElementContainer"]:has(.sta-filter-anchor)+div[data-testid="stElementContainer"] button:nth-of-type(2){color:#1a5c1a !important;border-color:#9c9 !important;}
div[data-testid="stElementContainer"]:has(.sta-filter-anchor)+div[data-testid="stElementContainer"] button:nth-of-type(2)[kind="segmented_controlActive"]{background:#e8f5e8 !important;}
div[data-testid="stElementContainer"]:has(.sta-filter-anchor)+div[data-testid="stElementContainer"] button:nth-of-type(3){color:#555 !important;border-color:#bbb !important;}
div[data-testid="stElementContainer"]:has(.sta-filter-anchor)+div[data-testid="stElementContainer"] button:nth-of-type(3)[kind="segmented_controlActive"]{background:#f0f0f0 !important;}
</style>
""")

# ═════════════════════════════════════════════════════════════════════════════
# UI CLOSE-OUT PASS 1 — NEW styling only (one consolidated block, existing
# injection pattern). Every new color is a CSS custom property in :root, each set
# to a hex ALREADY present in app.py (no invented colors). Protected/existing
# rules above are untouched. Tasks: 2 Approve/Reject, 3 card hover, 4 tab bar,
# 5 draft radio, 6 checkboxes, 7 dropdown menu, 8 banner button.
# ═════════════════════════════════════════════════════════════════════════════
st.html("""
<style>
:root{
  /* Accent = brand teal (tabs, checkboxes, radio, dropdown) */
  --accent:#475569;          /* Obsidian Plum: primary plum (decorative accent) */
  --accent-strong:#334155;   /* Obsidian Plum: strong plum (decorative strong accent) */
  --accent-tint:#e8eeff;     /* existing: kw-chip bg / selected-option tint */
  --surface-hover:#eef3f5;   /* existing: table row hover */
  /* PASS / positive -> Approve */
  --pass:#1e8449;            /* existing: claim-card.pass */
  --pass-grad-a:#27865a;     /* existing: approve gradient top */
  --pass-strong:#176437;     /* existing: approve border / active */
  --pass-hover-a:#2f9c69;    /* existing: approve hover top */
  --pass-hover-b:#238c50;    /* existing: approve hover bottom */
  /* FAIL / danger -> Reject */
  --fail:#7b0000;            /* existing: v-fail text */
  --fail-strong:#a01818;     /* existing: reject text / metric danger */
  /* Neutral / secondary -> banner button */
  --neutral-a:#f0f0f0;       /* existing: secondary button top */
  --neutral-b:#e0e0e0;       /* existing: secondary button bottom */
  --neutral-text:#333;       /* existing: secondary button text */
  --neutral-border:#aaa;     /* existing: secondary button border */
}

/* Manager Review action buttons — Approve (apr_, green) and Reject (rej_, red).
   Exact spec colors; keyed .st-key-* hooks; st.button + callbacks unchanged. */
div[class*="st-key-apr_"] .stButton>button[kind="primary"]{
  background:linear-gradient(to bottom,#27865a,#1e8449) !important;
  color:#ffffff !important;border:1px solid #176437 !important;border-radius:6px !important;
  box-shadow:0 2px 4px rgba(23,52,83,.12) !important;
  transition:background .15s ease,border-color .15s ease,box-shadow .15s ease,transform .15s ease !important;}
div[class*="st-key-apr_"] .stButton>button[kind="primary"]:hover{
  background:linear-gradient(to bottom,#2f9c69,#238c50) !important;
  transform:translateY(-1px) !important;box-shadow:0 3px 7px rgba(23,52,83,.18) !important;}
div[class*="st-key-apr_"] .stButton>button[kind="primary"]:active{
  background:#176437 !important;transform:translateY(0) !important;
  box-shadow:inset 0 1px 3px rgba(0,0,0,.18) !important;}
div[class*="st-key-apr_"] .stButton>button[kind="primary"]:focus-visible{
  outline:none !important;box-shadow:0 0 0 3px #A8B6C8 !important;}
div[class*="st-key-rej_"] .stButton>button[kind="secondary"]{
  background:linear-gradient(to bottom,#a01818,#7b0000) !important;
  color:#ffffff !important;border:1px solid #7b0000 !important;border-radius:6px !important;
  box-shadow:0 2px 4px rgba(23,52,83,.12) !important;
  transition:background .15s ease,border-color .15s ease,box-shadow .15s ease,transform .15s ease !important;}
div[class*="st-key-rej_"] .stButton>button[kind="secondary"]:hover{
  background:linear-gradient(to bottom,#b03a2e,#a01818) !important;
  transform:translateY(-1px) !important;box-shadow:0 3px 7px rgba(23,52,83,.18) !important;}
div[class*="st-key-rej_"] .stButton>button[kind="secondary"]:active{
  background:#7b0000 !important;transform:translateY(0) !important;
  box-shadow:inset 0 1px 3px rgba(0,0,0,.18) !important;}
div[class*="st-key-rej_"] .stButton>button[kind="secondary"]:focus-visible{
  outline:none !important;box-shadow:0 0 0 3px #A8B6C8 !important;}

/* TASK 3 (pass 1) removed by CORRECTION 1: the pending-card hover created a false
   "whole card is clickable" affordance. Card interactivity now lives only in the
   per-card "Open Case File" button (Manager Review). */

/* TASK 4 — superseded by the Institutional Graphite navigation tray below; the old
   underline-accent tab rules are removed so they cannot fight the new tab design. */
div[data-baseweb="tab-highlight"]{background-color:var(--accent) !important;}
div[data-baseweb="tab-border"]{background-color:transparent !important;}

/* TASK 5 — Draft decision radio (accept/edit/reject) accent. NOTE: no st.radio
   exists in the Case File popup yet, so this is inert until one is added (adding
   the widget would be a logic change, out of scope here). */
[data-testid="stRadio"] [role="radiogroup"] label:hover{color:var(--accent-strong) !important;}
[data-testid="stRadio"] [data-baseweb="radio"] div:first-child{border-color:var(--accent) !important;}
[data-testid="stRadio"] input[type="radio"]:checked+div,
[data-testid="stRadio"] [data-baseweb="radio"][aria-checked="true"] div:first-child{
  background-color:var(--accent) !important;border-color:var(--accent) !important;}

/* TASK 6 — Risk Settings checkboxes use the accent when checked. All st.checkbox
   in the app are the four Risk Settings toggles. Best-effort BaseWeb targeting
   for Streamlit 1.58 (verify visually). */
[data-testid="stCheckbox"] [data-baseweb="checkbox"] span:first-child{
  border-color:var(--accent) !important;}
[data-testid="stCheckbox"] [data-baseweb="checkbox"] input:checked~span:first-child,
[data-testid="stCheckbox"] [data-baseweb="checkbox"][aria-checked="true"] span:first-child,
[data-testid="stCheckbox"] [data-baseweb="checkbox"] span[data-checked="true"]{
  background-color:var(--accent) !important;border-color:var(--accent) !important;}

/* TASK 7 — the dropdown menu renders in a PORTAL outside the widget. The verified
   build exposes NO ul[data-baseweb="menu"] and NO [role="listbox"]; the real menu
   is div[data-baseweb="popover"] ul with li[role="option"] items. All dropdown-menu
   styling is consolidated in the "BaseWeb dropdown MENU" block below. (The severity
   filter is a segmented_control — chips, protected, no dropdown.) */

/* MIDNIGHT LUMEN — role-switch button (→ Manager / → Analyst): the 3rd stColumn of
   the header control row ONLY. Widget key/callback unchanged (positional selector). */
div[data-testid="stHorizontalBlock"]:has(.role-bar) div[data-testid="stColumn"]:nth-child(3) .stButton>button{
  background:linear-gradient(180deg,#526276 0%,#475569 100%) !important;border:1px solid #334155 !important;
  color:#FFFFFF !important;
  min-height:44px !important;width:100% !important;padding:0 14px !important;margin:0 !important;
  border-radius:10px !important;box-shadow:0 5px 12px rgba(51,65,85,0.24) !important;
  font-size:12px !important;font-weight:800 !important;
  transition:background .12s ease,border-color .12s ease,box-shadow .12s ease,transform .12s ease !important;}
div[data-testid="stHorizontalBlock"]:has(.role-bar) div[data-testid="stColumn"]:nth-child(3) .stButton>button:hover{
  background:linear-gradient(180deg,#475569 0%,#3F4C5E 100%) !important;border-color:#2F3947 !important;
  color:#FFFFFF !important;
  box-shadow:0 7px 16px rgba(51,65,85,0.30) !important;transform:translateY(-1px) !important;}
div[data-testid="stHorizontalBlock"]:has(.role-bar) div[data-testid="stColumn"]:nth-child(3) .stButton>button:active{
  background:#2F3947 !important;border-color:#2F3947 !important;color:#FFFFFF !important;
  box-shadow:inset 0 2px 3px rgba(17,24,39,0.28) !important;transform:translateY(0) !important;}
div[data-testid="stHorizontalBlock"]:has(.role-bar) div[data-testid="stColumn"]:nth-child(3) .stButton>button:focus-visible{
  outline:none !important;box-shadow:0 0 0 3px #A8B6C8 !important;}
div[data-testid="stHorizontalBlock"]:has(.role-bar) div[data-testid="stColumn"]:nth-child(3) .stButton>button:disabled{
  background:#EEF1F4 !important;border-color:#D0D5DD !important;color:#667085 !important;
  box-shadow:none !important;transform:none !important;opacity:1 !important;}
</style>
""")

# ═════════════════════════════════════════════════════════════════════════════
# UI CORRECTIVE PASS — CSS for corrections 2, 3, 4, 5 (one consolidated block).
# No invented colors: every value is an existing app hex or a :root var above.
# Corrections 1 (card-hover removal + Open Case File) and 3's content are Python.
# ═════════════════════════════════════════════════════════════════════════════
st.html("""
<style>
:root{
  --warn:#eab308;        /* existing: role-bar border */
  --warn-bg:#fff8e1;     /* existing: warn-box background */
  --warn-text:#5d4000;   /* existing: warn-box text */
}

/* CORRECTION 4 — remove the top strip. The prior tag-qualified
   `header[data-testid="stHeader"]` rule did not match in Streamlit 1.58 (the
   header still rendered as a role=banner strip). Target the stable testids
   directly; do not touch app content. */
[data-testid="stHeader"]{display:none !important;}
[data-testid="stDecoration"]{display:none !important;}

/* CORRECTION 2 — Case File verification is the hero: AI CLAIM → SOURCE EVIDENCE →
   VERDICT. Existing PASS/FAIL/NEEDS-REVIEW colors kept; only hierarchy, spacing,
   and emphasis are strengthened (existing vars only). */
.hero-claim{border-left-width:6px !important;}
.hero-claim .claim-line{padding:10px 16px;align-items:center;}
.hero-claim .claim-tag{color:var(--accent-strong) !important;min-width:134px;}
.hero-claim .claim-val{font-size:14px !important;line-height:1.45;}
.hero-claim .claim-result-line{padding:11px 16px;}
.hero-verdict{font-size:15px !important;font-weight:800 !important;
  padding:5px 14px !important;border-radius:3px !important;letter-spacing:.03em;}

/* CORRECTION 3 — verification → human-outcome linkage banner (text computed in
   Python from existing values). */
.outcome-link{border-radius:6px;padding:12px 16px;margin-top:12px;font-size:14px;
  line-height:1.5;border:1px solid #D0D5DD;border-left:5px solid var(--accent);
  background:#fff;color:#1a1a1a;}
.outcome-link.ok{border-left-color:var(--pass);}
.outcome-link.warn{border-left-color:var(--warn);background:var(--warn-bg);color:var(--warn-text);}
.outcome-link b{color:var(--accent-strong);}
.outcome-link.warn b{color:#3d2b00;}

/* BaseWeb dropdown MENU (Search, Typology, Remove Keyword) — rendered in a PORTAL
   outside the widget. This build has NO ul[data-baseweb="menu"] and NO
   [role="listbox"]; the menu is div[data-baseweb="popover"] ul, options are
   li[role="option"], and each option's colored surface is its inner > div (which
   carries the 5px pill radius). One portal is shared by all three selectboxes.
   !important is required to beat BaseWeb's emotion rules. */
div[data-baseweb="popover"]{background:#ffffff !important;border:1px solid #98A2B3 !important;border-radius:8px !important;box-shadow:0 8px 24px rgba(17,24,39,.18) !important;padding:0 !important;}
div[data-baseweb="popover"] ul{background:#ffffff !important;width:100% !important;}
/* normal option — the li paints the white gutter; the inner > div is the pill */
div[data-baseweb="popover"] li[role="option"]{background:#ffffff !important;}
div[data-baseweb="popover"] li[role="option"]>div{background:transparent !important;color:#17202A !important;font-size:13px !important;font-weight:400 !important;padding-left:12px !important;padding-right:12px !important;border-radius:5px !important;}
/* hover / BaseWeb-highlighted */
div[data-baseweb="popover"] li[role="option"]:hover>div{background:#EEF1F4 !important;color:#334155 !important;font-weight:600 !important;}
/* keyboard focus (standard :focus/:focus-visible on the option) */
div[data-baseweb="popover"] li[role="option"]:focus>div,
div[data-baseweb="popover"] li[role="option"]:focus-visible>div{background:#EEF1F4 !important;color:#334155 !important;font-weight:600 !important;box-shadow:inset 0 0 0 2px #475569 !important;}
/* selected + selected-on-hover */
div[data-baseweb="popover"] li[role="option"][aria-selected="true"]>div{background:#CBD5E1 !important;color:#334155 !important;font-weight:700 !important;}
div[data-baseweb="popover"] li[role="option"][aria-selected="true"]:hover>div{background:#A8B6C8 !important;color:#334155 !important;font-weight:700 !important;}
/* disabled */
div[data-baseweb="popover"] li[role="option"][aria-disabled="true"]{cursor:not-allowed !important;opacity:.75 !important;}
div[data-baseweb="popover"] li[role="option"][aria-disabled="true"]>div{background:#F4F6F8 !important;color:#667085 !important;}
/* mobile: constrain WIDTH only (max-width cascades to the auto-width wrappers, ul,
   and 100%-width options); never touch the transform, which carries the dynamic
   vertical placement. 300px max-height + vertical scroll are left untouched. */
@media (max-width:600px){
  div[data-baseweb="popover"]{max-width:calc(100vw - 32px) !important;}
  div[data-baseweb="popover"] ul{width:100% !important;}
}

/* HERO CASE B — human-review gate panel in the Case File (display of the shared
   src/review_gate rule). Existing semantic colors/vars only; no new colors. */
.gate-panel{border-radius:6px;padding:12px 16px;margin-top:12px;font-size:14px;
  line-height:1.5;border:1px solid #D0D5DD;border-left:6px solid var(--accent);background:#fff;}
.gate-panel .gate-title{font-size:15px;font-weight:800;letter-spacing:.03em;margin-bottom:4px;}
.gate-panel .gate-body{color:#1a1a1a;}
.gate-panel .gate-missing{margin-top:6px;color:#1a1a1a;}
.gate-panel .gate-foot{margin-top:6px;font-size:12px;color:#5a6570;text-transform:uppercase;letter-spacing:.05em;}
.gate-blocked{border-left-color:var(--fail);background:#fdecec;}
.gate-blocked .gate-title{color:var(--fail);}
.gate-passed{border-left-color:var(--pass);background:#eef7ee;}
.gate-passed .gate-title{color:var(--pass);}
.gate-disabled{border-left-color:var(--warn);background:var(--warn-bg);}
.gate-disabled .gate-title,.gate-disabled .gate-body{color:var(--warn-text);}
/* Gate ALLOWED, shown as a neutral BLUE "review requirements complete" panel —
   deliberately NOT green (green over-signals correctness for mere completeness). */
.gate-complete{border-left:5px solid #2e728f;background:#e8f4f8;}
.gate-complete .gate-title,.gate-complete .gate-body{color:#1a5276;}
/* Case File outcome summary rail — three equal derived-status cards. Per-card
   background/border/text color is set inline from the derived value. */
.case-summary-rail{background:#F4F6F8;border:1px solid #D0D5DD;border-radius:8px;
  box-shadow:0 3px 10px rgba(23,52,83,.08);padding:10px;margin-bottom:12px;
  display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;}
.case-summary-card{border-radius:6px;padding:10px 12px;}
.case-summary-label{font-size:11px;text-transform:uppercase;font-weight:700;letter-spacing:.04em;}
.case-summary-value{font-size:15px;font-weight:800;margin-top:4px;}
@media (max-width:700px){.case-summary-rail{grid-template-columns:1fr;}}
</style>
""")

# ─────────────────────────────────────────────────────────────────────────────
# PASS A — coherent tab navigation + neutral "Open Case File" action + Case File
# empty states. CSS only; existing vars/colors and stable BaseWeb/keyed selectors
# only. No tab renaming/reordering, no permission or behavior change, no routing
# or callback change. Layered last, so it composes over the earlier tab/button CSS.
# ─────────────────────────────────────────────────────────────────────────────
st.html("""<style>
/* Five-tab bar as a NAVIGATION TRAY: a fit-content, visibly bounded soft
   blue-gray container holds five discrete white tabs. Every tab (active or not)
   renders as its own bordered tab — no floating text, no pipe separators. The
   active tab is a strong solid-teal selected state. Targets the actual BaseWeb
   tab-list (scoped under stTabs so it wins the cascade over the base tab CSS).
   Exact listed colors + stable BaseWeb selectors only; labels/order/behavior
   unchanged. */
/* the tray — fit-content so it reads as a bounded control, not a full-width band */
div[data-testid="stTabs"] div[data-baseweb="tab-list"]{
  display:inline-flex !important;width:fit-content !important;
  background:#FFFFFF !important;border:1px solid #D0D5DD !important;
  border-radius:12px !important;padding:6px !important;gap:6px !important;
  box-shadow:0 6px 18px rgba(17,24,39,0.12) !important;}
/* every tab, including inactive: its own bordered tab, equal height */
button[data-baseweb="tab"]{
  background:linear-gradient(180deg,#FFFFFF 0%,#F4F6F8 100%) !important;color:#334155 !important;
  border:1px solid #D0D5DD !important;border-radius:8px !important;
  min-height:42px !important;padding:0 16px !important;
  font-size:13px !important;font-weight:700 !important;
  box-shadow:0 2px 5px rgba(17,24,39,0.06) !important;
  transition:background .12s ease,border-color .12s ease,box-shadow .12s ease,color .12s ease,transform .08s ease !important;}
/* inactive hover: pale lavender surface, lifts 1px */
button[data-baseweb="tab"][aria-selected="false"]:hover{
  background:#EEF1F4 !important;border-color:#98A2B3 !important;color:#2B3139 !important;
  transform:translateY(-1px) !important;box-shadow:0 2px 4px rgba(17,24,39,0.10) !important;}
/* active tab: plum gradient, white text */
button[data-baseweb="tab"][aria-selected="true"]{
  background:linear-gradient(180deg,#526276 0%,#475569 100%) !important;color:#FFFFFF !important;
  border:1px solid #334155 !important;transform:none !important;
  box-shadow:0 4px 10px rgba(51,65,85,0.22) !important;}
/* active hover: stay plum with white text */
button[data-baseweb="tab"][aria-selected="true"]:hover{
  background:linear-gradient(180deg,#475569 0%,#3F4C5E 100%) !important;color:#FFFFFF !important;
  border-color:#2F3947 !important;transform:none !important;
  box-shadow:0 4px 10px rgba(51,65,85,0.26) !important;}
/* Tab LABEL colour: Streamlit paints the label paragraph with its own theme class,
   so the label is set explicitly at higher specificity (inactive slate, active white). */
div[data-testid="stTabs"] button[data-baseweb="tab"],
div[data-testid="stTabs"] button[data-baseweb="tab"] p{color:#334155 !important;}
div[data-testid="stTabs"] button[data-baseweb="tab"][aria-selected="false"]:hover,
div[data-testid="stTabs"] button[data-baseweb="tab"][aria-selected="false"]:hover p{color:#2B3139 !important;}
div[data-testid="stTabs"] button[data-baseweb="tab"][aria-selected="true"],
div[data-testid="stTabs"] button[data-baseweb="tab"][aria-selected="true"] p{color:#FFFFFF !important;}
/* strong keyboard focus */
button[data-baseweb="tab"]:focus-visible{
  outline:none !important;box-shadow:0 0 0 3px #A8B6C8 !important;border-radius:8px !important;}
/* no separate sliding underline — each tab's own border is the boundary */
div[data-baseweb="tab-highlight"]{background-color:transparent !important;height:0 !important;}
div[data-baseweb="tab-border"]{background-color:transparent !important;height:0 !important;}

/* "Open Case File" (Manager Review) — neutral outlined secondary action, exact
   spec colors; keyed .st-key-oc_* hook; st.button + routing/callbacks unchanged. */
div[class*="st-key-oc_"] .stButton>button[kind="secondary"]{
  background:#ffffff !important;color:#334155 !important;
  border:1px solid #98A2B3 !important;border-radius:6px !important;
  box-shadow:0 1px 2px rgba(17,24,39,.08) !important;
  transition:background .15s ease,border-color .15s ease,box-shadow .15s ease,transform .15s ease !important;}
div[class*="st-key-oc_"] .stButton>button[kind="secondary"]:hover{
  background:#EEF1F4 !important;color:#2B3139 !important;border-color:#475569 !important;
  box-shadow:0 2px 5px rgba(51,65,85,.16) !important;transform:translateY(-1px) !important;}
div[class*="st-key-oc_"] .stButton>button[kind="secondary"]:active{
  background:#CBD5E1 !important;transform:translateY(0) !important;
  box-shadow:inset 0 1px 2px rgba(23,52,83,.14) !important;}
div[class*="st-key-oc_"] .stButton>button[kind="secondary"]:focus-visible{
  outline:none !important;box-shadow:0 0 0 3px #A8B6C8 !important;}

/* Case File neutral empty states (alert with no human review on file). No verdict,
   no fabricated review — just parity with the panels that would otherwise appear. */
.gate-empty{border-left-color:#D0D5DD !important;}
.gate-empty .gate-title{color:#5a6570 !important;font-weight:700 !important;}
.case-empty{padding:14px 16px;color:#5a6570;font-size:14px;line-height:1.5;}
</style>""")

# ─────────────────────────────────────────────────────────────────────────────
# Case File — OVERRIDE REQUEST context panel. Additive, DISPLAY-ONLY governance
# record derived live from pending_overrides.csv (load_overrides) for the open
# alert. DISTINCT from a Human Review: it never creates, reads, or implies a
# HumanReview, and never alters the "No human review recorded" statement. Pending
# is yellow; an approved decision turns the panel green, a rejected one red.
# ─────────────────────────────────────────────────────────────────────────────
st.html("""<style>
.ovreq-panel{background:#fff8e1;border:1px solid #eab308;border-left:5px solid #eab308;
  border-radius:7px;padding:14px 16px;margin-bottom:14px;
  box-shadow:0 2px 8px rgba(23,52,83,.08);}
.ovreq-panel.approved{background:#eef8f1;border:1px solid #6fbd88;border-left:5px solid #6fbd88;}
.ovreq-panel.rejected{background:#fdeaea;border:1px solid #dc7c7c;border-left:5px solid #dc7c7c;}
.ovreq-head{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:10px;}
.ovreq-title{color:#5d4000;font-size:14px;font-weight:800;letter-spacing:.03em;}
.ovreq-panel.approved .ovreq-title{color:#176437;}
.ovreq-panel.rejected .ovreq-title{color:#7b0000;}
.ovreq-badge{background:#ffffff;color:#5d4000;border:1px solid #eab308;border-radius:999px;
  padding:4px 9px;font-size:11px;font-weight:800;white-space:nowrap;}
.ovreq-panel.approved .ovreq-badge{color:#176437;border-color:#6fbd88;}
.ovreq-panel.rejected .ovreq-badge{color:#7b0000;border-color:#dc7c7c;}
.ovreq-details{display:grid;grid-template-columns:minmax(130px,180px) 1fr;gap:7px 14px;}
.ovreq-lbl{color:#5d6570;font-size:11px;font-weight:700;text-transform:uppercase;}
.ovreq-val{color:#17202A;font-size:13px;font-weight:600;}
.ovreq-val.reason{line-height:1.45;}
@media (max-width:700px){
  .ovreq-head{flex-wrap:wrap;}
  .ovreq-details{grid-template-columns:1fr;gap:2px 0;}
  .ovreq-lbl{margin-top:9px;}
}
</style>""")

# ─────────────────────────────────────────────────────────────────────────────
# TOP BAR
# ─────────────────────────────────────────────────────────────────────────────
emp = st.session_state.current_user

ov_df         = load_overrides()
pending_count = len(ov_df[ov_df["status"] == "pending"]) if not ov_df.empty else 0
pending_pill  = (
    f'<span class="hdr-pending"><span class="dot"></span>'
    f'{pending_count} pending override{"s" if pending_count != 1 else ""}</span>'
    if pending_count else ""
)

st.markdown(f"""
<div class="id-bar">
  <div class="id-bar-logo" role="heading" aria-level="1">Lumen <span>Verify</span></div>
  <div class="id-bar-right">
    <span class="id-bar-user">{emp['name']} · {emp['rank']} · {emp['id']}</span>
  </div>
</div>
<div class="sub-nav">
  <span class="sub-nav-left">AML Decision Workbench &nbsp;·&nbsp; Analyst Queue</span>
  <span class="sub-nav-right">
    {pending_pill}
    <span>Session: {st.session_state.session_id}</span>
    <span>{datetime.now(timezone.utc).strftime('%d-%b-%Y %H:%M')} UTC</span>
  </span>
</div>
""", unsafe_allow_html=True)

# ── Demo-reset control (yellow banner button) + confirmation dialog CSS ──────────
st.html("""<style>
/* MIDNIGHT LUMEN — header control row (banner + two buttons). Scoped to the ONE
   stHorizontalBlock that contains .role-bar; no other horizontal block is touched. */
div[data-testid="stHorizontalBlock"]:has(.role-bar){
  background:#F4F6F8 !important;padding:14px 24px 16px !important;margin:0 0 6px 0 !important;
  gap:12px !important;border:none !important;border-radius:0 !important;box-shadow:none !important;
  align-items:center !important;}

/* Reset Demo button (2nd stColumn), keyed selector — widget key/callback unchanged. */
/* Reset Demo — restrained pastel-yellow utility control. Wording, icon, key,
   callback, dialog and baseline restoration are unchanged; colours only. */
div.st-key-demo_reset .stButton>button{
  background:linear-gradient(180deg,#FFF9D9 0%,#F8EDB8 100%) !important;border:1px solid #D8C779 !important;
  color:#584C1F !important;
  min-height:44px !important;width:100% !important;padding:0 14px !important;margin:0 !important;
  border-radius:10px !important;box-shadow:0 4px 10px rgba(88,76,31,0.12) !important;transform:none !important;
  font-size:12px !important;font-weight:750 !important;cursor:pointer !important;white-space:nowrap !important;
  transition:background .12s ease,border-color .12s ease,box-shadow .12s ease,transform .12s ease,color .12s ease !important;}
div.st-key-demo_reset .stButton>button:hover{
  background:#F5E6A3 !important;border-color:#C5B15D !important;color:#4D421B !important;
  box-shadow:0 6px 14px rgba(88,76,31,0.16) !important;transform:translateY(-1px) !important;}
div.st-key-demo_reset .stButton>button:active{
  background:#EED985 !important;border-color:#B49A3F !important;color:#453A14 !important;
  box-shadow:inset 0 2px 3px rgba(88,76,31,0.16) !important;transform:translateY(0) !important;}
div.st-key-demo_reset .stButton>button:focus-visible{
  outline:none !important;border-color:#B49A3F !important;box-shadow:0 0 0 3px #F2E6AE !important;}
div.st-key-demo_reset .stButton>button:disabled{
  background:#F3F0DF !important;border-color:#D8D1A9 !important;color:#938B68 !important;
  box-shadow:none !important;transform:none !important;opacity:1 !important;}

/* MIDNIGHT LUMEN — header-only responsive layout (≤700px). Nothing below the header
   control row is affected; no header metadata is hidden. */
@media (max-width:700px){
  .id-bar{flex-direction:column !important;align-items:flex-start !important;justify-content:flex-start !important;
    min-height:0 !important;padding:17px 18px 15px !important;gap:12px !important;box-shadow:0 5px 16px rgba(17,24,39,0.22) !important;}
  .id-bar-logo{font-size:31px !important;font-weight:850 !important;line-height:1 !important;letter-spacing:-0.7px !important;padding:0 !important;margin:0 !important;}
  .id-bar-user{font-size:12px !important;font-weight:750 !important;padding:7px 10px !important;border-radius:8px !important;white-space:nowrap !important;max-width:100% !important;}
  .sub-nav{flex-direction:column !important;align-items:flex-start !important;justify-content:flex-start !important;
    min-height:0 !important;height:auto !important;padding:12px 18px 14px !important;gap:9px !important;overflow:visible !important;}
  .sub-nav-left{font-size:12px !important;white-space:normal !important;width:100% !important;}
  .sub-nav-right{display:grid !important;grid-template-columns:1fr !important;align-items:start !important;
    justify-items:start !important;width:100% !important;gap:7px !important;font-size:11px !important;white-space:normal !important;}
  div[data-testid="stHorizontalBlock"]:has(.role-bar){
    display:grid !important;grid-template-columns:minmax(0,1fr) minmax(0,1fr) !important;
    padding:10px 16px 12px !important;gap:8px !important;margin-bottom:10px !important;align-items:stretch !important;}
  div[data-testid="stHorizontalBlock"]:has(.role-bar) > div[data-testid="stColumn"]:nth-child(1){
    grid-column:1 / -1 !important;width:100% !important;}
  div[data-testid="stHorizontalBlock"]:has(.role-bar) > div[data-testid="stColumn"]:nth-child(2),
  div[data-testid="stHorizontalBlock"]:has(.role-bar) > div[data-testid="stColumn"]:nth-child(3){
    width:100% !important;min-width:0 !important;flex:none !important;}
  .role-bar{min-height:0 !important;padding:10px 12px !important;font-size:12px !important;line-height:1.35 !important;border-radius:9px !important;}
  div.st-key-demo_reset .stButton>button,
  div[data-testid="stHorizontalBlock"]:has(.role-bar) div[data-testid="stColumn"]:nth-child(3) .stButton>button{
    width:100% !important;min-height:42px !important;padding:0 8px !important;font-size:11.5px !important;border-radius:8px !important;}
  div[data-testid="stTabs"] button[data-baseweb="tab"]{min-height:40px !important;font-size:12px !important;}
}
/* Reset confirmation dialog */
.drd-warn{background:#fff8e1;border:1px solid #eab308;border-left:5px solid #eab308;color:#5d4000;
  border-radius:6px;padding:14px 16px;margin-bottom:16px;font-size:14px;line-height:1.5;}
.st-key-demo_reset_dialog [data-testid="stHorizontalBlock"]{gap:12px !important;}
.st-key-demo_reset_cancel .stButton>button{
  background:#ffffff !important;color:#17202A !important;border:1px solid #98A2B3 !important;
  height:40px !important;min-height:40px !important;border-radius:6px !important;}
.st-key-demo_reset_go .stButton>button{
  background:linear-gradient(180deg,#a01818,#7b0000) !important;color:#ffffff !important;
  border:1px solid #650000 !important;height:40px !important;min-height:40px !important;
  border-radius:6px !important;font-weight:700 !important;}
</style>""")


@st.dialog("Reset demo data?")
def _demo_reset_dialog():
    with st.container(key="demo_reset_dialog"):
        st.markdown(
            '<div class="drd-warn">This restores the three override requests to Pending and '
            'removes manager decisions created during demo testing. Customer, alert, evidence, '
            'claim, human-review, and readiness data will not change.</div>',
            unsafe_allow_html=True,
        )
        _dc1, _dc2 = st.columns(2)
        with _dc1:
            if st.button("Cancel", key="demo_reset_cancel", width="stretch"):
                st.session_state.demo_reset_confirm = False
                st.rerun()
        with _dc2:
            if st.button("Reset Demo", key="demo_reset_go", type="primary", width="stretch"):
                # Restore ONLY the two runtime CSVs from the committed baseline snapshots.
                reset_override_demo(OVERRIDES_CSV, AUDIT_CSV, BASELINE_PENDING_CSV, BASELINE_AUDIT_CSV)
                # Clear the override rationale dialog/form + stale case-routing state.
                st.session_state.pending_override = None
                st.session_state.selected_alert = None
                st.session_state.open_case = None
                for _k in [k for k in list(st.session_state.keys()) if str(k).startswith("override_rationale_")]:
                    st.session_state.pop(_k, None)
                # Land back in Override Requests (the manager's role is left unchanged).
                # Use the deferred flag consumed BEFORE the segmented control renders:
                # the dialog coordinator now runs AFTER that widget instantiates, so
                # manager_review_view can no longer be set directly here.
                st.session_state._force_override_view = True
                st.session_state.demo_reset_confirm = False
                st.session_state.demo_reset_toast = True
                st.rerun()


# One success toast after a completed reset.
if st.session_state.pop("demo_reset_toast", False):
    st.toast("Demo reset complete: 3 override requests restored.")

rs_col1, rs_reset, rs_col2 = st.columns([7.6, 1.5, 1])
with rs_col1:
    st.markdown(
        f'<div class="role-bar">Demo mode — viewing as '
        f'<b>{st.session_state.view_as}</b>. Switch role to access manager '
        f'functions.</div>',
        unsafe_allow_html=True,
    )
with rs_reset:
    if st.button("↻ Reset Demo", key="demo_reset", use_container_width=True):
        # Open the confirmation dialog; reset happens only on confirm. Clear any
        # open Case File / override modal state first (single-dialog invariant).
        _open_demo_reset()
        st.rerun()
with rs_col2:
    if st.button(
        "→ Analyst" if st.session_state.view_as == "Manager" else "→ Manager",
        use_container_width=True,
    ):
        st.session_state.view_as = (
            "Analyst" if st.session_state.view_as == "Manager" else "Manager"
        )
        st.rerun()

# NOTE: all dialogs (Reset Demo, Approve/Reject, Case File) are invoked from the
# single mutually-exclusive coordinator at the end of this file — never here and
# never inside a tab — so at most one @st.dialog opens per rerun.

# ─────────────────────────────────────────────────────────────────────────────
# TABS
# ─────────────────────────────────────────────────────────────────────────────
tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "  Alert Queue  ",
    "  Manager Review  ",
    "  Risk Settings  ",
    "  Change Log  ",
    "  Audit Trail  ",
])

# ═════════════════════════════════════════════════════════════════════════════
# CASE FILE — modal popup
# ═════════════════════════════════════════════════════════════════════════════
def _override_panel_html(row: dict) -> str:
    """Render one OVERRIDE REQUEST panel for a Case File override record. DISPLAY
    ONLY: reflects the analyst's submitted request (and, once decided, the manager's
    recorded decision). This is an override request, NOT a Human Review."""
    def esc(v):
        s = "" if v is None else str(v)
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    status = (row.get("status") or "").lower()
    field  = row.get("field_changed", "")
    if status == "approved":
        cls, badge = "approved", "APPROVED"
    elif status == "rejected":
        cls, badge = "rejected", "REJECTED"
    else:
        cls, badge = "pending", "PENDING MANAGER DECISION"

    change = (esc(sev_label(field, row.get("old_value", ""))) + " → "
              + esc(sev_label(field, row.get("new_value", ""))))
    details = [
        ("FIELD", esc(field) or "—", ""),
        ("REQUESTED CHANGE", change, ""),
        ("SUBMITTED BY",
         f'{esc(row.get("changed_by_name", "—"))} ({esc(row.get("changed_by_id", "—"))})', ""),
        ("ANALYST REQUEST REASON", esc(row.get("reason", "—")) or "—", " reason"),
        ("SUBMITTED AT", esc(row.get("changed_at", "—")) or "—", ""),
    ]
    if cls in ("approved", "rejected"):
        details += [
            ("REVIEWED BY", esc(row.get("reviewed_by", "—")) or "—", ""),
            ("REVIEWED AT", esc(row.get("reviewed_at", "—")) or "—", ""),
            ("MANAGER DECISION RATIONALE", esc(row.get("review_note", "—")) or "—", " reason"),
        ]
    rows_html = "".join(
        f'<div class="ovreq-lbl">{lbl}</div><div class="ovreq-val{extra}">{val}</div>'
        for lbl, val, extra in details
    )
    return (
        f'<div class="ovreq-panel {cls}">'
        f'<div class="ovreq-head">'
        f'<span class="ovreq-title">OVERRIDE REQUEST</span>'
        f'<span class="ovreq-badge">{badge}</span>'
        f'</div>'
        f'<div class="ovreq-details">{rows_html}</div>'
        f'</div>'
    )


def override_context_html(alert_id: str) -> str:
    """Combined OVERRIDE REQUEST panel HTML for one alert, derived LIVE from the
    runtime pending_overrides.csv via load_overrides(). Empty string when the alert
    has no override request. Selection: the pending request first, then the most
    recently reviewed request — at most two panels. Read-only; it never mutates or
    filters Override History, and never touches the Human Review section."""
    df = load_overrides()
    if df.empty or "alert_id" not in df.columns:
        return ""
    recs = df[df["alert_id"] == alert_id]
    if recs.empty:
        return ""
    pending  = recs[recs["status"] == "pending"]
    reviewed = recs[recs["status"].isin(["approved", "rejected"])]
    if not reviewed.empty:
        reviewed = reviewed.sort_values("reviewed_at", ascending=False)
    chosen = []
    if not pending.empty:
        chosen.append(pending.iloc[0].to_dict())
    if not reviewed.empty:
        chosen.append(reviewed.iloc[0].to_dict())
    return "".join(_override_panel_html(r) for r in chosen[:2])


# ─────────────────────────────────────────────────────────────────────────────
# Case File scrollbar — keep the modal inside the viewport with an obvious,
# draggable vertical scrollbar. SCOPED to the Case File dialog only: it uniquely
# contains the keyed Close button (.st-key-close_case_dialog), so :has() targets it
# without touching the Approve/Reject or Reset Demo dialogs and without changing the
# global scrollbar styling. The dialog box becomes a flex column capped at
# calc(100vh - 32px); only the body region (the block that holds the stVerticalBlock)
# scrolls, so the title bar and the built-in close "X" stay pinned and visible.
# Scoped !important is required to beat Streamlit's global thin/transparent scrollbar.
# ─────────────────────────────────────────────────────────────────────────────
st.html("""<style>
/* Neutralize baseweb's 32px top padding on the modal centering wrapper so the
   capped dialog keeps a symmetric 16px gap (its own margin) above and below,
   instead of being pushed past the bottom edge. Scoped to the Case File only. */
div[data-testid="stDialog"]:has(.st-key-close_case_dialog) > div:has(> div[role="dialog"]){
  padding-top:0 !important;}
div[data-testid="stDialog"]:has(.st-key-close_case_dialog) div[role="dialog"]{
  max-height:calc(100vh - 32px) !important;
  display:flex !important;flex-direction:column !important;overflow:hidden !important;}
div[data-testid="stDialog"]:has(.st-key-close_case_dialog) div[role="dialog"] > div:has(> [data-testid="stVerticalBlock"]){
  flex:1 1 auto !important;min-height:0 !important;
  overflow-y:auto !important;overflow-x:hidden !important;
  scrollbar-gutter:stable !important;
  /* Revert Streamlit's global scrollbar-width:thin / scrollbar-color:transparent back to
     the initial values so Chromium renders the ::-webkit-scrollbar design below instead of
     the thin standard scrollbar. Firefox colors are restored in the @supports block. */
  scrollbar-width:auto !important;scrollbar-color:auto !important;}
/* WebKit/Blink — always-visible, draggable 12px scrollbar for the Case File body only.
   Deliberately NO standard scrollbar-width/-color here: setting either makes Chromium
   switch to the standard scrollbar renderer and ignore ::-webkit-scrollbar, dropping the
   12px width, 10px thumb radius, and 3px thumb border. */
div[data-testid="stDialog"]:has(.st-key-close_case_dialog) div[role="dialog"] > div:has(> [data-testid="stVerticalBlock"])::-webkit-scrollbar{
  width:12px !important;}
div[data-testid="stDialog"]:has(.st-key-close_case_dialog) div[role="dialog"] > div:has(> [data-testid="stVerticalBlock"])::-webkit-scrollbar-track{
  background:#eef3f5 !important;}
div[data-testid="stDialog"]:has(.st-key-close_case_dialog) div[role="dialog"] > div:has(> [data-testid="stVerticalBlock"])::-webkit-scrollbar-thumb{
  background:#8aaabe !important;border-radius:10px !important;border:3px solid #eef3f5 !important;}
div[data-testid="stDialog"]:has(.st-key-close_case_dialog) div[role="dialog"] > div:has(> [data-testid="stVerticalBlock"])::-webkit-scrollbar-thumb:hover{
  background:#475569 !important;}
/* Firefox has no ::-webkit-scrollbar — give it the standard properties instead. Chromium
   supports selector(::-webkit-scrollbar), so it skips this block and keeps the design above. */
@supports not selector(::-webkit-scrollbar){
  div[data-testid="stDialog"]:has(.st-key-close_case_dialog) div[role="dialog"] > div:has(> [data-testid="stVerticalBlock"]){
    scrollbar-width:auto !important;scrollbar-color:#8aaabe #eef3f5 !important;}
}
</style>""")


@st.dialog("Case File", width="large")
def show_case_dialog(alert_id: str, source: dict) -> None:
    case = get_case_detail(alert_id, source)
    c = case["customer"]
    a = case["alert"]

    st.markdown(f"""
    <div class="case-panel-hdr" style="border:1px solid #b0b0b0;">
      <span class="case-panel-title">{c.get('name', a['customer_id'])} — {a['rule_triggered']}</span>
      <span class="case-panel-id">{a['alert_id']} &nbsp;·&nbsp; {a['customer_id']}</span>
    </div>
    """, unsafe_allow_html=True)

    # ── Case outcome summary: three DERIVED status cards (AI Verification, Review
    #    Requirements, Recorded Disposition), directly below the identity header and
    #    above AI Claim Verification. DISPLAY ONLY — computed from existing case
    #    values; no stored value, verifier, gate, pipeline, or audit logic changes.
    def _summary_style(v):
        if v == "PASS":
            return "background:#eef7ee;border:1px solid #9c9;color:#1a5c1a;"
        if v in ("FAIL", "BLOCKED", "PROCESSING ERROR"):
            return "background:#fde8e8;border:1px solid #c88;color:#7b0000;"
        if v in ("MIXED", "NEEDS REVIEW", "PENDING"):
            return "background:#fff8e1;border:1px solid #e0b877;color:#7d4e00;"
        if v in ("NOT RECORDED", "NONE", "NOT EVALUATED", "NOT PROCESSED", "NOT REQUIRED"):
            return "background:#f7f8f9;border:1px solid #D0D5DD;color:#5a6570;"
        return "background:#e8f4f8;border:1px solid #8aaabf;color:#1a5276;"  # COMPLETE / allowed disposition

    # Every rail value comes from the canonical lifecycle record — never from the
    # verifier summary, the review gate, or alerts.status.
    lc = lifecycle_index[a["alert_id"]]
    _ai_verif = lifecycle_ai_verification_label(lc)
    _review_req = lifecycle_review_requirements_label(lc)
    _recorded_disp = lifecycle_recorded_disposition_label(lc)

    st.markdown(
        '<div class="case-summary-rail">'
        f'<div class="case-summary-card" style="{_summary_style(_ai_verif)}">'
        '<div class="case-summary-label">AI VERIFICATION</div>'
        f'<div class="case-summary-value">{_ai_verif}</div></div>'
        f'<div class="case-summary-card" style="{_summary_style(_review_req)}">'
        '<div class="case-summary-label">REVIEW REQUIREMENTS</div>'
        f'<div class="case-summary-value">{_review_req}</div></div>'
        f'<div class="case-summary-card" style="{_summary_style(_recorded_disp)}">'
        '<div class="case-summary-label">RECORDED DISPOSITION</div>'
        f'<div class="case-summary-value">{_recorded_disp}</div></div>'
        '</div>',
        unsafe_allow_html=True,
    )

    # ── OVERRIDE REQUEST context — directly below the Case Outcome Summary rail and
    #    above AI Claim Verification. Renders whenever this alert has at least one
    #    override request (regardless of entry source), derived LIVE from
    #    pending_overrides.csv. A governance record DISTINCT from a Human Review: it
    #    neither creates nor implies one, and never alters the Human Review section
    #    or the "No human review recorded" statement below.
    _ovreq_html = override_context_html(a["alert_id"])
    if _ovreq_html:
        st.markdown(_ovreq_html, unsafe_allow_html=True)

    def _claim_card(cl):
        cls = "pass" if cl["result"] == "PASS" else "fail" if cl["result"] == "FAIL" else "review"
        badge = f'v-{cls}'
        return (
            f'<div class="claim-card {cls} hero-claim">'
            f'<div class="claim-line"><span class="claim-tag">① Draft Claim</span>'
            f'<span class="claim-val">{cl["type"]} = {cl["asserted_value"]}</span></div>'
            f'<div class="claim-line"><span class="claim-tag">② Source Evidence</span>'
            f'<span class="claim-val">{cl["note"]}</span></div>'
            f'<div class="claim-result-line"><span class="claim-tag">③ Verdict</span>'
            f'<span class="{badge} hero-verdict">{cl["result"]}</span></div>'
            f'</div>'
        )

    # Panel body. A NOT_PROCESSED alert gets the explicit "no draft exists" explanation.
    # The internal draft provenance (ai_draft_source / processing_run_id / model_id) stays
    # on the lifecycle record and in the CSVs, but is NOT surfaced to analysts here: the
    # Case File shows only the draft claim, its source evidence, and the verdict.
    if lc.processing_status is ProcessingStatus.NOT_PROCESSED:
        _panel_body = ('<div class="case-empty">This synthetic inventory alert has not '
                       'been run through the LUMEN processing workflow. No AI draft or '
                       'verification result exists.</div>')
    else:
        _panel_body = "".join(_claim_card(cl) for cl in case["ai_claims"]) \
            or '<div class="field-desc-txt">No AI claims drafted for this alert.</div>'

    st.markdown(f"""
    <div class="case-panel" style="margin-top:12px;">
      <div class="case-panel-hdr"><span class="case-panel-title">AI Draft Verification</span></div>
      <div style="padding:14px 16px;background:#f5f5f5;">{_panel_body}</div>
    </div>
    """, unsafe_allow_html=True)

    # Re-run the deterministic verifier on the displayed claims and compare its summary
    # with the RECORDED lifecycle result. Display only — no lifecycle or audit mutation.
    if lc.processing_status is ProcessingStatus.PROCESSED:
        if derive_ai_verification(case["ai_claims"]) != _ai_verif:
            st.markdown(
                '<div class="warn-box">CURRENT VERIFICATION DIFFERS FROM THE RECORDED '
                'PROCESSING RESULT.</div>',
                unsafe_allow_html=True,
            )

    if case["missing"]:
        st.markdown(
            f'<div class="warn-box">Missing source evidence: {", ".join(case["missing"])}</div>',
            unsafe_allow_html=True,
        )

    st.markdown(f"""
    <div class="case-panel" style="margin-top:12px;">
      <div class="case-grid">
        <div class="case-section">
          <div class="case-section-title">Customer Profile</div>
          <div class="field-row"><span class="field-lbl">Country</span><span class="field-val">{c.get('country','—')}</span></div>
          <div class="field-row"><span class="field-lbl">Occupation</span><span class="field-val">{c.get('occupation','—')}</span></div>
          <div class="field-row"><span class="field-lbl">KYC Status</span><span class="field-val">{c.get('kyc_status','—')}</span></div>
          <div class="field-row"><span class="field-lbl">KYC Current (12mo)</span><span class="field-val">{case['kyc_current_within_12mo']}</span></div>
          <div class="field-row"><span class="field-lbl">Prior SAR Count</span><span class="field-val" style="color:{'#8b0000' if case['prior_sar'] > 0 else '#1a5c1a'};font-weight:700;">{case['prior_sar']}</span></div>
          <div class="field-row"><span class="field-lbl">Evidence Completeness</span><span class="field-val">{case['readiness']}%</span></div>
        </div>
        <div class="case-section">
          <div class="case-section-title">Transactions</div>
          {''.join(
              f'<div class="field-row"><span class="field-lbl">{t["txn_id"]} · {t["timestamp"]}</span>'
              f'<span class="field-val">{t["direction"]} {t["amount"]} ({t["counterparty_country"]})</span></div>'
              for t in case["transactions"]
          ) or '<div class="field-desc-txt">No transactions on file.</div>'}
        </div>
      </div>
    </div>
    """, unsafe_allow_html=True)

    # CORRECTION 3 — make the verification → human-outcome relationship explicit. The
    # review DECISION (accepted/edited/rejected) is kept distinct from the final case
    # action; a failed draft claim never becomes the recorded disposition. Display only.
    _fails = [cl for cl in case["ai_claims"] if cl["result"] == "FAIL"]
    if _fails:
        _n = len(_fails)
        _rv = case["review"]
        if lc.review_gate is ReviewGateStatus.COMPLETE and _rv:
            _cls, _msg = "ok", (
                f"<b>{_n} draft claim(s) failed verification.</b> The human reviewer "
                f"({_rv.get('reviewer', '—')}) recorded review decision "
                f"'{lc.human_review_decision.value}' and final case action "
                f"'{lc.final_action}'. The failed draft claim did not become the final "
                f"disposition."
            )
        elif lc.review_gate is ReviewGateStatus.BLOCKED and _rv:
            _cls, _msg = "warn", (
                f"<b>{_n} draft claim(s) failed verification.</b> The stored "
                f"human-review decision is '{lc.human_review_decision.value}', but the "
                f"review is incomplete and no final disposition is recorded."
            )
        else:
            _cls, _msg = "warn", (
                f"<b>{_n} draft claim(s) failed verification.</b> No completed human "
                f"review is on file — the unsupported claim must not be accepted without "
                f"human sign-off."
            )
        st.markdown(f'<div class="outcome-link {_cls}">{_msg}</div>', unsafe_allow_html=True)

    # ── Human Review + Human-Review Gate — state driven by the canonical lifecycle. ──
    def _render_hr_empty(hr_body, gate_title, gate_body):
        """Render the Human Review + Human-Review Gate panels in their neutral (no
        stored review) positions. Same panels/classes as a populated review — only the
        lifecycle-derived text differs. No fabricated review, verdict, or audit event."""
        st.markdown(f"""
        <div class="case-panel" style="margin-top:12px;">
          <div class="case-panel-hdr"><span class="case-panel-title">Human Review</span></div>
          <div class="case-empty">{hr_body}</div>
        </div>
        """, unsafe_allow_html=True)
        st.markdown(
            '<div class="gate-panel gate-empty">'
            f'<div class="gate-title">{gate_title}</div>'
            f'<div class="gate-body">{gate_body}</div>'
            '</div>',
            unsafe_allow_html=True,
        )

    if lc.processing_status is ProcessingStatus.NOT_PROCESSED:
        _render_hr_empty(
            "Not evaluated. This alert has not been processed or routed for human review.",
            "HUMAN-REVIEW GATE: NOT EVALUATED",
            "No review gate was evaluated because this alert has not been processed.",
        )
    elif lc.review_routing is ReviewRoutingStatus.NOT_REQUIRED:
        _gate_body = ("The routing policy authorized the recorded system disposition: "
                      f"{lc.final_action}.")
        if lc.override_status is OverrideStatus.PENDING:
            _gate_body += " A manager override request is pending above."
        _render_hr_empty(
            f"Human review was not required under deterministic routing policy "
            f"{lc.routing_policy_id}.",
            "HUMAN-REVIEW GATE: NOT APPLICABLE",
            _gate_body,
        )
    elif lc.review_gate is ReviewGateStatus.PENDING:
        _render_hr_empty(
            "Human review is required and awaiting submission.",
            "HUMAN-REVIEW GATE: PENDING",
            "No final disposition is accepted until a complete human review is submitted.",
        )
    else:
        # COMPLETE or BLOCKED — a human review is on file. Preserve the populated card
        # fields/compact layout and the anti-rubber-stamp gate exactly.
        rv = case["review"]
        st.markdown(f"""
        <div class="case-panel" style="margin-top:12px;">
          <div class="case-panel-hdr"><span class="case-panel-title">Human Review</span></div>
          <div style="padding:14px 16px;">
            <div class="review-meta">
              <div><span class="review-lbl">Reviewer</span><span class="review-val">{rv.get('reviewer','—')}</span></div>
              <div><span class="review-lbl">Review Decision</span><span class="review-val">{rv.get('draft_disposition','—')}</span></div>
              <div><span class="review-lbl">Evidence Reviewed</span><span class="review-val">{rv.get('evidence_reviewed','—')}</span></div>
            </div>
            <div class="review-field"><span class="review-lbl">Decision Rationale</span>
              <p class="review-text">{rv.get('decision_reason') or '(none recorded)'}</p></div>
            <div class="review-field"><span class="review-lbl">Final Note</span>
              <p class="review-text">{rv.get('final_note') or '(none recorded)'}</p></div>
          </div>
        </div>
        """, unsafe_allow_html=True)

        # HERO CASE B — anti-rubber-stamp human-review gate. PURE evaluation for
        # DISPLAY ONLY: this writes no audit event (audit belongs to an actual
        # pipeline execution). It applies the SAME shared rule the decision pipeline
        # uses (src/pipeline.py Step 3 -> src/review_gate.evaluate_review). This is a
        # stored, seeded review — no reviewer clicked Submit or Approve here.
        _enforce = bool(st.session_state.risk_settings.get("block_rubber_stamp", True))
        _gate = evaluate_review(rv, enforce=_enforce)
        _missing = ", ".join(_gate.missing) if _gate.missing else "none"
        if not _gate.enforcement_enabled:
            _would = (f" It would fail on: <b>{_missing}</b>." if _gate.missing
                      else " This review is complete and would pass.")
            _gate_html = (
                '<div class="gate-panel gate-disabled">'
                '<div class="gate-title">⚠ GOVERNANCE ENFORCEMENT DISABLED</div>'
                '<div class="gate-body">The anti-rubber-stamp gate is turned OFF in '
                f'Risk Settings, so this stored review is not being enforced.{_would}'
                '</div></div>'
            )
        elif _gate.blocked:
            _gate_html = (
                '<div class="gate-panel gate-blocked">'
                '<div class="gate-title">HUMAN-REVIEW GATE: BLOCKED</div>'
                '<div class="gate-body">Incomplete review detected. This stored review '
                'fails the same enforcement rule used by the decision pipeline. '
                '<b>No final disposition is accepted.</b></div>'
                f'<div class="gate-missing">Missing or invalid: <b>{_missing}</b></div>'
                '<div class="gate-foot">Enforcement: enabled</div></div>'
            )
        else:
            _gate_html = (
                '<div class="gate-panel gate-complete">'
                '<div class="gate-title">REVIEW REQUIREMENTS: COMPLETE</div>'
                '<div class="gate-body">All required review fields are present. '
                f'Review decision recorded: <b>{lc.human_review_decision.value}</b>. '
                f'Final case action: <b>{lc.final_action}</b>.</div>'
                '<div class="gate-foot">Enforcement: enabled</div></div>'
            )
        st.markdown(_gate_html, unsafe_allow_html=True)

    if st.button("Close", key="close_case_dialog", type="primary"):
        st.session_state.open_case = None
        st.session_state.selected_alert = None
        st.session_state.case_search = None
        st.query_params.pop("alert", None)
        st.rerun()


# ═════════════════════════════════════════════════════════════════════════════
# TAB 1 — ALERT QUEUE
# ═════════════════════════════════════════════════════════════════════════════
with tab1:
    st.markdown('<div class="page-body">', unsafe_allow_html=True)

    total = len(display_df)

    st.markdown(
        f'<h2 class="section-h">Alert Inventory <span class="section-count">({total})</span></h2>',
        unsafe_allow_html=True,
    )

    def badge_html(text, cls):
        return (
            f'<span style="display:inline-block;padding:2px 7px;font-size:11px;'
            f'font-weight:700;letter-spacing:.05em;text-transform:uppercase;'
            f'border-radius:2px;border:1px solid;{cls}">{text}</span>'
        )

    SEV_STYLE = {
        "High":   "background:#fde8e8;color:#7b0000;border-color:#c88;",
        "Medium": "background:#fef3e2;color:#6b3800;border-color:#dba;",
        "Low":    "background:#e8f5e8;color:#1a5c1a;border-color:#9c9;",
    }
    # Lifecycle-derived status badges. Each reuses an existing palette hex already used
    # elsewhere in this file (rail/severity/override styles) — no new colors introduced.
    STA_STYLE = {
        "Not Processed":    "background:#f7f8f9;color:#5a6570;border-color:#D0D5DD;",  # neutral
        "Processing Error": "background:#fde8e8;color:#7b0000;border-color:#c88;",     # red
        "Awaiting Manager": "background:#fff8e1;color:#7d4e00;border-color:#e0b877;",  # amber
        "Awaiting Review":  "background:#e8eeff;color:#1a2e8c;border-color:#99a;",     # blue
        "Blocked":          "background:#fde8e8;color:#7b0000;border-color:#c88;",     # red
        "Closed":           "background:#f0f0f0;color:#555;border-color:#bbb;",        # gray
    }

    pending_alert_ids = set()
    if not ov_df.empty:
        pending_alert_ids = set(ov_df[ov_df["status"] == "pending"]["alert_id"])

    sel = st.session_state.selected_alert

    all_alert_ids = display_df["alert_id"].tolist()

    with st.container(border=True):
        st.markdown('<div class="filter-bar-labels">'
                     '<span>Search</span><span>Severity</span>'
                     '<span>Status</span></div>',
                     unsafe_allow_html=True)
        fc0, fc1, fc2 = st.columns([2.1, 1.6, 2.3])
        with fc0:
            def _pick_case():
                v = st.session_state.case_search
                if v:
                    st.session_state.selected_alert = v
                    st.session_state.open_case = v

            _lbl = {r["alert_id"]: f'{r["alert_id"]} — {r["customer"]} · {r["severity"]}'
                    for _, r in display_df.iterrows()}
            st.selectbox(
                "Search or open a case", all_alert_ids, key="case_search",
                index=None, placeholder="Search by alert ID or customer name…",
                on_change=_pick_case, label_visibility="collapsed",
                format_func=lambda a: _lbl.get(a, a),
            )
        with fc1:
            st.markdown('<span class="sev-filter-anchor"></span>', unsafe_allow_html=True)
            severity_filter = st.segmented_control(
                "Filter by severity",
                ["High", "Medium", "Low"],
                selection_mode="multi",
                default=[],
                key="severity_filter",
                label_visibility="collapsed",
            )
        with fc2:
            st.markdown('<span class="sta-filter-anchor"></span>', unsafe_allow_html=True)
            # Lifecycle-derived status options (canonical order, only those present) —
            # never alerts.status.
            _status_order = ["Not Processed", "Processing Error", "Awaiting Manager",
                             "Awaiting Review", "Blocked", "Closed"]
            _present_statuses = set(display_df["status"])
            status_filter = st.segmented_control(
                "Filter by status",
                [s for s in _status_order if s in _present_statuses],
                selection_mode="multi",
                default=[],
                key="status_filter",
                label_visibility="collapsed",
            )

    if severity_filter:
        display_df = display_df[display_df["severity"].isin(severity_filter)]
    if status_filter:
        display_df = display_df[display_df["status"].isin(status_filter)]

    # TASK 1: Sort chips removed — severity ordering is automatic. Always sort
    # highest severity first; a stable sort keeps the prior (queue) order as the
    # tiebreaker within each severity band.
    SEV_RANK = {"High": 0, "Medium": 1, "Low": 2}
    display_df = display_df.sort_values(
        by="severity", key=lambda s: s.map(SEV_RANK), kind="stable"
    )

    rows_html = ""
    for i, (_, r) in enumerate(display_df.iterrows()):
        is_sel   = sel == r["alert_id"]
        has_pend = r["alert_id"] in pending_alert_ids
        row_style = (
            "background:#EEF1F4;box-shadow:inset 3px 0 0 #475569;" if is_sel else
            "box-shadow:inset 3px 0 0 #d9a441;" if has_pend else ""
        )

        v    = int(r["readiness"])
        rcol = "#8b0000" if v < 50 else "#7d4e00" if v < 75 else "#1a5c1a"
        rb_html = (
            f'<div style="display:flex;align-items:center;gap:6px;">'
            f'<div style="width:70px;height:8px;background:#ddd;border:1px solid #bbb;'
            f'border-radius:1px;overflow:hidden;">'
            f'<div style="width:{v}%;height:100%;background:{rcol};"></div></div>'
            f'<span style="font-size:12px;font-weight:700;color:#333;'
            f'min-width:30px;">{v}%</span></div>'
        )

        # DRAFT source indicator — reuses the EXACT existing colors: amber (#8a5600, the
        # PENDING indicator) for a synthetic fixture, green (#1a5c1a, the old "AI" marker)
        # for captured live AI, muted (#ccc) em dash for none. Text only — no badge/bg.
        _draft = r["draft"]
        if _draft == "FIXTURE":
            ai_html = '<span style="color:#8a5600;font-size:12px;font-weight:700;">&#9679; FIXTURE</span>'
        elif _draft == "LIVE AI":
            ai_html = '<span style="color:#1a5c1a;font-size:12px;font-weight:700;">&#9679; LIVE AI</span>'
        else:
            ai_html = '<span style="color:#ccc;font-size:12px;">&#8212;</span>'

        sev_html = badge_html(r["severity"], SEV_STYLE.get(r["severity"], ""))
        sta_html = badge_html(r["status"], STA_STYLE.get(r["status"], ""))
        ana_html = f'<span style="color:#555;font-size:13px;">{r["analyst"]}</span>'
        if has_pend:
            ana_html += ('&nbsp;<span style="font-size:11px;font-weight:700;color:#8a5600;'
                         'background:#fdf0d5;border:1px solid #e0b877;border-radius:3px;'
                         'padding:1px 5px;">PENDING</span>')

        aid = r["alert_id"]

        def _cell(content, extra="", td_style="", interactive=False, aria=""):
            a_attrs = f'aria-label="{aria}"' if interactive else 'tabindex="-1" aria-hidden="true"'
            return (
                f'<td style="{td_style}"><a href="?alert={aid}" target="_self" {a_attrs} '
                f'style="display:block;padding:9px 14px;text-decoration:none;'
                f'color:inherit;{extra}">{content}</a></td>'
            )

        sev_bg = {"High": "background:#fdecec;", "Medium": "background:#fdf4e3;",
                  "Low": "background:#eef7ee;"}.get(r["severity"], "")

        rows_html += (
            f'<tr style="{row_style}">'
            + _cell(aid, "font-weight:800;color:#3F4C5E;font-size:13px;")
            + _cell(r["customer"], "font-weight:600;font-size:13px;")
            + _cell(r["rule"], "font-size:13px;")
            + _cell(sev_html, td_style=sev_bg)
            + _cell(rb_html)
            + _cell(ai_html)
            + _cell(sta_html)
            + _cell(ana_html)
            + _cell('<span class="lv-open">Open ›</span>', interactive=True,
                    aria=f'Open case {aid} — {r["customer"]}')
            + '</tr>'
        )

    table_html = f"""
<style>
  .lv-table {{ width:100%; border-collapse:collapse; font-size:13.5px; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif; }}
  .lv-table thead tr {{ background:linear-gradient(180deg,#374151 0%,#2B3139 100%); }}
  .lv-table th {{ padding:11px 14px; text-align:left; font-size:12px; font-weight:850;
       color:#FFFFFF; border-right:1px solid #46515F; letter-spacing:.2px;
       min-height:42px; box-shadow:inset 0 -1px 0 rgba(255,255,255,0.05);
       border-bottom:none; white-space:nowrap; }}
  .lv-table th:last-child {{ border-right:none; }}
  .lv-table td {{ padding:0; border-bottom:1px solid #D0D5DD;
       border-right:1px solid #D0D5DD; color:#17202A; vertical-align:middle; background:#FFFFFF;
       font-size:12.5px; font-weight:500; }}
  .lv-table td:last-child {{ border-right:none; }}
  .lv-table td a {{ color:inherit; }}
  .lv-table td a:focus-visible {{ outline:none; box-shadow:0 0 0 3px #A8B6C8; }}
  .lv-table tbody tr:nth-child(even) td {{ background:#F4F6F8; }}
  .lv-table tbody tr:nth-child(odd) td {{ background:#FFFFFF; }}
  .lv-table tbody tr:hover td {{ background:#EEF1F4 !important; cursor:pointer; }}
  .lv-open {{ color:#3F4C5E; font-weight:800; font-size:13px; white-space:nowrap; }}
  .lv-table tbody tr:hover .lv-open {{ text-decoration:underline; }}
</style>
<table class="lv-table">
  <caption style="position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);">Alert queue — click a row to open its case file</caption>
  <thead><tr>
    <th scope="col">Alert ID</th><th scope="col">Customer</th><th scope="col">Rule Triggered</th>
    <th scope="col">Severity</th><th scope="col">EVIDENCE COMPLETENESS</th><th scope="col">DRAFT</th>
    <th scope="col">Status</th><th scope="col">Analyst</th><th scope="col">Action</th>
  </tr></thead>
  <tbody>{rows_html}</tbody>
</table>"""

    st.markdown(table_html, unsafe_allow_html=True)

    # Case File is opened by the single dialog coordinator at the end of the file,
    # not here, so it can never coincide with the override or reset dialog.

    st.markdown('</div>', unsafe_allow_html=True)

# ═════════════════════════════════════════════════════════════════════════════
# TAB 2 — MANAGER REVIEW
# ═════════════════════════════════════════════════════════════════════════════
with tab2:
    st.markdown('<div class="page-body">', unsafe_allow_html=True)

    if st.session_state.view_as != "Manager":
        st.markdown("""
        <div class="warn-box" style="margin:0 0 14px 0">
          Manager Review is only accessible in Manager view.
          Use the <b>→ Manager</b> button at the top of the page.
        </div>""", unsafe_allow_html=True)
    else:
        # After an override decision, keep Manager Review on the override subview.
        # Set BEFORE the segmented control instantiates so no widget-state error occurs.
        if st.session_state.pop("_force_override_view", False):
            st.session_state["manager_review_view"] = "override_requests"
        # ══ HUMAN REVIEW OVERSIGHT ══════════════════════════════════════════
        # Independent, DERIVED, display-only view of the stored human reviews,
        # separated from pending field overrides. Reads existing review/alert/
        # customer/claim/gate data; hardcodes nothing. Completeness always uses the
        # shared gate with enforce=True (even if the Risk Settings toggle is off).
        # Pure: evaluate_review / get_case_detail write NO audit event.
        st.html("""<style>
.st-key-human_review_oversight{background:#ffffff;border:1px solid #D0D5DD;border-radius:10px;
  padding:16px;margin-bottom:22px;box-shadow:0 4px 14px rgba(23,52,83,.10);}
.hro-title{font-size:18px;font-weight:800;color:#17202A;margin-bottom:4px;}
.hro-subtitle{font-size:13px;color:#5d6573;margin-bottom:14px;}
.hro-rail{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;background:#F4F6F8;
  border:1px solid #D0D5DD;border-radius:8px;padding:10px;margin-bottom:18px;}
.hro-metric{padding:10px 12px;border-radius:6px;border:1px solid;}
.hro-metric-lbl{font-size:11px;text-transform:uppercase;font-weight:700;letter-spacing:.04em;}
.hro-metric-val{font-size:22px;font-weight:800;margin-top:3px;}
.hro-metric-attention{background:#fff1f1;border-color:#e9a0a0;}
.hro-metric-attention .hro-metric-lbl,.hro-metric-attention .hro-metric-val{color:#a00000;}
.hro-metric-complete{background:#e8f4f8;border-color:#9fc6d8;}
.hro-metric-complete .hro-metric-lbl,.hro-metric-complete .hro-metric-val{color:#1a5276;}
.hro-metric-pending{background:#fff8e1;border-color:#eab308;}
.hro-metric-pending .hro-metric-lbl,.hro-metric-pending .hro-metric-val{color:#8a5a00;}
.hro-warn{background:#fff8e1;border:1px solid #eab308;color:#8a5a00;border-radius:6px;
  padding:9px 12px;font-size:13px;font-weight:600;margin-bottom:14px;}
.hro-subhead{font-size:13px;font-weight:800;text-transform:uppercase;letter-spacing:.04em;
  color:#17202A;margin:16px 0 8px;}
.hro-empty{font-size:13px;color:#5d6573;padding:6px 2px;}
.hro-metric-total{background:#F4F6F8;border-color:#D0D5DD;}
.hro-metric-total .hro-metric-lbl,.hro-metric-total .hro-metric-val{color:#17202A;}
/* Requires Attention card — the keyed container OWNS the card (bg/border/left-border/
   radius/padding/shadow); the Open Case File button renders INSIDE it, not detached. */
div[class*="st-key-hro_att_"]{background:#fff1f1;border:1px solid #e9a0a0;border-left:5px solid #a00000;
  border-radius:8px;padding:14px 16px;margin-bottom:10px;box-shadow:0 3px 10px rgba(23,52,83,.08);}
/* Completed Reviews — two equal columns of CONTENT-HEIGHT cards. The previous flex
   layout with align-items:stretch stretched each card down the whole row; a CSS grid
   with grid-auto-rows:max-content + align-self:start sizes every card to its content
   only, so the card ends 16px (its padding) below the Open Case File button. */
div[class*="st-key-completed_review_grid"]{
  display:grid !important;grid-template-columns:1fr 1fr !important;gap:14px !important;
  align-items:start !important;grid-auto-rows:max-content !important;}
div[class*="st-key-hro_done_"]{
  background:#ffffff !important;border:1px solid #D0D5DD !important;border-left:4px solid #475569 !important;
  border-radius:8px !important;padding:16px !important;box-shadow:0 3px 10px rgba(23,52,83,.08) !important;
  height:auto !important;min-height:0 !important;align-self:start !important;}
/* Open Case File button sits INSIDE the card, 12px below the content (block gap). */
div[class*="st-key-hro_att_"] [data-testid="stVerticalBlock"],
div[class*="st-key-hro_done_"] [data-testid="stVerticalBlock"]{gap:12px !important;}
.hro-card-head{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;gap:8px;margin-bottom:8px;}
.hro-card-title{font-size:16px;font-weight:800;color:#17202A;}
.hro-card-sub{font-size:12px;color:#5d6573;}
.hro-badges{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px;}
.hro-badge{text-transform:uppercase;font-size:11px;font-weight:800;padding:4px 8px;border-radius:999px;border:1px solid;white-space:nowrap;}
.badge-red{background:#fff1f1;border-color:#e9a0a0;color:#a00000;}
.badge-green{background:#edf8f0;border-color:#9ed3aa;color:#176437;}
.badge-amber{background:#fff8e1;border-color:#eab308;color:#8a5a00;}
.badge-blue{background:#e8f4f8;border-color:#9fc6d8;color:#1a5276;}
.badge-neutral{background:#F4F6F8;border-color:#D0D5DD;color:#5d6573;}
.hro-details{display:flex;flex-wrap:wrap;gap:16px;}
.hro-field{display:flex;flex-direction:column;min-width:0;}
.hro-lbl{font-size:11px;text-transform:uppercase;font-weight:700;color:#5d6573;}
.hro-val{font-size:13px;font-weight:600;color:#17202A;overflow-wrap:anywhere;}
div[class*="st-key-hro_"] .stButton>button{
  background:#ffffff !important;color:#334155 !important;border:1px solid #98A2B3 !important;
  border-radius:6px !important;height:38px !important;min-height:38px !important;padding:0 16px !important;
  font-weight:600 !important;white-space:nowrap !important;width:auto !important;
  transition:background .15s ease,border-color .15s ease,box-shadow .15s ease,transform .15s ease !important;}
div[class*="st-key-hro_"] .stButton>button:hover{
  background:#EEF1F4 !important;border-color:#475569 !important;transform:translateY(-1px) !important;}
div[class*="st-key-hro_"] .stButton>button:focus-visible{
  outline:none !important;box-shadow:0 0 0 3px #A8B6C8 !important;}
/* Secondary nav — Manager Review view segmented control. Scoped under the keyed
   wrapper so the Alert Queue severity/status controls are untouched. */
.st-key-manager_review_view div[data-testid="stButtonGroup"]{
  display:inline-flex !important;width:fit-content !important;background:#FFFFFF !important;
  border:1px solid #D0D5DD !important;border-radius:10px !important;padding:5px !important;gap:5px !important;
  margin-bottom:18px !important;box-shadow:0 2px 6px rgba(23,52,83,.12) !important;}
.st-key-manager_review_view button[kind="segmented_control"]{
  background:#ffffff !important;border:1px solid #D0D5DD !important;color:#17202A !important;
  border-radius:6px !important;height:40px !important;padding:0 18px !important;font-size:13px !important;font-weight:700 !important;
  transition:background .15s ease,border-color .15s ease,box-shadow .15s ease,transform .15s ease !important;}
.st-key-manager_review_view button[kind="segmented_control"] p{color:#17202A !important;}
.st-key-manager_review_view button[kind="segmented_control"]:hover{
  background:#EEF1F4 !important;border-color:#98A2B3 !important;color:#2B3139 !important;
  transform:translateY(-1px) !important;box-shadow:0 2px 5px rgba(17,24,39,.12) !important;}
.st-key-manager_review_view button[kind="segmented_controlActive"]{
  background:linear-gradient(180deg,#526276 0%,#475569 100%) !important;border:1px solid #334155 !important;color:#ffffff !important;
  border-radius:6px !important;height:40px !important;padding:0 18px !important;font-size:13px !important;font-weight:700 !important;
  box-shadow:0 2px 6px rgba(23,52,83,.18) !important;}
.st-key-manager_review_view button[kind="segmented_controlActive"] p{color:#ffffff !important;}
.st-key-manager_review_view button[kind="segmented_controlActive"]:hover{
  background:#2F3947 !important;border-color:#2F3947 !important;color:#ffffff !important;}
.st-key-manager_review_view button[kind="segmented_control"]:focus-visible,
.st-key-manager_review_view button[kind="segmented_controlActive"]:focus-visible{
  outline:none !important;box-shadow:0 0 0 3px #A8B6C8 !important;}
@media (max-width:700px){.hro-rail{grid-template-columns:1fr;}.hro-details{flex-direction:column;gap:8px;}
  div[class*="st-key-completed_review_grid"]{grid-template-columns:1fr !important;gap:12px !important;}
  div[class*="st-key-hro_done_"]{min-width:0 !important;}}
</style>""")

        # Derive: partition the stored reviews by the shared gate (enforce=True).
        _hr_rows = source["human_reviews"]
        _attention, _completed = [], []
        for _, _hrow in _hr_rows.iterrows():
            _rv = _hrow.to_dict()
            _gate = evaluate_review(_rv, enforce=True)
            (_attention if _gate.blocked else _completed).append((_rv, _gate))
        _attention.sort(key=lambda x: x[0]["alert_id"])
        _completed.sort(key=lambda x: x[0]["alert_id"])
        _review_total = len(_attention) + len(_completed)
        _ov_now = load_overrides()
        _pending_ct = int((_ov_now["status"] == "pending").sum()) if not _ov_now.empty else 0
        _mr_valid = alerts_df["alert_id"].tolist()

        def _hro_badge(dim, value, blocked_ctx):
            v = str(value).upper()
            if v == "PASS":
                cls = "badge-green"
            elif v in ("FAIL", "BLOCKED"):
                cls = "badge-red"
            elif v in ("MIXED", "NEEDS REVIEW"):
                cls = "badge-amber"
            elif v == "NONE":
                cls = "badge-red" if blocked_ctx else "badge-neutral"
            elif v == "NOT EVALUATED":
                cls = "badge-neutral"
            else:                                     # COMPLETE / EDITED / allowed disposition
                cls = "badge-blue"
            return f'<span class="hro-badge {cls}">{dim}: {v}</span>'

        def _hro_render_card(rv, gate, kind):
            alert_id = rv.get("alert_id", "")
            valid = alert_id in _mr_valid
            # Classification, missing-field detection, and enforcement stay driven by the
            # real review gate (enforce=True) — see line above and the attention branch.
            blocked = gate.blocked
            if valid:
                _case = get_case_detail(alert_id, source)
                cust = _case["customer"].get("name", alert_id)
                # All three oversight badges are DERIVED FROM THE CANONICAL LIFECYCLE —
                # never from re-verification or the HumanReview draft_disposition. In
                # particular RECORDED DISPOSITION is lifecycle.final_action.
                _lc = lifecycle_index[alert_id]
                ai = lifecycle_ai_verification_label(_lc)
                review_req = lifecycle_review_requirements_label(_lc)
                disp = lifecycle_recorded_disposition_label(_lc)
            else:
                # Dangling review (alert_id not in alerts.csv): no lifecycle record.
                cust, ai, review_req, disp = alert_id, "NOT EVALUATED", \
                    ("BLOCKED" if blocked else "COMPLETE"), "NONE"
            badges = (
                _hro_badge("AI VERIFICATION", ai, blocked)
                + _hro_badge("REVIEW REQUIREMENTS", review_req, blocked)
                + _hro_badge("RECORDED DISPOSITION", disp, blocked)
            )
            if kind == "attention":
                _extra = (
                    '<div class="hro-field"><span class="hro-lbl">Missing Requirements</span>'
                    f'<span class="hro-val">{", ".join(gate.missing) if gate.missing else "none"}</span></div>'
                )
                _card_key = f"hro_att_{alert_id}"
            else:
                _extra = (
                    '<div class="hro-field"><span class="hro-lbl">Final Action</span>'
                    f'<span class="hro-val">{rv.get("final_action") or "—"}</span></div>'
                )
                _card_key = f"hro_done_{alert_id}"
            # The keyed container OWNS the visual card; content + Open Case File button
            # render INSIDE it (no detached button, no separate visual card).
            with st.container(key=_card_key):
                st.markdown(
                    f'<div class="hro-card-head"><span class="hro-card-title">{alert_id} · {cust}</span>'
                    f'<span class="hro-card-sub">Review ID: {rv.get("review_id","—")}</span></div>'
                    f'<div class="hro-badges">{badges}</div>'
                    '<div class="hro-details">'
                    '<div class="hro-field"><span class="hro-lbl">Reviewer</span>'
                    f'<span class="hro-val">{rv.get("reviewer","—")}</span></div>'
                    f'{_extra}</div>',
                    unsafe_allow_html=True,
                )
                if valid and st.button("Open Case File", key=f"hro_{alert_id}", width="content"):
                    # Open THIS review's Case File. _open_case_file clears any stale
                    # pending-override / reset routing so oversight never inherits it.
                    st.session_state.selected_alert = alert_id
                    _open_case_file(alert_id)
                    st.rerun()

        # ── Secondary navigation: one local segmented control separating the two
        #    Manager Review workflows. Internal option values are stable; labels carry
        #    the dynamic counts via format_func. Switching reruns (no audit write).
        _mr_view = st.segmented_control(
            "Manager Review view",
            options=["human_reviews", "override_requests"],
            format_func=lambda v: {
                "human_reviews": f"Human Reviews ({_review_total})",
                "override_requests": f"Override Requests ({_pending_ct})",
            }[v],
            selection_mode="single",
            default="human_reviews",
            key="manager_review_view",
            label_visibility="collapsed",
        )
        if _mr_view is None:                        # single-select guard: always show a view
            _mr_view = "human_reviews"

        if _mr_view == "human_reviews":
            with st.container(key="human_review_oversight"):
                st.markdown(
                    '<div class="hro-title">Human Review Oversight</div>'
                    '<div class="hro-subtitle">Independent visibility into incomplete and completed human reviews.</div>'
                    '<div class="hro-rail">'
                    '<div class="hro-metric hro-metric-attention"><div class="hro-metric-lbl">Requires Attention</div>'
                    f'<div class="hro-metric-val">{len(_attention)}</div></div>'
                    '<div class="hro-metric hro-metric-complete"><div class="hro-metric-lbl">Completed Reviews</div>'
                    f'<div class="hro-metric-val">{len(_completed)}</div></div>'
                    '<div class="hro-metric hro-metric-total"><div class="hro-metric-lbl">Total Human Reviews</div>'
                    f'<div class="hro-metric-val">{_review_total}</div></div>'
                    '</div>',
                    unsafe_allow_html=True,
                )
                if not st.session_state.risk_settings.get("block_rubber_stamp", True):
                    st.markdown(
                        '<div class="hro-warn">Governance enforcement is currently disabled, '
                        'but incomplete reviews remain visible for oversight.</div>',
                        unsafe_allow_html=True,
                    )
                st.markdown('<div class="hro-subhead">Requires Attention</div>', unsafe_allow_html=True)
                if _attention:
                    for _rv, _gate in _attention:
                        _hro_render_card(_rv, _gate, "attention")
                else:
                    st.markdown('<div class="hro-empty">No incomplete reviews on file.</div>', unsafe_allow_html=True)
                st.markdown('<div class="hro-subhead">Completed Reviews</div>', unsafe_allow_html=True)
                if _completed:
                    with st.container(key="completed_review_grid", horizontal=True,
                                      horizontal_alignment="left", vertical_alignment="top", gap="medium"):
                        for _rv, _gate in _completed:
                            _hro_render_card(_rv, _gate, "complete")
                else:
                    st.markdown('<div class="hro-empty">No completed reviews on file.</div>', unsafe_allow_html=True)

        else:
            # Manager override decision dialog CSS (the modal renders in a portal, so
            # this is global). st.html injects CSS with no layout container.
            st.html("""<style>
div[data-testid="stDialog"] div[role="dialog"]{
  background:#ffffff !important;border:1px solid #D0D5DD !important;border-radius:10px !important;
  box-shadow:0 10px 30px rgba(23,52,83,.18) !important;}
.ovd-summary{background:#F4F6F8;border:1px solid #D0D5DD;border-radius:7px;padding:14px 16px;margin-bottom:14px;}
.ovd-row{display:flex;gap:12px;margin-bottom:7px;font-size:13px;align-items:baseline;}
.ovd-row:last-child{margin-bottom:0;}
.ovd-lbl{flex:0 0 150px;font-size:11px;font-weight:700;color:#5d6573;text-transform:uppercase;letter-spacing:.03em;}
.ovd-val{color:#17202A;font-weight:600;overflow-wrap:anywhere;}
.ovd-help{font-size:12px;color:#5d6570;text-align:right;margin-top:4px;margin-bottom:10px;}
.st-key-override_decision_dialog textarea{border:1px solid #98A2B3 !important;border-radius:6px !important;}
.st-key-override_decision_dialog textarea:focus{
  border-color:#475569 !important;box-shadow:0 0 0 3px #DCE3EA !important;}
/* Cancel + Confirm are form submit buttons — always enabled (no disabled=), always
   clickable. Approve green / Reject red keyed by the decision suffix in the key. */
div[class*="st-key-override_submit_cancel_"] button{
  background:#ffffff !important;color:#17202A !important;border:1px solid #98A2B3 !important;
  border-radius:6px !important;height:40px !important;min-height:40px !important;font-weight:600 !important;
  cursor:pointer !important;opacity:1 !important;}
div[class*="st-key-override_submit_confirm_"][class*="_approved"] button{
  background:linear-gradient(180deg,#27865a,#1e8449) !important;border:1px solid #176437 !important;
  color:#ffffff !important;border-radius:6px !important;height:40px !important;min-height:40px !important;
  font-weight:700 !important;cursor:pointer !important;opacity:1 !important;}
div[class*="st-key-override_submit_confirm_"][class*="_rejected"] button{
  background:linear-gradient(180deg,#a01818,#7b0000) !important;border:1px solid #650000 !important;
  color:#ffffff !important;border-radius:6px !important;height:40px !important;min-height:40px !important;
  font-weight:700 !important;cursor:pointer !important;opacity:1 !important;}
</style>""")
            # Exactly one success toast after a completed decision.
            if st.session_state.get("override_toast"):
                _dec = st.session_state.pop("override_toast")
                st.toast("Override request approved and moved to Override History."
                         if _dec == "approved"
                         else "Override request rejected and moved to Override History.")
            # Approve/Reject open a required manager-decision dialog via the single
            # dialog coordinator at the end of the file (never invoked here), so the
            # decision modal can never coincide with the Case File or reset modal.

            st.markdown("""
            <div class="panel">
              <div class="panel-header">
                <span class="panel-title">Pending Override Requests</span>
              </div>
            </div>""", unsafe_allow_html=True)

            ov_fresh   = load_overrides()
            pending_ov = (
                ov_fresh[ov_fresh["status"] == "pending"]
                if not ov_fresh.empty else pd.DataFrame()
            )

            if pending_ov.empty:
                st.markdown(
                    '<div style="background:#fff;border:1px solid #b0b0b0;'
                    'padding:20px;font-size:14px;color:#5a6570">'
                    'No pending override requests.</div>',
                    unsafe_allow_html=True,
                )
            else:
                mr_valid_alerts = alerts_df["alert_id"].tolist()
                for _, row in pending_ov.iterrows():
                    st.markdown(f"""
                    <div class="ov-card">
                      <div class="ov-card-top">
                        <span class="ov-card-id">{override_display_id(row['change_id'])}</span>
                        <span class="ov-card-ts">{row['changed_at']}</span>
                      </div>
                      <div class="ov-card-body">
                        <b>Alert:</b> {row['alert_id']} &nbsp;·&nbsp;
                        <b>Field:</b> {row['field_changed']} &nbsp;·&nbsp;
                        <b>From:</b>
                        <span class="ov-old">{sev_label(row['field_changed'], row['old_value'])}</span>
                        &nbsp;→&nbsp;
                        <b>To:</b>
                        <span class="ov-new">{sev_label(row['field_changed'], row['new_value'])}</span>
                      </div>
                      <div class="ov-card-meta">
                        <b>Submitted by:</b> {row['changed_by_name']} ({row['changed_by_id']})
                      </div>
                      <div class="ov-card-reason">
                        <b>Reason:</b> {row['reason']}
                      </div>
                    </div>""", unsafe_allow_html=True)

                    # Responsive action row: one keyed horizontal container per override
                    # (replaces the old 4-column + spacer layout, which starved the buttons
                    # on narrow windows). Order, keys, types, callbacks, and the Open-Case
                    # guard condition are unchanged; only the layout mechanism changed.
                    _target = case_target_for_override(row, mr_valid_alerts)
                    with st.container(
                        key=f"ov_actions_{row['change_id']}",
                        horizontal=True,
                        horizontal_alignment="left",
                        vertical_alignment="center",
                        gap="small",
                    ):
                        if _target and st.button("Open Case File", key=f"oc_{row['change_id']}", width="content"):
                            # CORRECTION 1: inspect THIS override's evidence in the existing
                            # Case File dialog. _open_case_file clears override/reset state so
                            # only the Case File opens. Only this control is interactive.
                            _open_case_file(_target)
                            st.rerun()
                        if st.button("✓ Approve", key=f"apr_{row['change_id']}", type="primary", width="content"):
                            # Open the required decision dialog; nothing is persisted yet.
                            _open_override_decision(row["change_id"], "approved")
                            st.rerun()
                        if st.button("✕ Reject", key=f"rej_{row['change_id']}", type="secondary", width="content"):
                            _open_override_decision(row["change_id"], "rejected")
                            st.rerun()

            if not ov_fresh.empty:
                OV_STATUS_STYLE = {
                    "pending":  "background:#fff8e1;color:#7d4e00;border:1px solid #f0c040;",
                    "approved": "background:#e8f5e8;color:#1a5c1a;border:1px solid #9c9;",
                    "rejected": "background:#fde8e8;color:#7b0000;border:1px solid #c88;",
                }

                def _hesc(v):
                    return (str(v).replace("&", "&amp;").replace("<", "&lt;")
                            .replace(">", "&gt;"))

                hist_rows = "".join(
                    f'<tr>'
                    f'<td style="white-space:nowrap;font-weight:800;color:#3F4C5E;">{_hesc(override_display_id(r["change_id"]))}</td>'
                    f'<td style="white-space:nowrap;">{_hesc(r["alert_id"])}</td>'
                    f'<td>{_hesc(r["field_changed"])}</td>'
                    f'<td><span class="ov-old">{_hesc(sev_label(r["field_changed"], r["old_value"]))}</span>'
                    f' → <span class="ov-new">{_hesc(sev_label(r["field_changed"], r["new_value"]))}</span></td>'
                    f'<td style="white-space:nowrap;">{_hesc(r["changed_by_name"])}</td>'
                    f'<td><span style="display:inline-block;padding:3px 9px;border-radius:3px;'
                    f'font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;'
                    f'{OV_STATUS_STYLE.get(r["status"], "")}">{_hesc(r["status"])}</span></td>'
                    f'<td style="white-space:nowrap;">{_hesc(r["reviewed_by"]) or "—"}</td>'
                    f'<td style="color:#888;font-variant-numeric:tabular-nums;white-space:nowrap;">{_hesc(r["reviewed_at"]) or "—"}</td>'
                    f'<td>{(_hesc(r["review_note"]) if (r["status"] in ("approved", "rejected") and r["review_note"]) else "—")}</td>'
                    f'</tr>'
                    for _, r in ov_fresh.iterrows()
                )
                # Wrap ONLY the Override History heading + table in a keyed container so
                # this panel is boxed without affecting Change Log or Audit Trail.
                with st.container(key="override_history_panel"):
                    st.markdown("""
                    <div class="panel" style="margin-top:20px">
                      <div class="panel-header">
                        <span class="panel-title">Override History</span>
                        <span class="panel-subtitle">All submitted overrides</span>
                      </div>
                    </div>""", unsafe_allow_html=True)
                    st.markdown(
                        '<div style="font-size:12px;color:#5d6570;margin-top:3px;margin-bottom:10px;">'
                        'Pending, approved, and rejected requests remain here as the permanent decision record.'
                        '</div>',
                        unsafe_allow_html=True,
                    )
                    st.markdown(
                        f'<table class="log-tbl">'
                        f'<thead><tr><th>Request ID</th><th>Alert</th><th>Field</th>'
                        f'<th>Change</th><th>Submitted By</th><th>Status</th>'
                        f'<th>Reviewed By</th><th>Reviewed At</th><th>Decision Rationale</th></tr></thead>'
                        f'<tbody>{hist_rows}</tbody></table>',
                        unsafe_allow_html=True,
                    )

    st.markdown('</div>', unsafe_allow_html=True)

# ═════════════════════════════════════════════════════════════════════════════
# TAB 3 — RISK SETTINGS
# ═════════════════════════════════════════════════════════════════════════════
with tab3:
    st.markdown('<div class="page-body">', unsafe_allow_html=True)
    st.markdown("""
    <div class="panel">
      <div class="panel-header">
        <span class="panel-title">Risk Configuration</span>
        <span class="panel-subtitle">Changes are logged to the shared audit trail (data/audit_log.csv)</span>
      </div>
    </div>""", unsafe_allow_html=True)

    rs   = st.session_state.risk_settings
    rc1, rc2 = st.columns(2, gap="large")

    with rc1:
        with st.container(border=True):
            st.markdown('<div class="settings-section-title">Severity Thresholds</div>', unsafe_allow_html=True)
            st.markdown('<p class="field-desc-txt"><b>High threshold</b> — alerts at or above this require Senior Analyst / Manager review.</p>', unsafe_allow_html=True)
            new_high   = st.number_input("High severity trigger (≥)",   50, 100,         rs["high_threshold"],       5,  key="ni_high")
            st.markdown('<p class="field-desc-txt"><b>Medium threshold</b> — alerts between this and High get standard analyst review.</p>', unsafe_allow_html=True)
            new_medium = st.number_input("Medium severity trigger (≥)", 20, int(new_high)-5, min(rs["medium_threshold"], new_high-5), 5, key="ni_med")

        st.markdown('<div style="height:14px"></div>', unsafe_allow_html=True)

        with st.container(border=True):
            st.markdown('<div class="settings-section-title">Evidence Completeness Gates</div>', unsafe_allow_html=True)
            st.markdown('<p class="field-desc-txt"><b>KYC staleness limit</b> — profiles older than this (months) fail the readiness check. Default: 12.</p>', unsafe_allow_html=True)
            new_kyc    = st.number_input("KYC staleness limit (months)",         3,  36,  rs["kyc_staleness_months"],  3,  key="ni_kyc")
            st.markdown('<p class="field-desc-txt"><b>Transaction history</b> — minimum days required before AI can draft. Default: 90.</p>', unsafe_allow_html=True)
            new_txn    = st.number_input("Transaction history required (days)",  30, 180, rs["txn_history_days"],      30, key="ni_txn")
            st.markdown('<br>', unsafe_allow_html=True)
            new_cpty   = st.checkbox("Require counterparty identification", value=rs["require_counterparty_id"])
            new_sar    = st.checkbox("Require prior SAR history check",     value=rs["require_prior_sar_check"])

    with rc2:
        with st.container(border=True):
            st.markdown('<div class="settings-section-title">AI & Governance Controls</div>', unsafe_allow_html=True)
            st.markdown('<p class="field-desc-txt"><b>Block AI draft until readiness passes</b> — core build principle. Disabling violates the governance posture.</p>', unsafe_allow_html=True)
            new_ai_gate = st.checkbox("Block AI draft until readiness check passes", value=rs["ai_draft_requires_readiness"])
            st.markdown('<p class="field-desc-txt"><b>Anti-rubber-stamp gate</b> — a human review with a missing required field (evidence reviewed, decision reason, final note, final action, disposition) is blocked by src/review_gate + the decision pipeline.</p>', unsafe_allow_html=True)
            new_rubber  = st.checkbox("Enforce anti-rubber-stamp gate", value=rs["block_rubber_stamp"])
            if not rs["block_rubber_stamp"]:
                st.markdown(
                    '<div class="gate-panel gate-disabled" style="margin-top:8px;">'
                    '<div class="gate-title">⚠ GOVERNANCE ENFORCEMENT DISABLED</div>'
                    '<div class="gate-body">The anti-rubber-stamp gate is OFF. Incomplete '
                    'human reviews will not be held. Re-enable to restore enforcement.'
                    '</div></div>',
                    unsafe_allow_html=True,
                )

        st.markdown('<div style="height:14px"></div>', unsafe_allow_html=True)

        with st.container(border=True):
            st.markdown('<div class="settings-section-title">Typology Keywords</div>', unsafe_allow_html=True)
            st.markdown('<p class="field-desc-txt">Keywords per AML typology used by the AI drafter for claim matching.</p>', unsafe_allow_html=True)
            rule_choice = st.selectbox(
                "Typology", list(st.session_state.keywords.keys()),
                label_visibility="collapsed",
            )
            current_kws = st.session_state.keywords[rule_choice]
            chips = "".join(f'<span class="kw-chip">{k}</span>' for k in current_kws)
            st.markdown(
                chips or '<span style="color:#999;font-size:13px">None defined.</span>',
                unsafe_allow_html=True,
            )
            st.markdown('<br>', unsafe_allow_html=True)
            new_kw = st.text_input(
                "Add keyword",
                placeholder="e.g. layering, shell company",
                key="kw_input",
                label_visibility="collapsed",
            )
            kc1, kc2 = st.columns(2)
            with kc1:
                if st.button("＋ Add Keyword", use_container_width=True):
                    if new_kw.strip() and new_kw.strip() not in current_kws:
                        st.session_state.keywords[rule_choice].append(new_kw.strip())
                        st.session_state.settings_cl.append({
                            "time":   datetime.now(timezone.utc).strftime("%H:%M:%S"),
                            "type":   "Keyword Added",
                            "detail": f'"{new_kw.strip()}" → {rule_choice}',
                        })
                        write_log("keyword_added", {"typology": rule_choice, "keyword": new_kw.strip()})
                        st.rerun()
            with kc2:
                kw_del = st.selectbox(
                    "Remove", ["— remove —"] + current_kws,
                    label_visibility="collapsed", key="kw_remove",
                )
                if kw_del != "— remove —":
                    if st.button("✕ Remove", use_container_width=True):
                        st.session_state.keywords[rule_choice].remove(kw_del)
                        st.session_state.settings_cl.append({
                            "time":   datetime.now(timezone.utc).strftime("%H:%M:%S"),
                            "type":   "Keyword Removed",
                            "detail": f'"{kw_del}" ← {rule_choice}',
                        })
                        write_log("keyword_removed", {"typology": rule_choice, "keyword": kw_del})
                        st.rerun()

    st.markdown('<br>', unsafe_allow_html=True)
    if st.button("Save Risk Settings", type="primary"):
        mapping = [
            ("high_threshold",             new_high,    "High threshold"),
            ("medium_threshold",           new_medium,  "Medium threshold"),
            ("kyc_staleness_months",       new_kyc,     "KYC staleness (months)"),
            ("txn_history_days",           new_txn,     "Txn history (days)"),
            ("require_counterparty_id",    new_cpty,    "Require counterparty ID"),
            ("require_prior_sar_check",    new_sar,     "Require SAR check"),
            ("ai_draft_requires_readiness",new_ai_gate, "Block AI on incomplete cases"),
            ("block_rubber_stamp",         new_rubber,  "Anti-rubber-stamp gate"),
        ]
        changes = []
        for key, nv, lbl in mapping:
            if nv != rs[key]:
                changes.append(f"{lbl}: {rs[key]} → {nv}")
                st.session_state.risk_settings[key] = nv
        if changes:
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            for c in changes:
                st.session_state.settings_cl.append({"time": ts, "type": "Setting Changed", "detail": c})
            write_log("risk_settings_saved", {"changes": changes})
            st.success(f"{len(changes)} setting(s) saved and logged to the audit trail.")
        else:
            st.info("No changes to save.")
    st.markdown('</div>', unsafe_allow_html=True)

# ═════════════════════════════════════════════════════════════════════════════
# TAB 4 — CHANGE LOG
# ═════════════════════════════════════════════════════════════════════════════
with tab4:
    st.markdown('<div class="page-body">', unsafe_allow_html=True)
    st.markdown("""
    <div class="panel">
      <div class="panel-header">
        <span class="panel-title">Configuration Change Log</span>
      </div>
    </div>""", unsafe_allow_html=True)

    scl = st.session_state.settings_cl
    if not scl:
        st.markdown(
            '<div style="background:#fff;border:1px solid #b0b0b0;'
            'padding:20px;font-size:14px;color:#5a6570">No changes this session.</div>',
            unsafe_allow_html=True,
        )
    else:
        tt   = {"Setting Changed":"lt-change","Keyword Added":"lt-add","Keyword Removed":"lt-remove"}
        rows = "".join(
            f'<tr>'
            f'<td style="color:#888;font-variant-numeric:tabular-nums">{e["time"]}</td>'
            f'<td><span class="{tt.get(e["type"],"")}">{e["type"]}</span></td>'
            f'<td>{e["detail"]}</td>'
            f'</tr>'
            for e in reversed(scl)
        )
        st.markdown(
            f'<table class="log-tbl">'
            f'<thead><tr><th>Time</th><th>Type</th><th>Detail</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>',
            unsafe_allow_html=True,
        )
        st.markdown('<br>', unsafe_allow_html=True)
        if st.button("Clear Session Log", type="secondary"):
            st.session_state.settings_cl = []
            st.rerun()
    st.markdown('</div>', unsafe_allow_html=True)

# ═════════════════════════════════════════════════════════════════════════════
# TAB 5 — AUDIT TRAIL  (data/audit_log.csv via src.audit)
# ═════════════════════════════════════════════════════════════════════════════
with tab5:
    st.markdown('<div class="page-body">', unsafe_allow_html=True)
    st.markdown("""
    <div class="panel">
      <div class="panel-header">
        <span class="panel-title">Audit Trail</span>
      </div>
    </div>""", unsafe_allow_html=True)

    if not AUDIT_LOG_CSV.exists():
        st.markdown(
            '<div style="background:#fff;border:1px solid #b0b0b0;'
            'padding:20px;font-size:14px;color:#5a6570">'
            'No audit entries yet. Actions taken in this workbench '
            '(overrides, settings changes, claim verification) write rows here automatically.</div>',
            unsafe_allow_html=True,
        )
    else:
        audit_df = pd.read_csv(AUDIT_LOG_CSV, dtype=str, keep_default_na=False)
        audit_df = audit_df.sort_values("timestamp", ascending=False)
        st.markdown(
            f'<div style="font-size:13px;color:#555;margin-bottom:10px">'
            f'{len(audit_df)} entries in data/audit_log.csv</div>',
            unsafe_allow_html=True,
        )

        def _esc(v):
            return (str(v).replace("&", "&amp;").replace("<", "&lt;")
                    .replace(">", "&gt;"))

        audit_rows = "".join(
            f'<tr>'
            f'<td style="color:#888;font-variant-numeric:tabular-nums;white-space:nowrap;">{_esc(r["timestamp"])}</td>'
            f'<td style="white-space:nowrap;">{_esc(r["actor"])}</td>'
            f'<td><span class="lt-change">{_esc(r["action"])}</span></td>'
            f'<td style="white-space:nowrap;">{_esc(r["alert_id"]) or "—"}</td>'
            f'<td style="color:#555;">{_esc(r["details_json"])}</td>'
            f'</tr>'
            for _, r in audit_df.iterrows()
        )
        st.markdown(
            f'<table class="log-tbl">'
            f'<thead><tr><th>Timestamp</th><th>Actor</th><th>Action</th>'
            f'<th>Alert</th><th>Details</th></tr></thead>'
            f'<tbody>{audit_rows}</tbody></table>',
            unsafe_allow_html=True,
        )
    st.markdown('</div>', unsafe_allow_html=True)


# ═════════════════════════════════════════════════════════════════════════════
# DIALOG COORDINATOR — exactly one modal per rerun
# ═════════════════════════════════════════════════════════════════════════════
# Streamlit raises "Only one dialog is allowed to be opened at the same time" if
# two @st.dialog functions are invoked during a single rerun (and tab1..tab5 all
# execute every rerun). Every dialog in this app is therefore invoked ONLY from
# this one mutually-exclusive if/elif/elif — never inside a tab, never pre-tabs —
# in fixed priority order: Reset Demo → Override decision → Case File. The _open_*
# helpers additionally clear the other dialogs' state on open, so conflicting
# modal state never coexists in the first place. This invokes no CSV write and no
# audit event; opening a modal only reads state.
if st.session_state.get("demo_reset_confirm"):
    _demo_reset_dialog()
elif st.session_state.get("pending_override"):
    _po = st.session_state["pending_override"]
    (_approve_override_dialog if _po.get("decision") == "approved"
     else _reject_override_dialog)()
elif st.session_state.get("open_case"):
    _aid = st.session_state.open_case
    st.session_state.open_case = None          # call-once: Case File behavior unchanged
    show_case_dialog(_aid, source)
