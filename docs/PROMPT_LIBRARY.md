# Lumen Verify — AI Workflow Prompt Library

**Capstone Deliverable 5**
WIC AI Innovator Capstone · Banking & Financial Crime Compliance Intelligence Challenge

---

## 1. How to use this library

This document records the instructions Lumen gives a language model, the closed set of claims the model may produce, and the deterministic function that checks each one. It is a governance artifact, not a runtime component. The application does not read it, and analysts do not interact with it.

It answers five questions a reviewer or a future developer will ask:

1. What exactly is the model told?
2. What may it assert, and what may it not?
3. What checks each assertion?
4. What went wrong when we ran it?
5. How does someone safely add a tenth claim type?

**Lumen does not maintain nine prompts.** It issues one system prompt, one tool schema, and one per-alert user message carrying source records. Section 3's nine entries are claim and verifier contracts, not prompt templates. Calling them templates would describe a structure this codebase does not have.

**The model drafts. Code verifies. Humans decide.**

**What this document found.** Comparing each claim type's description in the prompt against the function that scores it, six of nine describe a different rule than the one enforced. Every contradicted claim in the corpus belongs to one of those six. Section 4 gives the numbers.

---

## 2. The runtime prompt and its controls

### 2.1 System prompt, verbatim

Assembled by `_build_system_prompt()` at `src/llm_drafter.py:97-123` from two literal blocks with a generated block between them, joined with newlines. The type list is produced by looping over `KNOWN_CLAIM_TYPES`, which `llm_drafter.py` imports from `verifier.py`, with descriptions from `CLAIM_TYPE_DESCRIPTIONS` at `src/llm_drafter.py:47-57`.

```
You are an AML (anti money laundering) claim drafter.

You may only assert claims from this closed list of types:
- expected_activity_mismatch: Observed activity is far above the customer's declared expected_monthly_volume.
- high_risk_country: A transaction involves a counterparty country on the high-risk list.
- missing_kyc_data: A required KYC evidence item for this alert is not available.
- prior_alert_history: The customer has triggered alerts before this one.
- prior_sar_history: Customer has a prior suspicious activity report on record (prior_cases.prior_sar_count greater than zero).
- rapid_movement: Funds moved in and back out within a short window (transactions in then out, similar amounts, same day).
- stale_kyc_profile: The KYC profile was not refreshed within the last 12 months.
- structuring: Multiple deposits each just under the reporting threshold, aggregating over a short span of days.
- unusual_transaction_volume: Aggregate transaction volume is well outside the customer's normal baseline.

You MUST respond by calling the submit_aml_claims tool with every claim you
want to make. Do not return any text. Only the tool call.

Include only claims you can back with evidence_refs that point at actual
rows in the source data provided in the user message.
Format each reference as the exact CSV table name, a dot, the column
name, an equals sign, and the value. For example:
prior_cases.customer_id=CUST0001 or transactions.txn_id=TXN00001.
Legal table names are: customers, transactions, alerts, evidence_items,
prior_cases, kyc_profile_status, ai_outputs, human_reviews.
Do not invent table names.

Emit between 1 and 3 claims for this alert. Not more.
```

The system prompt, the tool schema, and the per-alert user message are three separate API parameters. They are never concatenated into one string.

### 2.2 What constrains the output

| Control | Mechanism | What it prevents |
|---|---|---|
| Tool schema | The model must answer with a `submit_aml_claims` tool call and no prose | Free-text responses; unparseable output |
| Generated vocabulary | The type list in the prompt is built from `KNOWN_CLAIM_TYPES`, imported from the verifier module | A claim type appearing in the prompt with no verifier behind it |
| Dispatch gate | `verify_claim` returns NEEDS_REVIEW for a `claim_type` absent from its dispatch table rather than dispatching it | An unrecognized claim reaching a verifier |
| Evidence-reference gate | For types listed in `REQUIRES_EVIDENCE_REFS`, `verify_claim` rejects a claim whose `evidence_refs` field is empty | Bare assertions with no citation attached |
| Output cap | The prompt limits the model to between one and three claims per alert | Volume padding. Ninety-three claims across thirty-nine alerts. |

Two limits of this list, stated rather than left to be discovered:

**The evidence-reference gate checks presence, not truth.** It tests that the field is non-empty. No verifier function reads `evidence_refs` at all; each re-queries the source tables by `customer_id` or `alert_id`. A claim citing a transaction identifier that does not exist would pass the gate identically to one citing a real row. The prompt's most detailed instruction is enforced nowhere.

**Two of the eight legal table names are not source tables.** The prompt invites references to `ai_outputs` and `human_reviews`. Neither is loaded by verification. The model may cite tables nothing checks against.

---

## 3. The nine claim and verifier contracts

Every claim type maps to exactly one deterministic function in `src/verifier.py`. The column that matters is the third: whether the description the model receives states the rule it will be scored against.

| Claim type | What the prompt tells the model | What the verifier enforces | Rule stated to the model? |
|---|---|---|---|
| `prior_sar_history` | `prior_cases.prior_sar_count` greater than zero | `prior_sar_count > 0` on the customer's first prior-cases row | **Yes**, including table, column and operator |
| `stale_kyc_profile` | Profile not refreshed within the last 12 months | `current_within_12mo` is false | **Yes** |
| `missing_kyc_data` | A required KYC evidence item for this alert is not available | At least one `evidence_items` row for the alert has `available` false | **Yes** |
| `high_risk_country` | Counterparty country is on the high-risk list | Membership in a frozenset of seven ISO codes | Rule yes, **list never supplied** |
| `structuring` | Multiple deposits just under the reporting threshold, over a short span of days | Two or more inbound transactions strictly between 9,000 and 9,999, inside 72 hours | **Partly.** Direction, band, count and window all undisclosed |
| `prior_alert_history` | Customer has triggered alerts **before this one** | Count of alert rows for the customer exceeds one | **No.** No ordering or date filter; the current alert is counted |
| `expected_activity_mismatch` | Activity **far above** declared `expected_monthly_volume` | Largest single transaction exceeds `expected_monthly_volume`, strictly | **No.** "Far above" against a strict 1× bar |
| `rapid_movement` | In then out, **similar amounts, same day** | Three or more transactions in a rolling 24-hour window, both directions, summing above 10,000 | **No.** Amount similarity never checked; the 10,000 floor and the count of three are undisclosed |
| `unusual_transaction_volume` | **Aggregate** transaction volume well outside baseline | Largest **single** transaction exceeds 2× `expected_monthly_volume`, across the customer's entire history | **No.** Aggregate asked for, single transaction scored |

Three scope notes, so nobody has to derive them:

- `unusual_transaction_volume` and `expected_activity_mismatch` are nested predicates over one calculation, at 2× and 1×. Every pass of the first implies a pass of the second. They are not independent checks.
- `missing_kyc_data` confirms that *a* gap exists. It does not confirm that the item the model named is the missing one, and it treats any value other than false, including a malformed one, as available.
- The 12-month rule behind `stale_kyc_profile` is applied upstream in the data layer as a 365-day comparison against a fixed reference date. The verifier reads the resulting boolean and performs no date arithmetic. A `kyc_staleness_months` setting exists in the application interface and is consumed by no verification path.

---

## 4. Results, limitations, and what we deliberately did not prompt for

### 4.1 The corpus

| | Count |
|---|---|
| Claims captured | 93 |
| Supported by source evidence | 61 |
| Contradicted by source evidence | 32 |
| Needs review | 0 |
| Alerts represented | 39 of 39 |

Roughly one in three AI-drafted claims was contradicted. Every one was caught by code before an analyst saw it.

| Claim type | Supported | Contradicted | Rate | Rule stated? |
|---|---|---|---|---|
| `unusual_transaction_volume` | 1 | 19 | 95% | No |
| `rapid_movement` | 1 | 2 | 67% | No |
| `high_risk_country` | 1 | 1 | 50% | List withheld |
| `expected_activity_mismatch` | 12 | 10 | 45% | No |
| `structuring` | 1 | 0 | 0% | Partly |
| `prior_sar_history` | 1 | 0 | 0% | Yes |
| `missing_kyc_data` | 28 | 0 | 0% | Yes |
| `stale_kyc_profile` | 16 | 0 | 0% | Yes |

### 4.2 The finding

Group the corpus by whether the model was told the rule it would be scored against.

| | Claims | Contradicted | Rate |
|---|---|---|---|
| Rule stated in the prompt | 45 | 0 | **0%** |
| Rule withheld or differently stated | 48 | 32 | **67%** |

**Where the prompt's definition matched the verifier's rule, the model was right forty-five times out of forty-five. Where the two diverged, it was wrong about two thirds of the time.**

The sharpest case is `unusual_transaction_volume`. The model is told to assess *aggregate* volume against a baseline. The verifier compares the *largest single transaction* against twice the expected monthly figure. Nineteen of twenty claims of that type were contradicted, and the one claim carrying a narrative value carried an aggregate, because an aggregate is what the prompt asked for.

The remedy this points to is not better phrasing. It is stating the enforced rule, with its threshold, in the description the model receives. `prior_sar_history` already does this. Its description names the table, the column and the operator, and it is the only description written in the verifier's own language.

### 4.3 What this finding does not establish

Alignment and computational shape co-vary perfectly in this dataset. Every aligned type is a field read; every misaligned type filters, windows, or does arithmetic. These data cannot separate "the model was told a different rule" from "the model is weak at computation."

The one type that discriminates between the two is `high_risk_country`. It is set membership, a lookup rather than a calculation, and it still contradicts at 50%, because the list of seven countries was never given to the model. That favors the definition-gap explanation. It rests on two claims.

Four further qualifications:

1. Four types carry an n of one or two. Only `missing_kyc_data` (28), `expected_activity_mismatch` (22), `unusual_transaction_volume` (20) and `stale_kyc_profile` (16) carry weight.
2. `unusual_transaction_volume` and `expected_activity_mismatch` are nested, so two of the four threshold-shaped rates describe one computation.
3. The 0% for `missing_kyc_data` reflects a permissive contract, as Section 3 records.
4. The dataset is synthetic. These figures demonstrate that this mechanism catches this class of error. They are not a real-world error rate.

### 4.4 One claim the verifier refused but never examined

On ALERT023 the model asserted `unusual_transaction_volume` with a narrative value rather than a boolean: an aggregate of 18,746.00 across six transactions in May 2026. It cited six transaction identifiers and the customer's expected monthly volume. Every reference was real. The six transactions exist, they are the customer's only transactions, and they do fall in May 2026.

Their actual total is 22,846.00. The asserted figure corresponds to no subset of them, no directional subtotal, and no signed combination. Tested exhaustively against all sixty-three subset sums and all seven hundred and twenty-nine signed readings, it matches none. The figure is fabricated, not mis-aggregated.

The verifier returned FAIL. **It did not do so because of the invented number.** It compared the largest single transaction, 5,354.00, against a 24,000.00 bar. The asserted aggregate never entered the computation, and the claim would have failed identically had the model stated the correct total.

The honest account: the deterministic layer refused to certify an unsupported claim, which is what it exists to do. It did not detect the fabrication, because nothing examines `asserted_value` when that field carries prose. The closed vocabulary governs which claim may be made. It does not govern what shape the assertion takes. And the prose appeared at all because the prompt asked for an aggregate the schema had no field for.

### 4.5 Anti-patterns

The following were considered and rejected, each for the same reason: no deterministic function could falsify the output.

| Rejected | Why |
|---|---|
| "Summarize this alert for the analyst" | A summary cannot be checked against a source table. It can only be believed. |
| "Recommend a disposition" | Moves the decision to the model. The human then reviews a recommendation, not evidence. |
| "Assign a severity rating" | Severity is a policy determination from the bank's risk matrix, not a model output. |
| "Rate your confidence in this finding" | A confident wrong claim and an unconfident wrong claim are both wrong. |
| "Draft the SAR narrative" | Model-authored regulatory filing text with no verification layer is the failure this project exists to prevent. |
| "Explain why this customer is suspicious" | Invites a constructed rationale, indistinguishable in form from an evidenced one. |

These exclusions are enforced by the tool schema and by what the verifiers accept, not by a list of prohibitions in the prompt. The prompt does not tell the model what to avoid. It gives the model one way to answer.

---

## 5. Adding a tenth claim type

The order matters. Each step exists because skipping it produced a defect recorded above.

1. **Write the verifier function first.** It must read named columns from named source tables and return PASS, FAIL, or NEEDS_REVIEW. If the rule cannot be stated as a comparison against stored values, stop. The claim type does not belong in this system.
2. **Register it in the dispatch table.** Until then, `verify_claim` returns NEEDS_REVIEW for it rather than dispatching.
3. **Add it to `KNOWN_CLAIM_TYPES`.** The prompt's vocabulary is generated from this collection, so the type becomes assertable at this point and not before.
4. **Write the description in the verifier's language.** State the table, the column, the operator and the threshold, the way `prior_sar_history` does. This is the step Section 4.2 exists to justify. A description that paraphrases intent instead of stating the rule produces claims the verifier will contradict.
5. **Decide whether it belongs in `REQUIRES_EVIDENCE_REFS`,** knowing the gate checks that references are present and not that they resolve.
6. **Constrain the asserted value.** Decide what type the assertion is and reject anything else. Section 4.4 is what happens when this step is skipped.
7. **Add tests** covering PASS, FAIL, NEEDS_REVIEW, a missing source table, and an unparseable value.
8. **Record the contract in Section 3 of this document,** including whether the rule is stated to the model.

The vocabulary cannot expand by prompting alone, because the prompt does not own the list. It reads it from the verifier module.

---

## Appendix A — Provenance and corrections

Every contract in Section 3 was read directly from `src/verifier.py` at commit `f289b8c` on 2026-07-22. The system prompt in Section 2.1 is the rendered output of `_build_system_prompt()` at the same commit. No contract in this document is transcribed from design intent or from observed behavior.

An earlier revision of this library described five of these contracts incorrectly:

| Claim type | Earlier description | Code |
|---|---|---|
| `structuring` | Sub-10,000 deposits in a 72-hour window | Inbound transactions strictly between 9,000 and 9,999 |
| `stale_kyc_profile` | Compares refresh date against a freshness threshold | Reads a precomputed boolean; no date arithmetic |
| `prior_alert_history` | Checks prior alert records | Counts all alerts, including the current one |
| `unusual_transaction_volume` | Single transaction above 2× expected monthly volume | Correct, but spans the customer's entire history |
| `missing_kyc_data` | At least one required evidence item missing | Correct, but does not check which item |

That revision also carried a standing system framing block written from design intent, which did not match the assembled prompt, and a statement that every verification result is written to the audit trail, which is not what the audit trail records.

The correction record is retained deliberately. A document about verifiable claims should show that its own claims were verified, and against what.

## Appendix B — Next steps, ordered by what each prevents

| Next | What it addresses |
|---|---|
| State each verifier's enforced rule and threshold in its prompt description | Section 4.2. The 67% contradiction rate on types whose rule was withheld. |
| Supply the high-risk country list to the model at draft time | The 50% rate on `high_risk_country`. |
| Constrain `asserted_value` to a typed value per claim type | Section 4.4. Prose cannot enter a boolean field. |
| Validate that `evidence_refs` resolve to real rows, and remove `ai_outputs` and `human_reviews` from the legal table list | Section 2.2. The prompt's most detailed instruction is currently unenforced. |
| Tighten `prior_alert_history` to exclude the alert under review | Section 3. |
| Have `missing_kyc_data` check the item the model named | Section 3, and the reading of its 0% rate. |
| Move the KYC staleness threshold into a named, verifier-consumed constant | Section 3, and the currently inert `kyc_staleness_months` setting. |
| Reach the `NEEDS_REVIEW` verdict state | Implemented and tested, unreachable in the current dataset because every claim's source data was present. |

---

*Lumen Verify · github.com/leishka-pagan/Lumen*
