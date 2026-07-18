"""Generate the synthetic AML dataset, one CSV per table, deterministically.

Run:  python scripts/generate_data.py

The data is DESIGNED, not random noise. A fixed random seed makes the output
byte-for-byte reproducible. Designed "hero" cases are planted for the demo and
are clearly marked with HERO CASE comments below. Filler rows are generated
around them with the seed so the dataset looks realistic without burying the
hero cases.

Reference date is hardcoded (not datetime.now) so output does not change with
the calendar. All relative ages (stale KYC, dormancy) are computed from it.
"""

from __future__ import annotations

import json
import random
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

SEED = 42
REF_DATE = date(2026, 5, 29)  # "today" for this dataset, fixed for reproducibility.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

HIGH_RISK_COUNTRIES = ["IR", "KP", "SY", "MM", "CU", "VE", "RU"]  # used by high_risk_country typology
NORMAL_COUNTRIES = ["US", "GB", "CA", "DE", "FR", "JP", "AU"]
OCCUPATIONS = ["Teacher", "Engineer", "Nurse", "Retired", "Accountant", "Driver", "Consultant"]

# Display names for the filler customers CUST0009..CUST0030, in that id order. These
# replace the earlier generic "Customer NN" placeholders with plausible, ordinary,
# fictional individuals — no public figures, no duplicates, and distinct from both the
# designed hero customers and the employee names used elsewhere. Assigning a name does
# not consume the RNG, so every other generated field stays byte-for-byte identical.
# Every filler profile is an individual (the dataset's only business is the designed
# CUST0005, Eastgate Trading LLC), so each entry here is an individual name.
FILLER_NAMES = [
    "Emi Ishikawa", "Bradley Coleman", "Haruto Sato", "Margaret Dawson",
    "Nathan Cormier", "Stefan Vogel", "Ingrid Baumann", "Colin Hartley",
    "Owen Tremblay", "Yuki Nakamura", "Barbara Hollis", "Camille Laurent",
    "Lukas Weber", "Chloe Beaumont", "Derek Olson", "Vanessa Pruitt",
    "Kenji Okada", "Alan Pemberton", "Sylvie Marchand", "Ren Kobayashi",
    "Gregory Fuentes", "Ethan Callahan",
]


def iso(dt: datetime | date) -> str:
    return dt.isoformat()


def build_dataset() -> dict[str, pd.DataFrame]:
    rng = random.Random(SEED)

    customers: list[dict] = []
    transactions: list[dict] = []
    alerts: list[dict] = []
    evidence_items: list[dict] = []
    prior_cases: list[dict] = []
    kyc_profile_status: list[dict] = []
    ai_outputs: list[dict] = []
    human_reviews: list[dict] = []

    # Counters for stable IDs.
    txn_seq = 0
    alert_seq = 0
    pc_seq = 0
    ev_seq = 0
    out_seq = 0
    rev_seq = 0

    def next_txn() -> str:
        nonlocal txn_seq
        txn_seq += 1
        return f"TXN{txn_seq:05d}"

    def next_alert() -> str:
        nonlocal alert_seq
        alert_seq += 1
        return f"ALERT{alert_seq:03d}"

    def next_pc() -> str:
        nonlocal pc_seq
        pc_seq += 1
        return f"PC{pc_seq:04d}"

    def next_ev() -> str:
        nonlocal ev_seq
        ev_seq += 1
        return f"EV{ev_seq:04d}"

    def next_out() -> str:
        nonlocal out_seq
        out_seq += 1
        return f"OUT{out_seq:03d}"

    def next_rev() -> str:
        nonlocal rev_seq
        rev_seq += 1
        return f"REV{rev_seq:03d}"

    def add_evidence(alert_id: str, items: list[tuple[str, bool]], checked: datetime) -> None:
        for item_type, available in items:
            evidence_items.append(
                {
                    "alert_id": alert_id,
                    "item_type": item_type,
                    "available": available,
                    "last_checked": iso(checked),
                }
            )

    # ----------------------------------------------------------------------
    # DESIGNED CUSTOMERS (CUST0001 to CUST0008). Each anchors a typology or
    # hero case. Filler customers CUST0009+ are generated afterward.
    # ----------------------------------------------------------------------

    # ======================================================================
    # HERO CASE A (the catch): AI gets caught lying about SAR history.
    # Customer has prior_sar_count = 0, but the AI draft for this alert
    # asserts prior_sar_history = true. The verifier (once built) will FAIL
    # this claim against prior_cases. This is the flagship "AI gets caught" demo.
    # ======================================================================
    customers.append(
        {
            "customer_id": "CUST0001",
            "name": "Dana Whitfield",
            "kyc_status": "verified",
            "expected_monthly_volume": 12000.0,
            "country": "US",
            "occupation": "Consultant",
            "kyc_last_updated": iso(date(2025, 9, 1)),
        }
    )
    # Ground truth: zero prior SARs.
    prior_cases.append(
        {
            "case_id": next_pc(),
            "customer_id": "CUST0001",
            "prior_sar_count": 0,
            "last_sar_date": "",
        }
    )
    a_hero = next_alert()
    alerts.append(
        {
            "alert_id": a_hero,
            "customer_id": "CUST0001",
            "rule_triggered": "R-LARGE-WIRE",
            "severity": "high",
            "triggered_at": iso(datetime(2026, 5, 20, 9, 15)),
            "status": "in_review",
        }
    )
    transactions.append(
        {
            "txn_id": next_txn(),
            "customer_id": "CUST0001",
            "amount": 28000.0,
            "direction": "in",
            "counterparty_country": "GB",
            "timestamp": iso(datetime(2026, 5, 20, 9, 10)),
        }
    )
    add_evidence(a_hero, [("id_document", True), ("source_of_funds", True), ("sar_history", True)], datetime(2026, 5, 21, 10, 0))
    # The AI claim that contradicts prior_cases (asserted true, truth is 0 SARs).
    ai_outputs.append(
        {
            "output_id": next_out(),
            "alert_id": a_hero,
            "claim_id": "CLM001",
            "claim_type": "prior_sar_history",
            "asserted_value": "true",
            "evidence_refs": json.dumps(["prior_cases.customer_id=CUST0001"]),
            "generated_at": iso(datetime(2026, 5, 21, 14, 0)),
        }
    )

    # ----------------------------------------------------------------------
    # TYPOLOGY: expected_activity_mismatch. University student, declared
    # expected_monthly_volume 2000, receives a single 45000 wire.
    # ----------------------------------------------------------------------
    customers.append(
        {
            "customer_id": "CUST0002",
            "name": "Marcus Reed",
            "kyc_status": "verified",
            "expected_monthly_volume": 2000.0,
            "country": "US",
            "occupation": "Student",
            "kyc_last_updated": iso(date(2025, 8, 15)),
        }
    )
    prior_cases.append({"case_id": next_pc(), "customer_id": "CUST0002", "prior_sar_count": 0, "last_sar_date": ""})
    a_student = next_alert()
    alerts.append(
        {
            "alert_id": a_student,
            "customer_id": "CUST0002",
            "rule_triggered": "R-VOLUME-SPIKE",
            "severity": "high",
            "triggered_at": iso(datetime(2026, 5, 18, 11, 0)),
            "status": "open",
        }
    )
    transactions.append(
        {
            "txn_id": next_txn(),
            "customer_id": "CUST0002",
            "amount": 45000.0,
            "direction": "in",
            "counterparty_country": "AE",
            "timestamp": iso(datetime(2026, 5, 18, 10, 55)),
        }
    )
    add_evidence(a_student, [("id_document", True), ("source_of_funds", False)], datetime(2026, 5, 19, 9, 0))
    ai_outputs.append(
        {
            "output_id": next_out(),
            "alert_id": a_student,
            "claim_id": "CLM002",
            "claim_type": "expected_activity_mismatch",
            "asserted_value": "inflow 45000.00 vs expected_monthly_volume 2000.00",
            "evidence_refs": json.dumps(
                ["customers.customer_id=CUST0002", "transactions.txn_id=TXN00002"]
            ),
            "generated_at": iso(datetime(2026, 5, 19, 13, 0)),
        }
    )

    # ----------------------------------------------------------------------
    # TYPOLOGY: rapid_movement. Four same-day, same-amount movements.
    # ----------------------------------------------------------------------
    customers.append(
        {
            "customer_id": "CUST0003",
            "name": "Priya Nair",
            "kyc_status": "verified",
            "expected_monthly_volume": 15000.0,
            "country": "GB",
            "occupation": "Accountant",
            "kyc_last_updated": iso(date(2025, 11, 2)),
        }
    )
    prior_cases.append({"case_id": next_pc(), "customer_id": "CUST0003", "prior_sar_count": 0, "last_sar_date": ""})
    a_rapid = next_alert()
    alerts.append(
        {
            "alert_id": a_rapid,
            "customer_id": "CUST0003",
            "rule_triggered": "R-RAPID-MOVEMENT",
            "severity": "med",
            "triggered_at": iso(datetime(2026, 5, 22, 17, 0)),
            "status": "open",
        }
    )
    for hour in (9, 11, 13, 15):
        transactions.append(
            {
                "txn_id": next_txn(),
                "customer_id": "CUST0003",
                "amount": 9000.0,  # same amount, same day
                "direction": "out" if hour in (11, 15) else "in",
                "counterparty_country": "GB",
                "timestamp": iso(datetime(2026, 5, 22, hour, 0)),
            }
        )
    add_evidence(a_rapid, [("transaction_history", True), ("source_of_funds", True)], datetime(2026, 5, 23, 9, 0))
    ai_outputs.append(
        {
            "output_id": next_out(),
            "alert_id": a_rapid,
            "claim_id": "CLM003",
            "claim_type": "rapid_movement",
            "asserted_value": "4 same-amount transactions within one day",
            "evidence_refs": json.dumps(["transactions.customer_id=CUST0003"]),
            "generated_at": iso(datetime(2026, 5, 23, 10, 30)),
        }
    )

    # ----------------------------------------------------------------------
    # TYPOLOGY: structuring. Multiple cash deposits just under 10000 across
    # three days, designed to dodge the 10000 reporting threshold.
    # ----------------------------------------------------------------------
    customers.append(
        {
            "customer_id": "CUST0004",
            "name": "Tomas Herrera",
            "kyc_status": "verified",
            "expected_monthly_volume": 8000.0,
            "country": "US",
            "occupation": "Driver",
            "kyc_last_updated": iso(date(2025, 7, 10)),
        }
    )
    prior_cases.append({"case_id": next_pc(), "customer_id": "CUST0004", "prior_sar_count": 0, "last_sar_date": ""})
    a_struct = next_alert()
    alerts.append(
        {
            "alert_id": a_struct,
            "customer_id": "CUST0004",
            "rule_triggered": "R-STRUCTURING",
            "severity": "high",
            "triggered_at": iso(datetime(2026, 5, 16, 16, 0)),
            "status": "in_review",
        }
    )
    for day, amt in ((14, 9500.0), (15, 9200.0), (15, 9700.0), (16, 9400.0)):
        transactions.append(
            {
                "txn_id": next_txn(),
                "customer_id": "CUST0004",
                "amount": amt,  # each just under the 10000 threshold
                "direction": "in",
                "counterparty_country": "US",
                "timestamp": iso(datetime(2026, 5, day, 12, 0)),
            }
        )
    add_evidence(a_struct, [("transaction_history", True), ("source_of_funds", False)], datetime(2026, 5, 17, 9, 0))
    ai_outputs.append(
        {
            "output_id": next_out(),
            "alert_id": a_struct,
            "claim_id": "CLM004",
            "claim_type": "structuring",
            "asserted_value": "4 deposits under 10000 across 3 days",
            "evidence_refs": json.dumps(["transactions.customer_id=CUST0004"]),
            "generated_at": iso(datetime(2026, 5, 17, 11, 0)),
        }
    )

    # ----------------------------------------------------------------------
    # TYPOLOGY: high_risk_country + unusual_transaction_volume. Dormant
    # business account wakes up with a large transfer to a high-risk country.
    # ----------------------------------------------------------------------
    customers.append(
        {
            "customer_id": "CUST0005",
            "name": "Eastgate Trading LLC",
            "kyc_status": "verified",
            "expected_monthly_volume": 5000.0,
            "country": "US",
            "occupation": "Import/Export Business",
            "kyc_last_updated": iso(date(2025, 3, 20)),
        }
    )
    prior_cases.append({"case_id": next_pc(), "customer_id": "CUST0005", "prior_sar_count": 0, "last_sar_date": ""})
    # Long dormancy: a single tiny transaction over a year ago, then nothing.
    transactions.append(
        {
            "txn_id": next_txn(),
            "customer_id": "CUST0005",
            "amount": 120.0,
            "direction": "out",
            "counterparty_country": "US",
            "timestamp": iso(datetime(2025, 1, 10, 9, 0)),
        }
    )
    a_dormant = next_alert()
    alerts.append(
        {
            "alert_id": a_dormant,
            "customer_id": "CUST0005",
            "rule_triggered": "R-DORMANT-WAKE",
            "severity": "high",
            "triggered_at": iso(datetime(2026, 5, 25, 8, 30)),
            "status": "open",
        }
    )
    # The wake-up transfer: large, to a high-risk country.
    transactions.append(
        {
            "txn_id": next_txn(),
            "customer_id": "CUST0005",
            "amount": 60000.0,
            "direction": "out",
            "counterparty_country": "IR",
            "timestamp": iso(datetime(2026, 5, 25, 8, 25)),
        }
    )
    add_evidence(a_dormant, [("transaction_history", True), ("counterparty_screening", True)], datetime(2026, 5, 26, 9, 0))
    ai_outputs.append(
        {
            "output_id": next_out(),
            "alert_id": a_dormant,
            "claim_id": "CLM005",
            "claim_type": "high_risk_country",
            "asserted_value": "IR",
            "evidence_refs": json.dumps(["transactions.counterparty_country=IR"]),
            "generated_at": iso(datetime(2026, 5, 26, 10, 0)),
        }
    )
    ai_outputs.append(
        {
            "output_id": next_out(),
            "alert_id": a_dormant,
            "claim_id": "CLM006",
            "claim_type": "unusual_transaction_volume",
            "asserted_value": "60000.00 outflow in 1 day",
            "evidence_refs": json.dumps(
                ["customers.customer_id=CUST0005", "transactions.customer_id=CUST0005"]
            ),
            "generated_at": iso(datetime(2026, 5, 26, 10, 0)),
        }
    )

    # ----------------------------------------------------------------------
    # TYPOLOGY: stale_kyc_profile (KYC drift). KYC last refreshed more than
    # four years ago. kyc_status expired, current_within_12mo False.
    # ----------------------------------------------------------------------
    stale_date = date(2021, 12, 1)  # > 4 years before REF_DATE
    customers.append(
        {
            "customer_id": "CUST0006",
            "name": "Helen Voss",
            "kyc_status": "expired",
            "expected_monthly_volume": 6000.0,
            "country": "DE",
            "occupation": "Retired",
            "kyc_last_updated": iso(stale_date),
        }
    )
    prior_cases.append({"case_id": next_pc(), "customer_id": "CUST0006", "prior_sar_count": 0, "last_sar_date": ""})
    a_stale = next_alert()
    alerts.append(
        {
            "alert_id": a_stale,
            "customer_id": "CUST0006",
            "rule_triggered": "R-KYC-DRIFT",
            "severity": "low",
            "triggered_at": iso(datetime(2026, 5, 10, 14, 0)),
            "status": "open",
        }
    )
    transactions.append(
        {
            "txn_id": next_txn(),
            "customer_id": "CUST0006",
            "amount": 3000.0,
            "direction": "in",
            "counterparty_country": "DE",
            "timestamp": iso(datetime(2026, 5, 9, 10, 0)),
        }
    )
    add_evidence(a_stale, [("id_document", True), ("kyc_refresh", False)], datetime(2026, 5, 11, 9, 0))
    ai_outputs.append(
        {
            "output_id": next_out(),
            "alert_id": a_stale,
            "claim_id": "CLM007",
            "claim_type": "stale_kyc_profile",
            "asserted_value": "true",
            "evidence_refs": json.dumps([]),  # field-based: no evidence_refs required
            "generated_at": iso(datetime(2026, 5, 11, 13, 0)),
        }
    )
    # An extra field-based claim, missing_kyc_data, on the same customer.
    ai_outputs.append(
        {
            "output_id": next_out(),
            "alert_id": a_stale,
            "claim_id": "CLM008",
            "claim_type": "missing_kyc_data",
            "asserted_value": "true",
            "evidence_refs": json.dumps([]),
            "generated_at": iso(datetime(2026, 5, 11, 13, 5)),
        }
    )

    # ======================================================================
    # HERO CASE B (rubber-stamp gate): a human review with required fields
    # MISSING. No decision_reason, no final_note, evidence_reviewed False, yet
    # the draft was accepted. This is the "human rubber-stamped it" demo: the
    # approval gate (built later) must block a disposition like this one.
    # ======================================================================
    customers.append(
        {
            "customer_id": "CUST0007",
            "name": "Roland Beck",
            "kyc_status": "verified",
            "expected_monthly_volume": 10000.0,
            "country": "CA",
            "occupation": "Consultant",
            "kyc_last_updated": iso(date(2025, 10, 5)),
        }
    )
    prior_cases.append(
        {
            "case_id": next_pc(),
            "customer_id": "CUST0007",
            "prior_sar_count": 2,  # a genuine prior history, contrast to HERO A
            "last_sar_date": iso(date(2024, 6, 12)),
        }
    )
    a_rubber = next_alert()
    alerts.append(
        {
            "alert_id": a_rubber,
            "customer_id": "CUST0007",
            "rule_triggered": "R-LARGE-WIRE",
            "severity": "med",
            "triggered_at": iso(datetime(2026, 5, 12, 10, 0)),
            "status": "closed",
        }
    )
    transactions.append(
        {
            "txn_id": next_txn(),
            "customer_id": "CUST0007",
            "amount": 22000.0,
            "direction": "in",
            "counterparty_country": "CA",
            "timestamp": iso(datetime(2026, 5, 12, 9, 50)),
        }
    )
    add_evidence(a_rubber, [("id_document", True), ("source_of_funds", True)], datetime(2026, 5, 12, 12, 0))
    ai_outputs.append(
        {
            "output_id": next_out(),
            "alert_id": a_rubber,
            "claim_id": "CLM009",
            "claim_type": "prior_alert_history",
            "asserted_value": "true",
            "evidence_refs": json.dumps(["alerts.customer_id=CUST0007"]),
            "generated_at": iso(datetime(2026, 5, 12, 13, 0)),
        }
    )
    # The rubber-stamp review: accepted with NO decision_reason and NO final_note.
    human_reviews.append(
        {
            "review_id": next_rev(),
            "alert_id": a_rubber,
            "reviewer": "reviewer:jdoe",
            "evidence_reviewed": False,
            "draft_disposition": "accepted",
            "decision_reason": "",  # MISSING (HERO CASE B)
            "final_note": "",  # MISSING (HERO CASE B)
            "final_action": "",
            "reviewed_at": iso(datetime(2026, 5, 12, 13, 5)),
        }
    )

    # A contrasting GOOD review on the structuring alert, all fields present.
    human_reviews.append(
        {
            "review_id": next_rev(),
            "alert_id": a_struct,
            "reviewer": "reviewer:asmith",
            "evidence_reviewed": True,
            "draft_disposition": "edited",
            "decision_reason": "Confirmed four sub-threshold deposits over three days; pattern consistent with structuring.",
            "final_note": "Escalating to investigations for SAR consideration.",
            "final_action": "escalate",
            "reviewed_at": iso(datetime(2026, 5, 17, 15, 0)),
        }
    )

    # One more designed customer with a benign profile (CUST0008), no alert.
    customers.append(
        {
            "customer_id": "CUST0008",
            "name": "Grace Lin",
            "kyc_status": "verified",
            "expected_monthly_volume": 4000.0,
            "country": "AU",
            "occupation": "Nurse",
            "kyc_last_updated": iso(date(2026, 1, 20)),
        }
    )
    prior_cases.append({"case_id": next_pc(), "customer_id": "CUST0008", "prior_sar_count": 0, "last_sar_date": ""})

    # 10th AI output: a contrast prior_sar_history claim that is TRUE (CUST0007
    # genuinely has prior_sar_count = 2). Lets the demo show a PASS as well.
    ai_outputs.append(
        {
            "output_id": next_out(),
            "alert_id": a_rubber,
            "claim_id": "CLM010",
            "claim_type": "prior_sar_history",
            "asserted_value": "true",
            "evidence_refs": json.dumps(["prior_cases.customer_id=CUST0007"]),
            "generated_at": iso(datetime(2026, 5, 12, 13, 1)),
        }
    )

    # ----------------------------------------------------------------------
    # FILLER CUSTOMERS CUST0009 to CUST0030, plus their transactions, KYC
    # currency rows, and a scattering of lower-severity alerts. All seeded.
    # ----------------------------------------------------------------------
    designed_count = len(customers)
    for i in range(designed_count + 1, 31):
        cid = f"CUST{i:04d}"
        kyc_age_days = rng.randint(30, 700)
        kyc_dt = REF_DATE - timedelta(days=kyc_age_days)
        customers.append(
            {
                "customer_id": cid,
                "name": FILLER_NAMES[i - (designed_count + 1)],
                "kyc_status": rng.choice(["verified", "verified", "pending"]),
                "expected_monthly_volume": float(rng.choice([3000, 5000, 8000, 12000, 20000])),
                "country": rng.choice(NORMAL_COUNTRIES),
                "occupation": rng.choice(OCCUPATIONS),
                "kyc_last_updated": iso(kyc_dt),
            }
        )
        prior_cases.append(
            {"case_id": next_pc(), "customer_id": cid, "prior_sar_count": 0, "last_sar_date": ""}
        )
        # 5 to 11 ordinary transactions each.
        for _ in range(rng.randint(5, 11)):
            day = rng.randint(1, 27)
            transactions.append(
                {
                    "txn_id": next_txn(),
                    "customer_id": cid,
                    "amount": float(rng.randint(50, 6000)),
                    "direction": rng.choice(["in", "out"]),
                    "counterparty_country": rng.choice(NORMAL_COUNTRIES),
                    "timestamp": iso(datetime(2026, 5, day, rng.randint(8, 18), rng.randint(0, 59))),
                }
            )
        # Most filler customers get one or two routine low/med alerts, so the
        # total alert count lands in the 30 to 50 target range.
        for _ in range(rng.randint(1, 2) if rng.random() < 0.9 else 0):
            aid = next_alert()
            alerts.append(
                {
                    "alert_id": aid,
                    "customer_id": cid,
                    "rule_triggered": rng.choice(["R-THRESHOLD", "R-VELOCITY", "R-WATCHLIST-NEARMATCH"]),
                    "severity": rng.choice(["low", "med"]),
                    "triggered_at": iso(datetime(2026, 5, rng.randint(1, 27), rng.randint(8, 18), 0)),
                    "status": rng.choice(["open", "closed"]),
                }
            )
            add_evidence(aid, [("id_document", True), ("transaction_history", rng.choice([True, False]))], datetime(2026, 5, 28, 9, 0))

    # KYC currency flags, one row per customer. current_within_12mo is derived
    # from kyc_last_updated relative to REF_DATE.
    for c in customers:
        last = date.fromisoformat(c["kyc_last_updated"])
        within = (REF_DATE - last).days <= 365
        kyc_profile_status.append(
            {
                "customer_id": c["customer_id"],
                "current_within_12mo": within,
                "last_updated": c["kyc_last_updated"],
            }
        )

    # ======================================================================
    # WEEK 5 DEMO ENRICHMENT (Hero Case A). Extra supporting data so ALERT001
    # renders as a fuller case in the UI. Appended last so every previously
    # assigned txn_id and review_id stays stable. None of this changes the
    # planted contradiction: prior_sar_count for CUST0001 stays 0, so
    # verify_prior_sar_history still returns FAIL for the AI claim above.
    # ======================================================================
    transactions.append(
        {"txn_id": next_txn(), "customer_id": "CUST0001", "amount": 4200.0, "direction": "in",
         "counterparty_country": "US", "timestamp": iso(datetime(2026, 5, 12, 10, 30))}
    )
    transactions.append(
        {"txn_id": next_txn(), "customer_id": "CUST0001", "amount": 15000.0, "direction": "in",
         "counterparty_country": "GB", "timestamp": iso(datetime(2026, 5, 18, 14, 5))}
    )
    transactions.append(
        {"txn_id": next_txn(), "customer_id": "CUST0001", "amount": 6300.0, "direction": "out",
         "counterparty_country": "US", "timestamp": iso(datetime(2026, 5, 20, 16, 45))}
    )
    add_evidence(
        "ALERT001",
        [("transaction_history", True), ("counterparty_screening", True),
         ("source_of_funds_corroboration", False)],
        datetime(2026, 5, 21, 11, 0),
    )
    # A completed human review on ALERT001 that catches the AI overreach. Added
    # after the existing reviews so it is REV003, leaving REV001 (Hero B) intact.
    human_reviews.append(
        {
            "review_id": next_rev(),
            "alert_id": "ALERT001",
            "reviewer": "reviewer:mchen",
            "evidence_reviewed": True,
            "draft_disposition": "edited",
            "decision_reason": "AI asserted prior SAR history, but prior_cases shows zero SARs for this customer. Claim rejected as unsupported.",
            "final_note": "Large inbound wire reviewed; no prior SAR on record. Enhanced monitoring continued, no SAR filed at this time.",
            "final_action": "monitor",
            "reviewed_at": iso(datetime(2026, 5, 22, 9, 30)),
        }
    )

    # ======================================================================
    # WEEK 5 DEMO: case readiness distribution.
    # app.py computes readiness live as (available evidence / total evidence)
    # per alert (case_readiness_pct). We do NOT store a readiness score, which
    # would duplicate and drift from that live formula. Instead we shape each
    # alert's evidence completeness so the COMPUTED distribution is:
    #   - 1 fully-ready positive control at 100%: ALERT004 (Hero Moment 3)
    #   - 1 readiness-blocked demo case at ~50%: ALERT006
    #   - 2 below-threshold alerts at 60-70%: ALERT002 (60%), ALERT005 (70%)
    #   - every other alert at 88-97% (Lumen pre-assembled, demo-ready)
    #   - overall average lands at about 91%
    # This pass rebuilds evidence_items so the readiness math is exact and
    # deterministic. It supersedes the piecemeal evidence added above.
    # ======================================================================
    READINESS_ROSTER = [
        "id_document", "proof_of_address", "source_of_funds", "kyc_refresh",
        "transaction_history", "counterparty_screening", "sanctions_screening",
        "adverse_media_check", "beneficial_ownership", "expected_activity_profile",
        "pep_screening", "account_opening_docs", "wire_details",
        "customer_correspondence", "risk_assessment_memo", "edd_review",
    ]
    # ALERT004 is the fully-ready positive control for Hero Moment 3.
    FULLY_READY = {"ALERT004": (10, 10)}                            # 100%
    # ALERT006 is the readiness-blocked demo case at 50%.
    BLOCKED_RATIOS = {"ALERT006": (5, 10)}                          # 50%
    BELOW_RATIOS = {"ALERT002": (6, 10), "ALERT005": (7, 10)}        # 60%, 70%
    READY_RATIOS = [(14, 15), (15, 16)]                             # 93%, 94%
    readiness_checked = datetime(2026, 5, 28, 9, 0)

    rebuilt_evidence: list[dict] = []
    for a_idx, a in enumerate(alerts):
        aid = a["alert_id"]
        if aid in FULLY_READY:
            available, total = FULLY_READY[aid]
        elif aid in BLOCKED_RATIOS:
            available, total = BLOCKED_RATIOS[aid]
        elif aid in BELOW_RATIOS:
            available, total = BELOW_RATIOS[aid]
        else:
            available, total = READY_RATIOS[a_idx % len(READY_RATIOS)]
        for idx in range(total):
            rebuilt_evidence.append(
                {
                    "alert_id": aid,
                    "item_type": READINESS_ROSTER[idx % len(READINESS_ROSTER)],
                    "available": idx < available,
                    "last_checked": iso(readiness_checked),
                }
            )
    evidence_items = rebuilt_evidence

    frames = {
        "customers": pd.DataFrame(customers),
        "transactions": pd.DataFrame(transactions),
        "alerts": pd.DataFrame(alerts),
        "evidence_items": pd.DataFrame(evidence_items),
        "prior_cases": pd.DataFrame(prior_cases),
        "kyc_profile_status": pd.DataFrame(kyc_profile_status),
        "ai_outputs": pd.DataFrame(ai_outputs),
        "human_reviews": pd.DataFrame(human_reviews),
    }
    return frames


PENDING_OVERRIDE_COLUMNS = [
    "change_id", "alert_id", "field_changed", "old_value", "new_value",
    "changed_by_id", "changed_by_name", "changed_at", "reason",
    "status", "reviewed_by", "reviewed_at", "review_note",
]

AUDIT_COLUMNS = ["log_id", "timestamp", "actor", "action", "alert_id", "details_json"]

# Action names the live app actually emits (src/audit.py callers in
# src/llm_drafter.py, src/pipeline.py, and app.py). The seed below uses ONLY
# these, so the historical trail and the live trail share one vocabulary.
LIVE_AUDIT_ACTIONS = frozenset(
    [
        "draft_started", "draft_completed", "claim_dropped", "draft_failed",
        "alert_processing_started", "alert_processing_finished", "claim_verified",
        "field_override", "override_review", "keyword_added", "keyword_removed",
        "risk_settings_saved", "draft_empty",
    ]
)


def build_pending_overrides() -> list[dict]:
    """Deterministic pending override requests for the Manager Review screen.

    All rows are status 'pending' with blank reviewer fields, and every
    alert_id references a real alert in alerts.csv.
    """
    return [
        {
            "change_id": "CHG-SEED-001", "alert_id": "ALERT002",
            "field_changed": "severity", "old_value": "high", "new_value": "med",
            "changed_by_id": "EMP-204", "changed_by_name": "Jordan Avery",
            "changed_at": iso(datetime(2026, 5, 23, 9, 15)),
            "reason": "Customer supplied a scholarship grant letter explaining the 45000 inflow. Recommend downgrading severity pending manager sign-off.",
            "status": "pending", "reviewed_by": "", "reviewed_at": "", "review_note": "",
        },
        {
            "change_id": "CHG-SEED-002", "alert_id": "ALERT005",
            "field_changed": "disposition", "old_value": "open", "new_value": "escalate",
            "changed_by_id": "EMP-217", "changed_by_name": "Priya Raman",
            "changed_at": iso(datetime(2026, 5, 26, 13, 40)),
            "reason": "Dormant business account moved 60000 to a high-risk jurisdiction. Requesting escalation to investigations.",
            "status": "pending", "reviewed_by": "", "reviewed_at": "", "review_note": "",
        },
        {
            "change_id": "CHG-SEED-003", "alert_id": "ALERT001",
            "field_changed": "risk_rating", "old_value": "high", "new_value": "medium",
            "changed_by_id": "EMP-204", "changed_by_name": "Jordan Avery",
            "changed_at": iso(datetime(2026, 5, 22, 10, 5)),
            "reason": "AI prior SAR claim was rejected on review. With that claim removed, residual risk is medium. Manager confirmation requested.",
            "status": "pending", "reviewed_by": "", "reviewed_at": "", "review_note": "",
        },
    ]


def build_audit_seed() -> list[dict]:
    """Deterministic historical audit events for the governance screen.

    Uses ONLY action names in LIVE_AUDIT_ACTIONS. Timestamps precede REF_DATE.
    """
    def row(n, ts, actor, action, alert_id, details):
        return {
            "log_id": f"LOG-SEED-{n:03d}",
            "timestamp": ts,
            "actor": actor,
            "action": action,
            "alert_id": alert_id,
            "details_json": json.dumps(details, sort_keys=True),
        }

    return [
        row(1, iso(datetime(2026, 5, 21, 14, 0, 5)), "llm_drafter", "draft_started", "ALERT001", {}),
        row(2, iso(datetime(2026, 5, 21, 14, 0, 9)), "llm_drafter", "draft_completed", "ALERT001", {"claim_count": 2}),
        row(3, iso(datetime(2026, 5, 21, 14, 5, 0)), "pipeline", "claim_verified", "ALERT001",
            {"claim_type": "prior_sar_history", "status": "FAIL", "reason": "prior_sar_count=0 contradicts asserted prior SAR history"}),
        row(4, iso(datetime(2026, 5, 22, 9, 30, 0)), "ui:EMP-204", "field_override", "ALERT001",
            {"change_id": "CHG-SEED-003", "field": "risk_rating", "old": "high", "new": "medium"}),
        row(5, iso(datetime(2026, 5, 23, 9, 15, 0)), "ui:EMP-204", "field_override", "ALERT002",
            {"change_id": "CHG-SEED-001", "field": "severity", "old": "high", "new": "med"}),
        row(6, iso(datetime(2026, 5, 25, 8, 31, 0)), "llm_drafter", "draft_started", "ALERT005", {}),
        row(7, iso(datetime(2026, 5, 25, 8, 31, 4)), "llm_drafter", "claim_dropped", "ALERT005",
            {"claim_type": "high_risk_country", "reason": "missing_evidence_refs"}),
        row(8, iso(datetime(2026, 5, 25, 8, 31, 5)), "llm_drafter", "draft_completed", "ALERT005", {"claim_count": 1}),
        row(9, iso(datetime(2026, 5, 25, 8, 35, 0)), "pipeline", "claim_verified", "ALERT005",
            {"claim_type": "high_risk_country", "status": "PASS", "reason": "transaction to IR"}),
        row(10, iso(datetime(2026, 5, 26, 13, 40, 0)), "ui:EMP-217", "field_override", "ALERT005",
            {"change_id": "CHG-SEED-002", "field": "disposition", "old": "open", "new": "escalate"}),
        row(11, iso(datetime(2026, 5, 27, 10, 0, 0)), "ui:EMP-300", "risk_settings_saved", "",
            {"changes": {"high_risk_threshold": "10000"}}),
        row(12, iso(datetime(2026, 5, 27, 10, 2, 0)), "ui:EMP-300", "keyword_added", "",
            {"typology": "structuring", "keyword": "smurfing"}),
        row(13, iso(datetime(2026, 5, 27, 10, 3, 0)), "ui:EMP-300", "keyword_removed", "",
            {"typology": "structuring", "keyword": "obsolete_term"}),
        row(14, iso(datetime(2026, 5, 28, 15, 12, 0)), "ui:EMP-108", "override_review", "ALERT007",
            {"change_id": "CHG-HIST-009", "decision": "approved"}),
    ]


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    frames = build_dataset()
    for name, df in frames.items():
        out = DATA_DIR / f"{name}.csv"
        df.to_csv(out, index=False)
        print(f"wrote {out.relative_to(PROJECT_ROOT)}  ({len(df)} rows)")

    # WEEK 5 DEMO SEED: pending overrides for the Manager Review screen.
    overrides_df = pd.DataFrame(build_pending_overrides(), columns=PENDING_OVERRIDE_COLUMNS)
    overrides_path = DATA_DIR / "pending_overrides.csv"
    overrides_df.to_csv(overrides_path, index=False)
    print(f"wrote {overrides_path.relative_to(PROJECT_ROOT)}  ({len(overrides_df)} rows)")

    # WEEK 5 DEMO SEED: historical audit trail so the governance screen is not
    # empty. Seeded events use ONLY action names the live app emits (see
    # LIVE_AUDIT_ACTIONS). audit.log_event appends new events after these, so
    # there is one canonical, coherent trail. This file is tracked (not ignored).
    audit_df = pd.DataFrame(build_audit_seed(), columns=AUDIT_COLUMNS)
    audit_path = DATA_DIR / "audit_log.csv"
    audit_df.to_csv(audit_path, index=False)
    print(f"wrote {audit_path.relative_to(PROJECT_ROOT)}  ({len(audit_df)} seeded rows)")

    print(f"\nDone. Reproducible with SEED={SEED}, REF_DATE={REF_DATE.isoformat()}.")


if __name__ == "__main__":
    main()
