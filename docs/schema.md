# Data schema

The workbench stores everything in nine tables, one CSV per table in `data/`.
The Python models that define these tables live in `src/schema.py` (Pydantic
v2). This document describes each table in plain language and then how they fit
together.

A note on two special column formats:

- `ai_outputs.evidence_refs` and `audit_log.details_json` hold structured data.
  In the CSV they are stored as JSON text. Read them with `json.loads`.
  Each reference in evidence_refs uses the format
  table_name.column=value. Legal table names are: customers, transactions,
  alerts, evidence_items, prior_cases, kyc_profile_status, ai_outputs,
  human_reviews.
- Empty optional fields (for example a review with no `final_note`) are stored
  as empty cells. When loading with pandas, use `keep_default_na=False` if you
  want them to come back as empty strings rather than NaN.

---

## Tables

### customers

The people and businesses under monitoring. One row per customer. Holds
identity and profile facts: name, country, occupation, KYC status, the date KYC
was last updated, and `expected_monthly_volume`, the modeled normal throughput
that several claim types compare actual activity against.

### transactions

Individual money movements. One row per transaction, each tied to a customer.
Records the amount, the direction (`in` or `out`), the counterparty country,
and a timestamp. This is the raw material for the pattern-based claim types
(rapid movement, structuring, volume).

### alerts

Monitoring alerts raised against customers by detection rules. One row per
alert. Records which rule fired, the severity (`high`, `med`, `low`), when it
triggered, and its status (`open`, `in_review`, `closed`). An alert is the unit
of work a reviewer dispositions.

### evidence_items

The pieces of evidence expected for a given alert. One row per item per alert.
Each item has a type (for example `source_of_funds`) and an `available` flag
saying whether it is actually on file. This is what the `missing_kyc_data`
claim is checked against.

### prior_cases

A customer's historical SAR (suspicious activity report) record. One row per
customer. `prior_sar_count` is how many SARs were previously filed. This is the
ground truth that the `prior_sar_history` claim is verified against, and the
record the AI contradicts in HERO CASE A.

### kyc_profile_status

A derived view of whether each customer's KYC is current. One row per customer.
`current_within_12mo` is true when KYC was refreshed in the last year. This is
the field the `stale_kyc_profile` claim is checked against.

### ai_outputs

The structured claims emitted by the AI. One row per claim. Each row names the
alert it belongs to, the claim type (from the closed vocabulary in
`docs/claim_types.md`), the asserted value, and `evidence_refs` pointing at the
source rows the AI says support the claim. From Phase 2 onward these rows are produced by src/llm_drafter.py, which
calls the Anthropic API and enforces the closed vocabulary via both tool
schema and Python validation gates.

### human_reviews

A reviewer's disposition of an alert. One row per review. Records who reviewed
it, whether they actually looked at the evidence, what they did with the AI
draft (`accepted`, `edited`, `rejected`), and the required justification fields
`decision_reason` and `final_note`. A review missing those fields is a rubber
stamp (HERO CASE B).

### audit_log

An append-only trail of actions taken in the workbench. One row per action.
Records who acted, what they did, the related alert, and a JSON details blob.
It is written for real by `src/audit.py` and is regenerated at runtime, so it
is git-ignored.

---

## How the tables relate

`customers` is the hub. Almost everything hangs off `customer_id`:

```
customers (customer_id)
  |
  |-- transactions        (customer_id)   many per customer
  |-- prior_cases         (customer_id)   one per customer
  |-- kyc_profile_status  (customer_id)   one per customer
  |-- alerts              (customer_id)   many per customer
         |
         |-- evidence_items   (alert_id)   many per alert
         |-- ai_outputs       (alert_id)   many claims per alert
         |-- human_reviews    (alert_id)   one disposition per alert
         |-- audit_log        (alert_id)   many log entries per alert
```

In words: a customer has transactions, one prior-case record, one KYC-status
record, and zero or more alerts. Each alert gathers its expected evidence
items, the AI's structured claims, the human's review, and audit entries. The
verifier's job is to take an `ai_outputs` claim and check it against the
relevant source table (the join key is either `customer_id`, reached via the
alert, or the alert's own `evidence_items`).

---

## Hero cases: where they live in the data

| Hero case | Where to look | What it shows |
|-----------|---------------|---------------|
| HERO CASE A (the catch) | `ai_outputs` row CLM001 on ALERT001 (customer CUST0001) asserts `prior_sar_history = true`; `prior_cases` for CUST0001 has `prior_sar_count = 0` | The AI makes a claim the records contradict. The verifier will catch it. |
| HERO CASE B (rubber stamp) | `human_reviews` row REV001 on ALERT007 (customer CUST0007): `draft_disposition = accepted` but `decision_reason` and `final_note` are empty and `evidence_reviewed = false` | A human approved without justifying or reviewing evidence. The anti-rubber-stamp gate (`src/review_gate.py`, invoked by `src/pipeline.py` Step 3) blocks it and fails closed; the Case File shows it BLOCKED using the same shared rule. |
| Contrast (true positive) | `prior_cases` for CUST0007 has `prior_sar_count = 2`; claim CLM010 asserting `prior_sar_history = true` is therefore correct | Shows the verifier should PASS a true claim, not just fail false ones. |
| expected_activity_mismatch | CUST0002 (student), expected 2,000/mo, receives a 45,000 inflow; claim CLM002 | Pattern claim with a real underlying mismatch. |
| rapid_movement | CUST0003, four 9,000 transactions on one day; claim CLM003 | Real rapid in/out pattern. |
| structuring | CUST0004, four sub-10,000 deposits over three days; claim CLM004 | Real structuring pattern. |
| high_risk_country + unusual_transaction_volume | CUST0005 (dormant business) wakes with a 60,000 transfer to IR; claims CLM005, CLM006 | Two claims, both with real support. |
| stale_kyc_profile + missing_kyc_data | CUST0006, KYC last updated 2021-12-01 (over four years), `current_within_12mo = false`; claims CLM007, CLM008 | Field-based KYC-drift claims. |
