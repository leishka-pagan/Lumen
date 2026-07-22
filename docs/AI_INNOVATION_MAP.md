# Lumen Verify — AI Innovation Map

**Capstone Deliverable 1**
WIC AI Innovator Capstone · Banking & Financial Crime Compliance Intelligence Challenge

---

## 1. The headline

The AML alert workflow has nine stages. **Lumen applies AI to exactly one of them.**

The other eight are deterministic code or human judgment, and they exist in large part to constrain the one stage that is AI. That is the innovation. Not the model, the containment around it.

Most AI compliance tooling asks where AI can be inserted. Lumen asks a narrower question: *where can AI be used such that its output remains falsifiable?* Everything in this map follows from that.

---

## 2. The workflow map

| # | Stage | Mechanism | Who decides | AI? |
|---|---|---|---|---|
| 1 | Alert detection | Rule-based transaction monitoring | Bank policy | No |
| 2 | Triage and severity | Severity matrix over rule, amount and customer risk | Bank policy | No |
| 3 | Evidence assembly | Required-item lookup against source tables | Deterministic | No |
| 4 | Evidence completeness | Available items over required items | Deterministic | No |
| 5 | **Claim drafting** | **Anthropic tool-use API, closed vocabulary, structured output** | **Model proposes** | **Yes** |
| 6 | Schema and vocabulary enforcement | Validation gates before dispatch | Deterministic | No |
| 7 | Claim verification | Nine verifier functions against source tables | Deterministic | No |
| 8 | Human review and disposition | Analyst, gated on review completeness | **Human decides** | No |
| 9 | Audit trail | Controlled-vocabulary event log | Deterministic | No |

This is a decomposition of the workflow, not a module map. The nine stages are how the process is best understood; they do not each correspond to a named structure in the codebase.

Stage 5 is the only place a language model touches the workflow. Stages 6 and 7 exist specifically to check stage 5. Stage 8 is where a person is accountable. Stage 9 is what an examiner reads.

**One stage of proposal. Two stages of mechanical rebuttal. One stage of human accountability.**

---

## 3. What AI does, precisely

**It converts unstructured case context into discrete, checkable assertions.**

That is the whole job. Given a customer's transaction records and profile, the model emits structured claims of nine approved types: `structuring`, `rapid_movement`, `unusual_transaction_volume`, `expected_activity_mismatch`, `high_risk_country`, `prior_sar_history`, `prior_alert_history`, `missing_kyc_data`, `stale_kyc_profile`.

Not a summary. Not a risk score. Not a recommendation. A list of factual assertions, each of which a Python function can accept or reject.

The innovation is that the model's output is deliberately shaped to be *rebuttable*. An analyst reading a generated summary can only agree or disagree with it. An analyst reading a verified claim is looking at something a machine already tried to disprove.

The vocabulary is closed in code rather than by instruction. The claim-type list inside the system prompt is generated at runtime from the same collection the verifier dispatches on, so a claim type cannot be offered to the model unless a verifier exists for it. Adding a tenth type requires writing its verifier first.

---

## 4. What we deliberately did not automate

Every item below was technically feasible and was rejected for the same reason: **no deterministic function could falsify the output.**

| Not automated | Why |
|---|---|
| Case summarization | A summary cannot be checked against a source table. It can only be believed. |
| Disposition recommendation | Moves the decision to the model. The analyst then reviews a recommendation, not evidence. |
| Severity assignment | Severity is a policy determination from the bank's risk matrix, not a model output. |
| Confidence scoring | Self-reported confidence is not evidence. A confident wrong claim and an unconfident wrong claim are equally wrong. |
| SAR narrative drafting | Model-authored regulatory filing text with no verification layer is the exact failure this project exists to prevent. |
| Alert closure | An AI that auto-closed alerts would be the regulatory failure, not the efficiency gain. |

Each exclusion removes a capability. Each also removes a path by which unverifiable model output could reach a regulatory record.

---

## 5. Why the containment is necessary, measured

Across the seeded dataset, every AI-generated claim was re-verified through the deterministic verifiers.

| | Count |
|---|---|
| Claims drafted by the model | 93 |
| Supported by source evidence | 61 |
| **Contradicted by source evidence** | **32** |
| Alerts represented | 39 of 39 |

Roughly one in three model-drafted claims was contradicted by the records. Every one was caught before an analyst saw it.

The failures are not randomly distributed, and the dividing line turned out to be a property of the instructions rather than of the subject matter.

Each claim type is described to the model in the system prompt. Comparing those descriptions against the functions that score them, six of the nine describe a different rule than the one enforced. `unusual_transaction_volume`, for instance, is described to the model as an *aggregate* volume assessment, while the verifier compares the *largest single transaction* against twice the expected monthly figure.

| | Claims | Contradicted | Rate |
|---|---|---|---|
| Rule stated in the prompt | 45 | 0 | **0%** |
| Rule withheld or differently stated | 48 | 32 | **67%** |

**Where the prompt's definition matched the verifier's rule, the model was right forty-five times out of forty-five. Where the two diverged, it was wrong about two thirds of the time.**

Two honest qualifications. Alignment and computational shape co-vary perfectly in this dataset, so these figures cannot fully separate "the model was told a different rule" from "the model is weak at computation." The one type that discriminates, `high_risk_country`, is a simple list lookup and still contradicts at 50%, because the list was never given to the model; that favors the first explanation, on two observations. And four of the eight types carry an n of one or two.

The finding is directly actionable: state the enforced rule and its threshold in the description the model receives. One claim type already does this, naming its table, column and operator, and it has no contradictions.

*Figures are from a seeded synthetic dataset built for this capstone. They demonstrate the mechanism, not a production error rate.*

---

## 6. Maturity and scope

Honest status of every component, so this map can be read against the repository.

| Component | Status |
|---|---|
| Data layer, eight source tables, schema-validated | Live |
| Triage and severity matrix | Live |
| Evidence completeness engine | Live |
| Claim drafting (`llm_drafter.py`) | Built. Captured from the live API, replayed deterministically so the demo is reproducible. |
| Schema and vocabulary enforcement | Live |
| Nine deterministic verifiers | Live |
| Human review gate rule (`review_gate.py`) | Live, unit-tested, drives what the analyst sees |
| Disposition capture | Session-scoped demonstration. Persistence to the review store was scoped out of the capstone window. |
| Audit trail | Live. Records review and override decisions, not per-claim verification results. |
| Analyst workbench UI | Live |
| Orchestration pipeline (`pipeline.py`) | Built as the intended production wiring. The workbench calls the verifier directly; the pipeline is not in the demo path. |

The full test suite runs green.

---

## 7. Governance posture

Lumen was designed against current model-risk supervisory expectations, which emphasize documented oversight, validation, and controls, and which explicitly exclude generative and agentic AI from the legacy model-risk framework, leaving institutions to build the control layer themselves.

Lumen is an attempt at that control layer:

- **Validation** is deterministic and repeatable. The same claim against the same records returns the same verdict, every time.
- **Oversight** is enforced, not requested. A review missing evidence confirmation, rationale, or a final action does not evaluate as complete.
- **Documentation** is an examiner-reviewable audit trail using a controlled event vocabulary, so a review or override decision can be reconstructed with actor, action, and timestamp.
- **Explainability** is structural rather than interpretive. Every verdict traces to a named function and a source record, not to a model's account of its own reasoning.

Two gaps in that posture, recorded here rather than left for a reviewer to find:

**The schema constrains which claim may be made, not what shape the assertion takes.** One captured claim carried narrative prose in a field whose verifier expects a boolean, including a figure that matches no reading of the records it cited. The claim was refused as unsupported, on an unrelated threshold test. The fabricated figure itself was examined by nothing.

**Evidence references are required but not resolved.** The prompt instructs the model to cite actual rows in an exact format, and validation confirms the field is present. No verifier reads it; each re-queries the source tables independently. A reference to a row that does not exist would pass.

Both are addressed in Section 8.

---

## 8. Where this goes next

| Next | Why it matters |
|---|---|
| State each verifier's enforced rule and threshold in its prompt description | Directly targets the 67% contradiction rate on types whose rule was withheld |
| Constrain the asserted value to a typed value per claim type | Closes the gap that let prose, and a fabricated figure, into a boolean field |
| Validate that evidence references resolve to real rows | Makes the prompt's most detailed instruction enforceable |
| Persist dispositions to the review store with audit events | Closes the loop from demonstration to system of record |
| Wire the orchestration pipeline into the runtime path | Moves drafting from replay to live inference under the same verification |
| Reach the `NEEDS_REVIEW` verdict state | Implemented and tested, but unreachable in the current dataset because every claim's source data was present |
| Extend the vocabulary beyond nine claim types | Each addition requires its verifier first, by design |

---

## 9. The one-line version

**Lumen Verify uses AI in exactly one stage of a nine-stage workflow, shapes its output so code can prove it wrong, and refuses to let a human sign off on a review they did not perform.**

The model drafts. Code verifies. Humans decide.

---

*Lumen Verify · github.com/leishka-pagan/Lumen*
