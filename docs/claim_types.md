# Claim-type vocabulary

This is the closed list of claims the AI is allowed to make. "Closed" means the
AI may only emit a claim whose type appears on this list. Anything else is
rejected before it reaches a human. There are exactly nine types.

Why a closed list? Because every claim type has a matching, deterministic
verification rule. If the AI could invent new claim types, we could not check
them automatically, and we would be back to a human reading free text and
trusting it. The closed vocabulary is what makes "verify before approve"
possible.

How to read each entry:

- **Source data**: the table and column the claim is checked against. (See
  `docs/schema.md` for the tables.)
- **Verification rule**: what the verifier does, in plain English.
- **Classification**:
  - *field-based*: checked by reading a single recorded value (fast, certain).
  - *pattern-based*: checked by looking across many transactions for a pattern.
- **Example**: a one-sentence illustration.

---

## 1. prior_sar_history

The AI claims the customer has previously had a suspicious activity report
(SAR) filed against them.

- **Source data**: `prior_cases.prior_sar_count`
- **Verification rule**: The claim passes only if the customer's
  `prior_sar_count` is greater than zero. If the AI says there is prior SAR
  history but the count is zero, the claim fails.
- **Classification**: field-based
- **Example**: The AI says "this customer has filed SARs before," and the
  verifier checks whether that is actually recorded.

## 2. rapid_movement

The AI claims money moved in and back out very quickly, a sign of funds passing
through rather than being held.

- **Source data**: `transactions.amount`, `transactions.direction`,
  `transactions.timestamp`
- **Verification rule**: The claim passes if three or more transactions fall
  inside a single 24-hour window, with at least one inbound and one outbound
  among them, together totalling more than 10,000. Otherwise it fails.
- **Classification**: pattern-based
- **Example**: Four transactions of the same amount on the same day, money in
  then straight back out.

## 3. structuring

The AI claims the customer broke a large sum into several smaller deposits to
stay under the reporting threshold (typically 10,000).

- **Source data**: `transactions.amount`, `transactions.timestamp`
- **Verification rule**: The claim passes if two or more inbound deposits, each
  strictly between 9,000 and 9,999, fall inside a 72-hour window. Otherwise it
  fails.
- **Classification**: pattern-based
- **Example**: Deposits of 9,500, then 9,200, 9,700, and 9,400 across three
  days, each one deliberately under 10,000.

## 4. expected_activity_mismatch

The AI claims the customer's actual activity is far larger than what their
profile says to expect.

- **Source data**: `customers.expected_monthly_volume`, `transactions.amount`
- **Verification rule**: The claim passes if the largest single transaction
  exceeds the customer's expected monthly volume. Otherwise it fails.
- **Classification**: pattern-based
- **Example**: A student whose profile expects about 2,000 a month receives a
  single 45,000 wire.

## 5. missing_kyc_data

The AI claims required know-your-customer (KYC) paperwork is missing for this
alert.

- **Source data**: `evidence_items.available`
- **Verification rule**: The claim passes if a required KYC evidence item is
  marked as not available. Otherwise it fails.
- **Classification**: field-based
- **Example**: The source-of-funds document for the alert is recorded as not on
  file.

## 6. stale_kyc_profile

The AI claims the customer's KYC profile is out of date (KYC drift).

- **Source data**: `kyc_profile_status.current_within_12mo`
- **Verification rule**: The claim passes if the profile was not refreshed
  within the last 12 months (`current_within_12mo` is false). Otherwise it fails.
- **Classification**: field-based
- **Example**: A customer whose KYC was last reviewed more than four years ago.

## 7. high_risk_country

The AI claims the customer transacted with a country on the high-risk list.

- **Source data**: `transactions.counterparty_country`
- **Country codes**: the high-risk list uses ISO alpha-2 two-letter codes:
  IR, KP, SY, MM, CU, VE, RU.
- **Verification rule**: The claim passes if at least one transaction has a
  counterparty country on the high-risk list. Otherwise it fails.
- **Classification**: field-based
- **Example**: A transfer sent to a sanctioned jurisdiction.

## 8. unusual_transaction_volume

The AI claims the customer's total transaction volume is abnormal for them.

- **Source data**: `transactions.amount`, `customers.expected_monthly_volume`
- **Verification rule**: The claim passes if the largest single transaction
  exceeds twice the customer's expected monthly volume, taken across the
  customer's entire transaction history. Otherwise it fails.
- **Classification**: pattern-based
- **Example**: A long-dormant account suddenly moves 60,000 in a single day.

## 9. prior_alert_history

The AI claims this customer has triggered alerts before.

- **Source data**: `alerts.customer_id`
- **Verification rule**: The claim passes if the customer has more than one
  alert on record. Otherwise it fails.
- **Classification**: field-based
- **Example**: The customer has two earlier alerts in addition to the current one.

---

## Summary table

| # | Claim type                  | Classification | Checked against                              |
|---|-----------------------------|----------------|----------------------------------------------|
| 1 | prior_sar_history           | field-based    | prior_cases.prior_sar_count                  |
| 2 | rapid_movement              | pattern-based  | transactions (amount, direction, timestamp)  |
| 3 | structuring                 | pattern-based  | transactions (amount, direction, timestamp)  |
| 4 | expected_activity_mismatch  | pattern-based  | customers.expected_monthly_volume, txn amount|
| 5 | missing_kyc_data            | field-based    | evidence_items.available                     |
| 6 | stale_kyc_profile           | field-based    | kyc_profile_status.current_within_12mo       |
| 7 | high_risk_country           | field-based    | transactions.counterparty_country            |
| 8 | unusual_transaction_volume  | pattern-based  | customers.expected_monthly_volume, txn amount|
| 9 | prior_alert_history         | field-based    | alerts.customer_id                           |
