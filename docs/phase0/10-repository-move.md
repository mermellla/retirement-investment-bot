# Repository move (OI-13) — canonical repository: `mermellla/retirement-investment-bot` (public, MIT)

**Status 2026-09-17:** the clean history below was pushed to `github.com/mermellla/retirement-investment-bot` on top of the
owner's initial commit (LICENSE, README) and the full suite was run there. That repository is canonical from this point;
the `agency-agents` branches are historical. Because the repository is public: secrets stay in Railway/Claude environment
variables only (the lockout test scans for secret-shaped literals), no owner contact details are hard-coded
(`EDGAR_USER_AGENT` is required from the environment), and the Python package keeps its internal name `tradeagent`.

The Phase 0 package was rebuilt as a standalone repository with **fresh history** — no fork history, no persona files,
no unrelated CI or license — and re-verified there.

## What the clean repository contains
```
README.md  pyproject.toml  .gitignore
config/    docs/{spec,phase0,adr}/   supabase/{migrations,migrations_deferred}/   src/tradeagent/   tests/
```
History: commit 1 `Phase 0 checkpoint` (the tree as approved, tagged `phase0-checkpoint`), commit 2
`Phase 0 review round 1` (this round). No LICENSE file is included: the package is original work and the licence is
the owner's choice (the fork's MIT licence was not carried over).

## Verification run inside the clean repository (2026-09-13)
| Check | Command | Result |
|---|---|---|
| Migrations | apply all nine files to a fresh PostgreSQL 16 database | clean; 38 tables, 32 enums, 21 functions in schema `trading`, 0 in `public` |
| Tests | `python -m pytest -q` with `TRADEAGENT_TEST_ADMIN_URL` set | 78 passed |
| Type check | `python -m mypy src` (strict, pydantic plugin) | no issues |
| Lint / format | `ruff check src tests`, `ruff format --check src tests` | clean |
| Secret scan | `detect-secrets scan --all-files` over the whole tree | no findings |
| Lockout grep | `tests/test_lockout_grep.py` | no live URL, live key names, funding endpoints, or secret-looking literals |

## How it was delivered
The GitHub App used by this session is not permitted to create repositories (`POST /user/repos` → 403,
verification V-21), so the clean history is delivered two ways; both contain the identical commits:

1. **Git bundle** `trading-agent-phase0.bundle` (sent as a file). Push it to a new empty private repository:
   ```
   gh repo create mermellla/trading-agent --private
   git clone trading-agent-phase0.bundle trading-agent && cd trading-agent
   git remote set-url origin git@github.com:mermellla/trading-agent.git
   git push -u origin main --tags
   ```
2. **Orphan branch** `trading-agent-clean` in `mermellla/agency-agents` (same commits, no shared history with `main`):
   ```
   git clone --branch trading-agent-clean --single-branch git@github.com:mermellla/agency-agents.git trading-agent
   cd trading-agent && git remote set-url origin git@github.com:mermellla/trading-agent.git
   git push -u origin trading-agent-clean:main --tags
   ```
After the push, delete the `trading-agent-clean` branch and the `claude/spec-v2-3-phase-0-*` branch from
`agency-agents`; nothing else in that fork references the package.

## Owner steps before Slice 1 (from ADR-0022 and §11)
1. Create the Supabase project; run the migrations with the CLI; leave `trading` out of the exposed schemas.
2. `alter role trading_worker login password '…'`; set `DATABASE_URL` in Railway to that role's URL.
3. Create the private Storage bucket `decision-context`.
4. Create the Railway project with the environment variables listed in `03-architecture.md`.
