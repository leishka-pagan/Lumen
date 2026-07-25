# Lumen Verify — Defensible AML Decision Workbench

A working prototype that demonstrates how AI-generated AML (anti money laundering)
findings can be deterministically verified against source data before a human
approves them.

**The model drafts. Code verifies. Humans decide.**

## Live demo

**[capstone-lumen.streamlit.app](https://capstone-lumen.streamlit.app/)**

The full workbench, deployed on Streamlit Community Cloud. No install required — it
runs the same seeded dataset and deterministic verifiers as a local checkout.

## Interactive site

Four self-contained interactive pages are published from `docs/` via GitHub Pages:

**[leishka-pagan.github.io/Lumen](https://leishka-pagan.github.io/Lumen/)**

| Page | What it shows |
|---|---|
| [Architecture](https://leishka-pagan.github.io/Lumen/architecture.html) | The six-layer flow from data to audit trail. Click any layer for its inputs, outputs, and the code it maps to, plus the nine claim types, the seven controls, and the headline numbers. The site root lands here. |
| [Playground](https://leishka-pagan.github.io/Lumen/playground.html) | Run the deterministic verifier in your browser. Pick a claim type and a PASS or FAIL case, then watch the same logic as `src/verifier.py` produce a trace and a verdict. No AI involved. |
| [The Finding](https://leishka-pagan.github.io/Lumen/finding.html) | The alignment result: AI accuracy split by whether the enforced rule was stated in the prompt, with a prompt-versus-verifier comparison across all nine claim types. |
| [Scaling](https://leishka-pagan.github.io/Lumen/scaling.html) | The cost model. One AI call per alert, verification in free Python code, and an interactive slider from ten thousand to one hundred thousand alerts a month. |

Each page is a single HTML file with no build step and no external dependencies.
The site root (`index.html`) is a copy of the architecture page, so the repository
URL lands there. Enabling Pages is a one-time repository setting: **Settings →
Pages → Deploy from a branch → `main` / `/docs`**.

## Build principle

Structured claims first. Deterministic verification second. Human approval last.

The AI does not get to write free text that a reviewer rubber-stamps. It emits
structured claims drawn from a closed vocabulary. Each claim is checked against the
underlying records by deterministic code. A human only approves after seeing which
claims passed, failed, or need review, and the review itself is not accepted as
complete unless it was actually performed.

## Status

All six layers are built and the full workbench runs.

| Layer | State |
|---|---|
| Data layer, schema-validated | Live |
| Triage, severity, and evidence completeness | Live |
| AI claim drafting (`src/llm_drafter.py`) | Built. Captured from the live Anthropic API and replayed deterministically, so the demo is reproducible. |
| Schema and vocabulary enforcement | Live |
| Deterministic verification, nine functions | Live |
| Human review gate (`src/review_gate.py`) | Live, unit-tested |
| Audit trail | Live |
| Streamlit workbench | Live |

Two honest scope notes:

- **Analyst dispositions are session-scoped.** The workbench accepts a complete
  human review and flips the gate, but persistence to the review store was out of
  scope for this build. The audit trail contains seeded history.
- **The orchestration pipeline is not on the demo path.** `src/pipeline.py` is the
  intended production wiring; the workbench calls the verifier directly.

## Layout

```
data/        synthetic CSVs plus derived outputs and demo baselines (generated, reproducible)
src/         Python modules (see the module map below)
docs/        specs, design docs, and capstone deliverables
scripts/     data generation and manual smoke tests
tests/       pytest suite covering data integrity, verification, and the review gate
app.py       the Streamlit workbench
```

## Quickstart

To run it locally instead of using the
[live demo](https://capstone-lumen.streamlit.app/):

```bash
pip install -r requirements.txt
py -m pytest          # run the full test suite
py -m streamlit run app.py
```

Two things worth knowing before you run anything:

**Do not regenerate the data unless you mean it.** `py scripts/generate_data.py`
rebuilds every CSV from a fixed seed. It is reproducible, but it also rewrites the
seeded review records and captured AI outputs that the demo cases depend on.

**The test suite requires a git checkout.** Several tests assert that the source
tree is unchanged by comparing files against committed blobs, so they need a `.git`
directory. Running the suite from a downloaded ZIP will fail those tests. This is a
test-harness limitation, not an application failure.

## The nine claim types

The AI may only assert claims from a closed list of nine types. The list given to
the model is generated at runtime from the same collection the verifier dispatches
on, so a claim type cannot be offered to the model unless a verifier function
exists for it. Adding a tenth type requires writing its check first.

`structuring`, `rapid_movement`, `unusual_transaction_volume`,
`expected_activity_mismatch`, `high_risk_country`, `prior_sar_history`,
`prior_alert_history`, `missing_kyc_data`, `stale_kyc_profile`

Each type, its exact verification rule, and its source table are documented in
[`docs/claim_types.md`](docs/claim_types.md).

## Anti-rubber-stamp review gate

The gate is a shared, deterministic, pure rule in `src/review_gate.py`
(`evaluate_review`). A human review counts as complete only when `evidence_reviewed`
is true and `draft_disposition`, `decision_reason`, `final_note`, and `final_action`
are all present and valid. Anything less is a rubber stamp and does not evaluate as
complete.

- **One rule, two consumers.** The workbench and the pipeline evaluate reviews
  through the same function, so display and enforcement cannot diverge. A test
  asserts the rule is not duplicated.
- **Fails closed.** A blocked review yields no final disposition and is never
  treated as finalized. The pipeline writes exactly one structured `review_blocked`
  audit event, or `review_bypassed` when enforcement is explicitly disabled.
  Enforcement is passed explicitly and defaults to on.
- **Live in the UI.** The Case File shows a HUMAN-REVIEW GATE panel driven by that
  rule, and an analyst can submit a disposition that flips it from BLOCKED to
  COMPLETE. That disposition is session-scoped and writes nothing to disk.

## Module map

| Module | Purpose |
|---|---|
| `src/schema.py` | Pydantic v2 models and validation for the documented tables |
| `src/verifier.py` | Nine deterministic verification functions, one per claim type |
| `src/review_gate.py` | Pure deterministic human-review completeness rule |
| `src/review_routing.py` | Deterministic policy deciding whether human review is required |
| `src/audit.py` | Audit trail writer using a controlled event vocabulary |
| `src/pipeline.py` | Orchestration; the intended production wiring |
| `src/llm_drafter.py` | Calls the Anthropic API via tool use to draft structured claims |
| `src/live_capture.py` | Captures live API responses for deterministic replay |
| `src/capture_guardrails.py` | Budget and authorization gates around live capture |
| `src/case_lifecycle.py` | Frozen validated domain model for case state |
| `src/lifecycle_projector.py` | Derives lifecycle state from source records |
| `src/lifecycle_store.py` | Atomic CSV persistence for the lifecycle projection |
| `src/demo_reset.py` | Restores demo state from baseline copies; fails closed |

## Documentation

- [`docs/schema.md`](docs/schema.md) — the documented tables, fields, and
  relationships.
- [`docs/claim_types.md`](docs/claim_types.md) — the closed claim-type vocabulary,
  written for non-engineers.
- [`docs/AI_INNOVATION_MAP.md`](docs/AI_INNOVATION_MAP.md) — where AI is and is not
  applied across the workflow, what was deliberately not automated, and what the
  verification layer caught.
- [`docs/PROMPT_LIBRARY.md`](docs/PROMPT_LIBRARY.md) — the exact runtime prompt, the
  nine claim and verifier contracts, measured results, and the procedure for adding
  a tenth claim type.
