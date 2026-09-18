# retirement-investment-bot

Experimental short-term trading agent — Specification v2.3 (`docs/spec/SPEC-v2.3.md`, authoritative). An LLM acts as
portfolio manager over 15-minute-delayed consolidated data with $500 of virtual capital in a 1x, long-only paper account;
the product is the dataset that answers whether it beats cash, SPY, VTI, and a deterministic baseline net of all costs.

Status: Phase 0 package plus Slices 1–3 (boot spine; universe and scanner with Jev-judged catalysts; paper broker
gateway with policy enforcement, replay-or-halt reconciliation, and stop re-arm). Modes buildable:
DRY_RUN, PAPER. LIVE is architected but locked out at every layer (ADR-0020). No credentials live in this repository;
secrets are environment variables only (Railway in deployment).

Start at `docs/phase0/00-phase0-index.md`. Decisions: `docs/adr/`. Accepted spec amendments: `docs/spec/AMENDMENTS.md`.
The Python package is `tradeagent` (`src/tradeagent`).

## Running the checks
```
uv pip install -e ".[dev]"              # or: pip install -e ".[dev]"
export TRADEAGENT_TEST_ADMIN_URL='postgresql://<admin-user>:<password>@localhost:5432/postgres'   # PostgreSQL 16, createdb rights
python -m pytest -q                      # 130 tests; db tests create and drop a throw-away database
ruff check src tests && ruff format --check src tests
python -m mypy src                       # strict, pydantic plugin
detect-secrets scan --all-files --exclude-files '^\.git/'
```

## Running the worker (DRY_RUN)
Environment: `DATABASE_URL` (the `trading_worker` role, ADR-0022), `SUPABASE_URL`, `SUPABASE_ANON_KEY`,
`SUPABASE_SERVICE_ROLE_KEY`, `ALPACA_PAPER_KEY`, `ALPACA_PAPER_SECRET`, `EDGAR_USER_AGENT` (app name + contact email),
`FINNHUB_API_KEY` (optional), `TYPESAFE_API_KEY` (Jev, ADR-0023). Then `tradeagent boot-check` and `tradeagent run`.
