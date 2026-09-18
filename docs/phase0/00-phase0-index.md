# Phase 0 engineering package — index

**Spec:** `docs/spec/SPEC-v2.3.md` (authoritative) · **Date:** 2026-09-13 · **Modes buildable:** DRY_RUN, PAPER · **LIVE:** locked out (ADR-0020)

| # | Deliverable (owner's Phase 0 list) | Where |
|---|---|---|
| 1 | Finalized spec under `docs/spec/` | `docs/spec/SPEC-v2.3.md`, `docs/spec/README.md` |
| 2 | Requirements-to-implementation traceability matrix | `01-traceability-matrix.md` |
| 3 | Contradictions, impossible requirements, API assumptions to verify, underspecified behaviour | `02-open-issues.md` (OI-01…OI-16) and `08-api-verification-log.md` (V-01…V-18) |
| 4 | ADRs required by Appendix C | `docs/adr/` — ADR-0001…0018 (one per item) + ADR-0019 (versioning/phases) + ADR-0020 (LIVE lockout) + ADR-0021 (capital reservation) + ADR-0022 (Supabase security) |
| 5 | Module/package architecture | `03-architecture.md` |
| 6 | Major domain interfaces and typed models | `src/tradeagent/domain/{enums,models}.py`, `src/tradeagent/interfaces/__init__.py`; inventory in `04-interfaces.md` |
| 7 | Supabase/Postgres schema | `05-schema.md` |
| 8 | Initial repo-tracked migrations | `supabase/migrations/` (nine files; 38 tables, 32 enums, 21 functions in schema `trading`; applied clean on PostgreSQL 16) + `supabase/migrations_deferred/` (3 tables deferred to S7/S9) |
| 9 | Test plan mapping every §17 deliverable and invariant to tests | `06-test-plan.md` |
| 10 | Tests writable before implementation | `06-test-plan.md` column "Pre-impl"; 130 already written and passing in `tests/`; ruff, mypy --strict, detect-secrets clean |
| 11 | Implementation sequence as vertical slices | `07-implementation-sequence.md` |

Also produced: versioned config seeds in `config/` (risk policy, fees, SIC backstop, seed denylist, scanner weights,
QB-1.0 rules, phase-change record) and the Python package skeleton (`pyproject.toml`, `src/tradeagent/`).

## What this package does not do
- No feature implementation: no adapters, no scanner, no LLM calls, no order submission. Interfaces are Protocols with
  no implementations; the only executable logic is config loading/validation, the exclusion screen, and model validation.
- No credentials anywhere; no network calls in code or tests.
- No LIVE path: refused by config, schema, and state machine (ADR-0020).

## Slice 3 delivered (2026-09-18; paper probe pending)
Paper broker gateway, §8.10 policy enforcement on every boot, §8.6 replay-or-halt reconciliation through the ledger,
09:10 stop re-arm with 09:31/09:33 verification, corporate actions; see `07-implementation-sequence.md` row S3. 149
tests, ruff, mypy --strict, detect-secrets clean. The ADR-0013 fractional Day-stop probe (`tradeagent
probe-fractional-stop`, guarded by `TRADEAGENT_PROBE_CONFIRM=yes`) is built but has not run: no `ALPACA_PAPER_KEY/SECRET`
were present. OI-01 stays open until it does.

## Slice 2 delivered (2026-09-17)
Universe and scanner with Jev-judged catalysts (ADR-0023, A-10); see `07-implementation-sequence.md` row S2. 130 tests,
ruff, mypy --strict, detect-secrets clean. Live scans need `ALPACA_PAPER_KEY/SECRET`, `EDGAR_USER_AGENT`, `FINNHUB_API_KEY`,
`TYPESAFE_API_KEY` in the environment; none were present in the development environment.

## Slice 1 delivered (2026-09-13)
Boot and ledger spine: `tradeagent boot-check | run | verify-projections` (`src/tradeagent/cli.py`); see
`07-implementation-sequence.md` row S1 and `03-architecture.md`. 97 tests, ruff, mypy --strict, detect-secrets clean.

## Owner review round 1 (2026-09-13)
Accepted amendments are logged in `docs/spec/AMENDMENTS.md` (A-01…A-06). Decisions still requested are in
`09-owner-decisions.md` (SIC mapping, denylist review, defaults table, schema-rent audit). The canonical repository is
`mermellla/retirement-investment-bot` (public, MIT); see `10-repository-move.md`.

## Status of the verification work
18 external assumptions were checked against current documentation on 2026-09-13 (`08-api-verification-log.md`).
OI-06 is resolved and OI-02/03/05 are accepted; OI-04 and OI-07 await the owner's policy call; OI-01 needs the Slice 3 paper probe (ADR-0013).
