# Defensible AML Decision Workbench

A working prototype that demonstrates how AI-generated AML (anti money
laundering) case summaries can be deterministically verified against source
data before a human approves them.

## Build principle

Structured claims first. Deterministic verification second. Human approval last.

The AI does not get to write free text that a reviewer rubber-stamps. The AI
emits structured claims drawn from a closed vocabulary. Each claim is checked
against the underlying records by deterministic code. A human only approves
after seeing which claims passed, failed, or need review.

## Project status: Phase 2 (LLM drafter)

Phase 1 delivered the foundation: scaffolding, schema, synthetic data
with planted hero cases, closed claim-type vocabulary, verifier dispatcher,
and audit log.

Phase 2 delivers the LLM integration layer: src/llm_drafter.py calls the
Anthropic API using tool use, forces structured claims from the closed
vocabulary, validates every claim against schema gates, and logs the full
draft lifecycle to the audit trail. The pipeline LLM step is now live.

Not in this phase: the Streamlit UI and real verifier
pattern-matching logic. Those arrive in later weeks.

## Layout

```
data/        synthetic CSVs, one per table (generated, reproducible)
src/         Python modules (schema, verifier, audit, pipeline, llm_drafter)
docs/        specs and design docs (schema.md, claim_types.md)
scripts/     data generation
scripts/smoke_drafter.py   live API smoke test (manual only, not CI)
tests/       pytest sanity checks for the planted hero cases
```

## Quickstart

```bash
pip install -r requirements.txt
python scripts/generate_data.py   # (re)generate the synthetic CSVs
python -m pytest                  # run the data-integrity tests
```

Data generation uses a fixed random seed, so the CSVs are byte-for-byte
reproducible across runs.

## Smoke test

Requires an Anthropic API key in the environment.

Set ANTHROPIC_API_KEY then run:

    python scripts/smoke_drafter.py

This calls the real API for ALERT001 and prints the returned claims.
Never run this in CI. It is a manual verification tool only.

## Hero cases

The synthetic data has planted demo moments. See `scripts/generate_data.py`
(search for "HERO CASE") and the "Hero cases" section of `docs/schema.md` for
exactly where each one lives and what it demonstrates.

## Anti-rubber-stamp review gate (Hero Case B)

The anti-rubber-stamp gate is implemented as a shared, deterministic, pure rule in
`src/review_gate.py` (`evaluate_review`). A human review is acceptable only when
`evidence_reviewed` is true and `draft_disposition`, `decision_reason`, `final_note`,
and `final_action` are all present and valid; otherwise it is a rubber stamp.

- **Pipeline enforcement.** `src/pipeline.py` `process_alert()` Step 3 evaluates the
  alert's human review through this gate. It fails closed: a blocked review yields no
  final disposition and is never treated as finalized, and exactly one structured
  `review_blocked` audit event is written (or `review_bypassed` when enforcement is
  explicitly disabled). Enforcement state is passed explicitly and defaults to on.
- **UI display.** The Case File evaluates a stored review with the *same* shared rule
  to show a "HUMAN-REVIEW GATE" panel (e.g. BLOCKED for the seeded REV001 / ALERT007).
  This is display only and writes no audit event.
- **Honest scope.** The live Streamlit UI does not submit or finalize human
  dispositions — there is no disposition-submission workflow. It evaluates and displays
  stored/seeded reviews; the decision pipeline is the enforcement path.

## Module map

| Module           | Status | Purpose                                          |
|------------------|-------------------|--------------------------------------------------|
| `src/schema.py`  | Complete          | Pydantic v2 models for all 9 tables              |
| `src/audit.py`   | Complete          | Append-only audit log writer                     |
| `src/verifier.py`| Complete          | Deterministic claim verification for all 9 claim types |
| `src/pipeline.py`| Complete          | Orchestration; Step 3 invokes the anti-rubber-stamp review gate |
| `src/review_gate.py`| Complete       | Pure deterministic human-review gate (Hero Case B) |
| `src/llm_drafter.py`| Complete       | Calls Anthropic API via tool use to draft structured claims |

## Documentation

- `docs/schema.md`: the 9 tables, field descriptions, and relationships.
- `docs/claim_types.md`: the closed claim-type vocabulary, written for
  non-engineers.
