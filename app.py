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


# Project root is the parent of lumen_ui/. Add it to sys.path so `src` can be
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src import audit, verifier  # noqa: E402
from src.review_gate import evaluate_review  # noqa: E402

st.set_page_config(
    page_title="Lumen Verify | AML Workbench",
    layout="wide",
    page_icon="",
)

# ── Paths ─────────────────────────────────────────────────────────────────────
DATA_DIR        = PROJECT_ROOT / "data"
OVERRIDES_CSV   = DATA_DIR / "pending_overrides.csv"
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


def load_overrides() -> pd.DataFrame:
    cols = [
        "change_id", "alert_id", "field_changed", "old_value", "new_value",
        "changed_by_id", "changed_by_name", "changed_at", "reason",
        "status", "reviewed_by", "reviewed_at",
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


def build_queue_row(alert_row: pd.Series, source: dict) -> dict:
    alert_id = alert_row["alert_id"]
    customers = source["customers"]
    crow = customers[customers["customer_id"] == alert_row["customer_id"]]
    customer_name = crow.iloc[0]["name"] if len(crow) else alert_row["customer_id"]

    ai_rows = source["ai_outputs"][source["ai_outputs"]["alert_id"] == alert_id]
    has_ai = len(ai_rows) > 0

    review_rows = source["human_reviews"][source["human_reviews"]["alert_id"] == alert_id]
    analyst = review_rows.iloc[0]["reviewer"] if len(review_rows) else "Unassigned"

    return {
        "alert_id": alert_id,
        "customer_id": alert_row["customer_id"],
        "customer": customer_name,
        "rule": alert_row["rule_triggered"],
        "severity": SEVERITY_LABELS.get(alert_row["severity"], alert_row["severity"]),
        "readiness": case_readiness_pct(alert_id, source),
        "ai": has_ai,
        "status": STATUS_LABELS.get(alert_row["status"], alert_row["status"]),
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

queue_df = pd.DataFrame([build_queue_row(r, source) for _, r in alerts_df.iterrows()])
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
    st.session_state.open_case = _qp_alert
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
.stApp{background:#eef1f4;}
.stMainBlockContainer{padding:0 !important;max-width:100% !important;}
header[data-testid="stHeader"]{display:none !important;}
div[data-testid="stToolbar"]{display:none !important;}
#MainMenu{display:none !important;}

/* FIX 1: id-bar gets a bottom border to cleanly separate from light page body */
.id-bar{background:#2e728f;padding:13px 26px;display:flex;justify-content:space-between;align-items:center;border-bottom:3px solid #1a5276;}
.id-bar-logo{font-size:24px;font-weight:700;color:#fff;letter-spacing:.02em;margin:0;}
.id-bar-logo span{color:#cfeaf5;}
.id-bar-right{font-size:14px;color:#eaf4f8;display:flex;gap:20px;align-items:center;}
.id-bar-right a{color:#eaf4f8;text-decoration:none;}
.id-bar-right a:hover{text-decoration:underline;}
.id-bar-sep{color:#6ba3ba;}
.id-bar-user{background:#255d75;border:1px solid #4a8ba5;padding:6px 14px;border-radius:4px;color:#fff !important;font-size:14px;font-weight:600;}

.sub-nav{background:#245d74;padding:0 26px;display:flex;align-items:center;justify-content:space-between;height:40px;border-bottom:3px solid #1a4f66;box-shadow:0 2px 4px rgba(0,0,0,.12);}
.sub-nav-left{font-size:14px;font-weight:600;color:#f3fafc;}
/* FIX 2: sub-nav-right font bumped to 13px — readable without being loud */
.sub-nav-right{font-size:13px;color:#e8f4f8;font-weight:500;font-variant-numeric:tabular-nums;display:flex;align-items:center;gap:14px;}
.hdr-pending{display:inline-flex;align-items:center;gap:5px;background:#c0392b;color:#fff;font-size:12px;font-weight:700;padding:3px 10px;border-radius:11px;letter-spacing:.02em;}
.hdr-pending .dot{width:6px;height:6px;border-radius:50%;background:#fff;display:inline-block;}

.page-body{padding:12px 26px 20px 26px;}
.section-h{font-size:18px !important;font-weight:700 !important;color:#173453 !important;margin:0 0 12px 0 !important;padding:0 !important;}
.section-h .section-count{color:#5a6570;font-weight:600;font-size:14px;}

/* FIX 3: role-bar (warning banner) — bigger padding, bigger font, visible */
.role-bar{background:#fff4d6;border:1px solid #eab308;border-left:6px solid #eab308;border-radius:6px;padding:14px 20px;display:flex;align-items:center;gap:12px;font-size:15px;line-height:1.5;color:#5d4000;min-height:52px;}
.role-bar b{color:#3d2b00;}
.pending-badge{display:inline-flex;align-items:center;gap:6px;background:#fde8e8;border:1px solid #d99;border-radius:5px;padding:10px 14px;font-size:14px;font-weight:700;color:#a01818;justify-content:center;}

.metric-strip{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:18px;}
.metric-cell{background:#fff;padding:16px 20px;border:1px solid #cdd6de;border-top:4px solid #1a5276;border-radius:6px;box-shadow:0 1px 2px rgba(0,0,0,.05);}
.metric-cell-label{font-size:12px;font-weight:700;color:#4a5560;letter-spacing:.06em;text-transform:uppercase;margin-bottom:6px;}
.metric-cell-value{font-size:34px;font-weight:800;line-height:1;color:#1a1a1a;font-variant-numeric:tabular-nums;}
.metric-cell-value.danger{color:#a01818;}
.metric-cell-value.warn{color:#8a5600;}
.metric-cell-value.info{color:#1a5276;}
.metric-cell-sub{font-size:12px;color:#5a6570;margin-top:5px;font-weight:500;}

.panel{background:#fff;border:1px solid #cdd6de;border-radius:6px;margin-bottom:16px;overflow:hidden;}
.panel-header{background:#f4f7fa;border-bottom:1px solid #cdd6de;padding:11px 18px;display:flex;justify-content:space-between;align-items:center;}
.panel-title{font-size:17px;font-weight:700;color:#173453;}
.panel-subtitle{font-size:13px;color:#5a6570;}

.data-table{width:100%;border-collapse:collapse;font-size:13px;}
.data-table thead tr{background:linear-gradient(to bottom,#e0e8f0,#c8d8e8);}
.data-table th{padding:8px 12px;text-align:left;font-size:12px;font-weight:700;color:#1a3a5c;border-right:1px solid #b8ccd8;border-bottom:2px solid #8aaabf;white-space:nowrap;}
.data-table th:last-child{border-right:none;}
.data-table td{padding:8px 12px;border-bottom:1px solid #e8e8e8;border-right:1px solid #f0f0f0;color:#1a1a1a;vertical-align:middle;}
.data-table td:last-child{border-right:none;}
.data-table tbody tr:nth-child(even) td{background:#f4f7fa;}
.data-table tbody tr:nth-child(odd) td{background:#fff;}
.data-table tbody tr:hover td{background:#ddeeff !important;cursor:pointer;}
.data-table tbody tr.selected td{background:#c8dcf0 !important;border-left:3px solid #1a5276;}
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

.edit-form{background:#f8f9fa;border:1px solid #b8ccd8;border-left:4px solid #1a5276;padding:14px 16px;margin:6px 0 10px 0;}
.edit-form-title{font-size:13px;font-weight:700;color:#1a5276;letter-spacing:.08em;text-transform:uppercase;margin-bottom:12px;}

.case-panel{background:#fff;border:1px solid #b0b0b0;margin-top:14px;}
.case-panel-hdr{background:linear-gradient(to bottom,#1a5276,#154360);padding:8px 14px;display:flex;justify-content:space-between;align-items:center;}
.case-panel-title{font-size:13px;font-weight:700;color:#fff;}
.case-panel-id{font-size:11px;color:#a8c4e0;}
.case-grid{display:grid;grid-template-columns:1fr 1fr;}
.case-section{padding:14px 16px;border-right:1px solid #e8e8e8;border-bottom:1px solid #e8e8e8;}
.case-section:nth-child(even){border-right:none;}
.case-section-title{font-size:11px;font-weight:700;color:#1a5276;letter-spacing:.1em;text-transform:uppercase;margin-bottom:10px;padding-bottom:5px;border-bottom:1px solid #d0e0ec;}
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

.settings-section-title{font-size:13px;font-weight:700;color:#1a5276;letter-spacing:.06em;text-transform:uppercase;margin-bottom:10px;}
.field-desc-txt{font-size:13px;color:#333;margin:2px 0 8px 0;line-height:1.45;}
.kw-chip{display:inline-block;padding:3px 9px;background:#e8eeff;color:#1a2e8c;border:1px solid #99aacc;border-radius:3px;font-size:13px;font-weight:500;margin:2px;}

/* FIX 4: log-tbl and badges — all bumped to 13px for readability */
.log-tbl{width:100%;border-collapse:collapse;font-size:13px;border:1px solid #cdd6de;}
.log-tbl th{padding:10px 14px;background:#e0e8f0;border-bottom:2px solid #8aaabf;font-size:13px;font-weight:700;color:#1a3a5c;text-transform:uppercase;letter-spacing:.05em;text-align:left;}
.log-tbl td{padding:9px 14px;border-bottom:1px solid #e8e8e8;color:#2a2a2a;font-size:13px;}
.log-tbl tbody tr:nth-child(even) td{background:#f7f9fc;}
.lt-change{background:#fef3e2;color:#6b3800;border:1px solid #dba;padding:3px 9px;border-radius:3px;font-size:13px;font-weight:700;}
.lt-add{background:#e8f5e8;color:#1a5c1a;border:1px solid #9c9;padding:3px 9px;border-radius:3px;font-size:13px;font-weight:700;}
.lt-remove{background:#fde8e8;color:#7b0000;border:1px solid #c88;padding:3px 9px;border-radius:3px;font-size:13px;font-weight:700;}

div[data-testid="stTabs"]>div:first-child{background:linear-gradient(to bottom,#eef1f4,#dfe4ea) !important;border-bottom:2px solid #cdd6de !important;padding:0 26px !important;gap:0 !important;}
button[data-baseweb="tab"]{font-size:14px !important;font-weight:600 !important;color:#3a4652 !important;padding:11px 20px !important;border-radius:0 !important;background:transparent !important;border-bottom:3px solid transparent !important;}
button[data-baseweb="tab"][aria-selected="true"]{background:#fff !important;color:#173453 !important;border-bottom:3px solid #1a5276 !important;}
div[data-testid="stTabs"]>div:nth-child(2){background:#eef1f4 !important;}
div[data-testid="stNumberInput"] input{background:#fff !important;border:1px solid #999 !important;border-radius:3px !important;font-size:14px !important;color:#111 !important;font-weight:600 !important;}
div[data-testid="stTextInput"] input{background:#fff !important;border:1px solid #999 !important;border-radius:3px !important;font-size:14px !important;color:#111 !important;}
div[data-testid="stSelectbox"]>div>div{background:#fff !important;border:1px solid #999 !important;border-radius:3px !important;font-size:14px !important;color:#111 !important;}
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
ul[data-baseweb="menu"],ul[role="listbox"]{background:#fff !important;}
ul[data-baseweb="menu"] li,li[role="option"]{background:#fff !important;color:#111 !important;}
div[data-testid="stDataFrame"]{background:#fff !important;}
div[data-testid="stDataFrame"] [data-testid="stTable"]{background:#fff !important;}
.stButton>button{border-radius:4px !important;font-size:13px !important;font-weight:700 !important;letter-spacing:.02em !important;padding:8px 16px !important;border:1px solid !important;}
.stButton>button[kind="primary"]{background:linear-gradient(to bottom,#2166a8,#1a5276) !important;color:#fff !important;border-color:#154360 !important;}
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
  padding:14px 16px 16px 16px !important;background:#fff;}
.filter-bar-labels{display:grid;grid-template-columns:2.1fr 1.6fr 2.3fr;
  gap:1rem;margin-bottom:6px;}
.filter-bar-labels span{font-size:12px;font-weight:700;color:#5a6570;
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
.ov-card{background:#ffffff;border:1px solid #cdd6de;border-left:5px solid #f0c040;
  border-radius:8px;padding:16px 18px;margin:0 0 16px 0;
  box-shadow:0 4px 12px rgba(23,52,83,.12);}
/* CORRECTION 1: whole-card hover removed — the card must NOT read as clickable.
   Interactivity lives only in the per-card "Open Case File" button. */
.ov-card-top{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:10px;}
.ov-card-id{font-size:15px;font-weight:700;color:#173453;}
.ov-card-ts{font-size:13px;color:#5a6570;font-variant-numeric:tabular-nums;}
.ov-card-body{font-size:14px;color:#1a1a1a;margin-bottom:8px;line-height:1.5;}
.ov-card-meta{font-size:14px;color:#4a5560;margin-bottom:6px;}
.ov-card-reason{font-size:14px;color:#1a1a1a;line-height:1.5;}
/* Manager Review — Override History panel wrapper (keyed st.container). Boxes only
   this panel; Change Log and Audit Trail are unaffected. */
.st-key-override_history_panel{background:#ffffff;border:1px solid #cdd6de;
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
  --accent:#2e728f;          /* existing: id-bar / table header / lv-open */
  --accent-strong:#1a5276;   /* existing: navy — active-tab text / strong accent */
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
  outline:3px solid #4a8ba5 !important;outline-offset:2px !important;}
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
  outline:3px solid #4a8ba5 !important;outline-offset:2px !important;}

/* TASK 3 (pass 1) removed by CORRECTION 1: the pending-card hover created a false
   "whole card is clickable" affordance. Card interactivity now lives only in the
   per-card "Open Case File" button (Manager Review). */

/* TASK 4 — tab bar: accent active indicator, inactive hover, no default
   Streamlit tab accent left visible. */
button[data-baseweb="tab"][aria-selected="true"]{
  color:var(--accent-strong) !important;
  border-bottom:3px solid var(--accent) !important;}
button[data-baseweb="tab"][aria-selected="false"]:hover{
  color:var(--accent-strong) !important;
  background:var(--surface-hover) !important;}
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

/* TASK 7 — BaseWeb dropdown menu (renders in a PORTAL outside the widget), so
   target it at the menu/listbox level. NOTE: the severity filter is a
   segmented_control (chips, protected) with no dropdown; this styles the app's
   real dropdown menus (search, typology, remove-keyword). */
ul[data-baseweb="menu"] li[role="option"]:hover,
ul[role="listbox"] li[role="option"]:hover{
  background:var(--surface-hover) !important;color:var(--accent-strong) !important;}
ul[data-baseweb="menu"] li[role="option"][aria-selected="true"],
ul[role="listbox"] li[role="option"][aria-selected="true"]{
  background:var(--accent-tint) !important;color:var(--accent-strong) !important;}

/* TASK 8 — demo-mode banner / role-switch button: neutral secondary variant of
   the Task 2 button system (NOT approve/reject colors), scoped to the row with
   the .role-bar banner. The session ID is display text, not a button. */
div[data-testid="stHorizontalBlock"]:has(.role-bar) .stButton>button{
  background:linear-gradient(to bottom,var(--neutral-a),var(--neutral-b)) !important;
  color:var(--neutral-text) !important;border-color:var(--neutral-border) !important;
  transition:filter .12s ease,box-shadow .12s ease,transform .05s ease;}
div[data-testid="stHorizontalBlock"]:has(.role-bar) .stButton>button:hover{
  filter:brightness(.97) !important;box-shadow:0 2px 6px rgba(0,0,0,.12) !important;}
div[data-testid="stHorizontalBlock"]:has(.role-bar) .stButton>button:active{
  filter:brightness(.93) !important;transform:translateY(1px);box-shadow:none !important;}
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
  line-height:1.5;border:1px solid #cdd6de;border-left:5px solid var(--accent);
  background:#fff;color:#1a1a1a;}
.outcome-link.ok{border-left-color:var(--pass);}
.outcome-link.warn{border-left-color:var(--warn);background:var(--warn-bg);color:var(--warn-text);}
.outcome-link b{color:var(--accent-strong);}
.outcome-link.warn b{color:#3d2b00;}

/* CORRECTION 5 — real BaseWeb dropdown menus (Search, Typology, Remove Keyword):
   visible hover, selected, and keyboard-focus states with readable contrast, at
   the portal (menu) level. Existing vars only. */
ul[data-baseweb="menu"] li[role="option"]{color:#1a1a1a !important;}
ul[data-baseweb="menu"] li[role="option"]:hover,
ul[data-baseweb="menu"] li[role="option"][data-highlighted="true"]{
  background:var(--surface-hover) !important;color:var(--accent-strong) !important;}
ul[data-baseweb="menu"] li[role="option"][aria-selected="true"]{
  background:var(--accent-tint) !important;color:var(--accent-strong) !important;
  font-weight:600 !important;}
ul[data-baseweb="menu"] li[role="option"]:focus,
ul[data-baseweb="menu"] li[role="option"]:focus-visible{
  outline:2px solid var(--accent) !important;outline-offset:-2px !important;
  background:var(--surface-hover) !important;}

/* HERO CASE B — human-review gate panel in the Case File (display of the shared
   src/review_gate rule). Existing semantic colors/vars only; no new colors. */
.gate-panel{border-radius:6px;padding:12px 16px;margin-top:12px;font-size:14px;
  line-height:1.5;border:1px solid #cdd6de;border-left:6px solid var(--accent);background:#fff;}
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
  background:#dfe4ea !important;border:1px solid #b8ccd8 !important;
  border-radius:10px !important;padding:6px !important;gap:6px !important;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.6),0 1px 3px rgba(0,0,0,.12) !important;}
/* every tab, including inactive: its own white bordered tab, equal height */
button[data-baseweb="tab"]{
  background:#ffffff !important;color:#173453 !important;
  border:1px solid #cdd6de !important;border-radius:6px !important;
  padding:8px 18px !important;
  box-shadow:0 1px 2px rgba(0,0,0,.08) !important;
  transition:background .12s ease,border-color .12s ease,box-shadow .12s ease,color .12s ease,transform .08s ease !important;}
/* inactive hover: light-teal surface, teal-blue border, lifts 1px */
button[data-baseweb="tab"][aria-selected="false"]:hover{
  background:#e8f4f8 !important;border-color:#4a8ba5 !important;color:#173453 !important;
  transform:translateY(-1px) !important;box-shadow:0 2px 4px rgba(0,0,0,.12) !important;}
/* active tab: strong solid-teal selected state, white text */
button[data-baseweb="tab"][aria-selected="true"]{
  background:#2e728f !important;color:#ffffff !important;
  border-color:#1a5276 !important;
  box-shadow:0 2px 5px rgba(0,0,0,.18) !important;}
/* active hover: stay teal with white text, darken toward navy (never white) */
button[data-baseweb="tab"][aria-selected="true"]:hover{
  background:#1a5276 !important;color:#ffffff !important;border-color:#1a5276 !important;
  box-shadow:0 2px 6px rgba(0,0,0,.20) !important;}
/* strong keyboard focus */
button[data-baseweb="tab"]:focus-visible{
  outline:3px solid #1a5276 !important;outline-offset:2px !important;border-radius:6px !important;}
/* no separate sliding underline — each tab's own border is the boundary */
div[data-baseweb="tab-highlight"]{background-color:transparent !important;height:0 !important;}
div[data-baseweb="tab-border"]{background-color:transparent !important;height:0 !important;}

/* "Open Case File" (Manager Review) — neutral outlined secondary action, exact
   spec colors; keyed .st-key-oc_* hook; st.button + routing/callbacks unchanged. */
div[class*="st-key-oc_"] .stButton>button[kind="secondary"]{
  background:#ffffff !important;color:#1a5276 !important;
  border:1px solid #2e728f !important;border-radius:6px !important;
  box-shadow:0 1px 2px rgba(23,52,83,.08) !important;
  transition:background .15s ease,border-color .15s ease,box-shadow .15s ease,transform .15s ease !important;}
div[class*="st-key-oc_"] .stButton>button[kind="secondary"]:hover{
  background:#e8f4f8 !important;color:#173453 !important;border-color:#245d74 !important;
  box-shadow:0 2px 5px rgba(23,52,83,.14) !important;transform:translateY(-1px) !important;}
div[class*="st-key-oc_"] .stButton>button[kind="secondary"]:active{
  background:#d5eaf1 !important;transform:translateY(0) !important;
  box-shadow:inset 0 1px 2px rgba(23,52,83,.14) !important;}
div[class*="st-key-oc_"] .stButton>button[kind="secondary"]:focus-visible{
  outline:3px solid #4a8ba5 !important;outline-offset:2px !important;}

/* Case File neutral empty states (alert with no human review on file). No verdict,
   no fabricated review — just parity with the panels that would otherwise appear. */
.gate-empty{border-left-color:#cdd6de !important;}
.gate-empty .gate-title{color:#5a6570 !important;font-weight:700 !important;}
.case-empty{padding:14px 16px;color:#5a6570;font-size:14px;line-height:1.5;}
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
  <h1 class="id-bar-logo">Lumen <span>Verify</span></h1>
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

rs_col1, rs_col2 = st.columns([9, 1])
with rs_col1:
    st.markdown(
        f'<div class="role-bar">Demo mode — viewing as '
        f'<b>{st.session_state.view_as}</b>. Switch role to access manager '
        f'functions.</div>',
        unsafe_allow_html=True,
    )
with rs_col2:
    if st.button(
        "→ Analyst" if st.session_state.view_as == "Manager" else "→ Manager",
        use_container_width=True,
    ):
        st.session_state.view_as = (
            "Analyst" if st.session_state.view_as == "Manager" else "Manager"
        )
        st.rerun()

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

    def _claim_card(cl):
        cls = "pass" if cl["result"] == "PASS" else "fail" if cl["result"] == "FAIL" else "review"
        badge = f'v-{cls}'
        return (
            f'<div class="claim-card {cls} hero-claim">'
            f'<div class="claim-line"><span class="claim-tag">① AI Claim</span>'
            f'<span class="claim-val">{cl["type"]} = {cl["asserted_value"]}</span></div>'
            f'<div class="claim-line"><span class="claim-tag">② Source Evidence</span>'
            f'<span class="claim-val">{cl["note"]}</span></div>'
            f'<div class="claim-result-line"><span class="claim-tag">③ Verdict</span>'
            f'<span class="{badge} hero-verdict">{cl["result"]}</span></div>'
            f'</div>'
        )

    claim_rows = "".join(_claim_card(cl) for cl in case["ai_claims"]) \
        or '<div class="field-desc-txt">No AI claims drafted for this alert.</div>'

    st.markdown(f"""
    <div class="case-panel" style="margin-top:12px;">
      <div class="case-panel-hdr"><span class="case-panel-title">AI Claim Verification</span></div>
      <div style="padding:14px 16px;background:#f5f5f5;">{claim_rows}</div>
    </div>
    """, unsafe_allow_html=True)

    if case["missing"]:
        st.markdown(
            f'<div class="warn-box">Missing evidence: {", ".join(case["missing"])}</div>',
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
          <div class="field-row"><span class="field-lbl">Case Readiness</span><span class="field-val">{case['readiness']}%</span></div>
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

    # CORRECTION 3 — make the verification → human-outcome relationship explicit.
    # Computed from existing case values only; nothing saved is read or altered.
    _fails = [cl for cl in case["ai_claims"] if cl["result"] == "FAIL"]
    if _fails:
        _n = len(_fails)
        _rv = case["review"]
        _disp = str(_rv.get("draft_disposition", "")).lower() if _rv else ""
        if _rv and _disp in ("edited", "rejected"):
            _cls, _msg = "ok", (
                f"<b>{_n} AI claim(s) failed verification.</b> The human reviewer "
                f"({_rv.get('reviewer', '—')}) did not accept the AI draft as-is "
                f"(disposition: <b>{_rv.get('draft_disposition')}</b>). The unsupported "
                f"claim did not silently become the final decision."
            )
        elif _rv and _disp == "accepted":
            _cls, _msg = "warn", (
                f"<b>{_n} AI claim(s) failed verification, yet the recorded disposition "
                f"is 'accepted'.</b> Confirm the human sign-off is complete before this "
                f"stands as the final decision."
            )
        else:
            _cls, _msg = "warn", (
                f"<b>{_n} AI claim(s) failed verification.</b> No completed human review "
                f"is on file — the unsupported claim must not be accepted without human "
                f"sign-off."
            )
        st.markdown(f'<div class="outcome-link {_cls}">{_msg}</div>', unsafe_allow_html=True)

    if case["review"]:
        rv = case["review"]
        st.markdown(f"""
        <div class="case-panel" style="margin-top:12px;">
          <div class="case-panel-hdr"><span class="case-panel-title">Human Review</span></div>
          <div style="padding:14px 16px;">
            <div class="review-meta">
              <div><span class="review-lbl">Reviewer</span><span class="review-val">{rv.get('reviewer','—')}</span></div>
              <div><span class="review-lbl">Disposition</span><span class="review-val">{rv.get('draft_disposition','—')}</span></div>
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
                '<div class="gate-panel gate-passed">'
                '<div class="gate-title">HUMAN-REVIEW GATE: PASSED</div>'
                '<div class="gate-body">All required review fields are present. '
                f'Final disposition accepted: <b>{_gate.disposition}</b>.</div>'
                '<div class="gate-foot">Enforcement: enabled</div></div>'
            )
        st.markdown(_gate_html, unsafe_allow_html=True)

    else:
        # No human review exists for this alert. Render neutral empty states in the
        # same positions the Human Review and Human-Review Gate panels occupy when a
        # review IS on file — additive consistency only. No fabricated review, no
        # disposition, no PASS/FAIL/BLOCKED verdict, and NO audit event (audit belongs
        # to a real pipeline execution, never to a Case File render).
        st.markdown("""
        <div class="case-panel" style="margin-top:12px;">
          <div class="case-panel-hdr"><span class="case-panel-title">Human Review</span></div>
          <div class="case-empty">No human review recorded for this alert.</div>
        </div>
        """, unsafe_allow_html=True)
        st.markdown(
            '<div class="gate-panel gate-empty">'
            '<div class="gate-title">HUMAN-REVIEW GATE</div>'
            '<div class="gate-body">Not evaluated because no human review exists.</div>'
            '</div>',
            unsafe_allow_html=True,
        )

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
        f'<h2 class="section-h">Active Alerts <span class="section-count">({total})</span></h2>',
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
    STA_STYLE = {
        "Pending Review": "background:#e8eeff;color:#1a2e8c;border-color:#99a;",
        "In Progress":    "background:#e8f5e8;color:#1a5c1a;border-color:#9c9;",
        "Closed":         "background:#f0f0f0;color:#555;border-color:#bbb;",
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
            status_filter = st.segmented_control(
                "Filter by status",
                ["Pending Review", "In Progress", "Closed"],
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
            "background:#e7eef1;box-shadow:inset 3px 0 0 #2e728f;" if is_sel else
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

        ai_html = (
            '<span style="color:#1a5c1a;font-size:12px;font-weight:700;">&#9679; AI</span>'
            if r["ai"]
            else '<span style="color:#ccc;font-size:12px;">&#8212;</span>'
        )

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
            + _cell(aid, "font-weight:700;color:#1a5276;font-size:13px;")
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
  .lv-table thead tr {{ background:#2e728f; }}
  .lv-table th {{ padding:11px 14px; text-align:left; font-size:13px; font-weight:700;
       color:#fff; border-right:1px solid #4a8ba5; letter-spacing:.03em;
       border-bottom:none; white-space:nowrap; }}
  .lv-table th:last-child {{ border-right:none; }}
  .lv-table td {{ padding:0; border-bottom:1px solid #e6e6e6;
       border-right:1px solid #efefef; color:#1a1a1a; vertical-align:middle; background:#fff; }}
  .lv-table td:last-child {{ border-right:none; }}
  .lv-table td a {{ color:inherit; }}
  .lv-table td a:focus-visible {{ outline:2px solid #2e728f; outline-offset:-2px; }}
  .lv-table tbody tr:nth-child(even) td {{ background:#f7f8f9; }}
  .lv-table tbody tr:nth-child(odd) td {{ background:#fff; }}
  .lv-table tbody tr:hover td {{ background:#eef3f5 !important; cursor:pointer; }}
  .lv-open {{ color:#2e728f; font-weight:700; font-size:13px; white-space:nowrap; }}
  .lv-table tbody tr:hover .lv-open {{ text-decoration:underline; }}
</style>
<table class="lv-table">
  <caption style="position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);">Alert queue — click a row to open its case file</caption>
  <thead><tr>
    <th scope="col">Alert ID</th><th scope="col">Customer</th><th scope="col">Rule Triggered</th>
    <th scope="col">Severity</th><th scope="col">Case Readiness</th><th scope="col">AI</th>
    <th scope="col">Status</th><th scope="col">Analyst</th><th scope="col">Action</th>
  </tr></thead>
  <tbody>{rows_html}</tbody>
</table>"""

    st.markdown(table_html, unsafe_allow_html=True)

    if st.session_state.open_case:
        _aid = st.session_state.open_case
        st.session_state.open_case = None
        show_case_dialog(_aid, source)

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
                    <span class="ov-card-id">{row['change_id']}</span>
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
                        # Case File dialog (reuses the tab-1 open_case trigger). Only this
                        # control is interactive — the card itself is not clickable.
                        st.session_state.open_case = _target
                        st.rerun()
                    if st.button("✓ Approve", key=f"apr_{row['change_id']}", type="primary", width="content"):
                        update_override_status(
                            row["change_id"], "approved",
                            st.session_state.current_user["name"],
                        )
                        st.success("Approved — change is now live.")
                        st.rerun()
                    if st.button("✕ Reject", key=f"rej_{row['change_id']}", type="secondary", width="content"):
                        update_override_status(
                            row["change_id"], "rejected",
                            st.session_state.current_user["name"],
                        )
                        st.warning("Rejected.")
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
                f'<td style="white-space:nowrap;font-weight:700;color:#1a5276;">{_hesc(r["change_id"])}</td>'
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
                    f'<table class="log-tbl">'
                    f'<thead><tr><th>Request ID</th><th>Alert</th><th>Field</th>'
                    f'<th>Change</th><th>Submitted By</th><th>Status</th>'
                    f'<th>Reviewed By</th><th>Reviewed At</th></tr></thead>'
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
            st.markdown('<div class="settings-section-title">Case Readiness Gates</div>', unsafe_allow_html=True)
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
