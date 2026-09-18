# Open issues: contradictions, impossible requirements, API assumptions, underspecified behaviour

**Status after the owner's Phase 0 review (2026-09-13):** OI-02, OI-03, OI-05 accepted (`docs/spec/AMENDMENTS.md`);
OI-06 resolved from FINRA's SR-FINRA-2024-019 schedule; OI-01 stays open for the Slice 3 probe; OI-04, OI-07,
OI-08–OI-12 are presented for decision in `09-owner-decisions.md`. New: OI-14 (capital reservation → ADR-0021) and
OI-15 (Supabase exposure → ADR-0022), both closed by design and tests. **Owner approvals of 2026-09-13 closed OI-04, OI-07,
OI-08–OI-12 (A-07…A-09).** Only OI-01 remains open (Slice 3 paper probe).

Each item states the evidence, the impact, and the **smallest compliant amendment** proposed. Nothing here has been
silently reinterpreted: where the package had to pick a value to proceed, the value is marked `(proposed)` in
`config/risk_policy.yaml` and listed under "Decision needed". Verification sources and dates are in
`08-api-verification-log.md` (V-nn).

## A. Requires an owner decision before PAPER

### OI-01 — Alpaca doc pages disagree on fractional stop orders (API verify; blocks §8.3 design if wrong)
**Evidence.** The fractional-trading page (V-03) and the order-type support table in "Placing Orders" (V-04) both say
fractional `market, limit, stop, stop_limit` are supported with `time_in_force=day` only. The `POST /v2/orders`
reference field text (V-05) still says `qty` is "fractionable for only market and day order types".
**Impact.** §8.3 relies on fractional Day stops. If the API rejects them, the pre-open re-arm cannot exist and the
naked-window story changes.
**Proposal.** Treat the fractional-trading page as authoritative and run the ADR-0013 empirical probe in Slice 3 with a
single fractional Day stop on the paper account. Contingency if rejected: the risk desk restricts entries to whole-share
lots (price ≤ position size), which at $500 means a ≤ $150 share price cap; §8.3 would then need a v2.4 line.
**Status (2026-09-18).** The probe is built (`tradeagent probe-fractional-stop`, refuses to run without
`TRADEAGENT_PROBE_CONFIRM=yes` and paper keys; buys 0.5 share, submits a far Day stop, polls its status, cancels, and
prints the trace). It has not run because no paper keys were present in the development environment. Until it does,
`StopArmer` submits fractional Day stops as designed and any broker rejection surfaces as a `stop_coverage` incident.
**Decision needed:** none now; the owner is informed of the contingency.

### OI-02 — Extended-hours scope: the overnight session (underspecified) — **ACCEPTED (A-02)**
**Evidence.** Alpaca now offers an overnight session 8 pm–4 am ET on Blue Ocean ATS (V-03, V-04), and `extended_hours=true`
Day limit orders are eligible for it. §4.3 says "extended hours for exits only" and its guard is anchored to an IEX quote,
but IEX does not trade overnight, so the guard cannot be satisfied there.
**Proposal (smallest amendment to §4.3).** "Extended hours" means the pre-market (04:00–09:30 ET) and after-hours
(16:00–20:00 ET) sessions only; overnight is excluded. Implemented provisionally as
`extended_hours.sessions_allowed_for_exits: [premarket, after_hours]` and enforced by the config validator.

### OI-03 — `BROKER_POLICY` should include two more account-configuration fields — **ACCEPTED (A-01)**
**Evidence.** The account-configuration object (V-06) exposes `fractional_trading` and `disable_overnight_trading` in
addition to the three fields §8.10 names. The `dtbp_check`/`pdt_check` fields no longer exist (PDT retired, V-01).
**Proposal (amend §8.10 / Appendix B).** `BROKER_POLICY` adds `fractional_trading = true` (the strategy needs it) and
`disable_overnight_trading = true` (consistent with OI-02). Modelled as optional fields on `BrokerPolicy` that are not
enforced until accepted.

### OI-04 — Seven SIC codes in §4.4 do not exist in EDGAR (impossible as written) — **mapping in 09-owner-decisions.md §A**
**Evidence.** V-08: 1241, 1321, 3483, 3489, 3795, 4612, 4613 are absent from the EDGAR SIC list; 3480 covers all
ordnance and ammunition; 4610 is the petroleum pipeline code.
**Proposal (amend §4.4 item 2).** Fossil: 1220, 1221, 1311, 1381, 1382, 1389, 2911, 4610, 4922. Defense: 3480, 3760.
Default-deny additions 3730, 3533, 6792 and review-list addition 2990 as in ADR-0006. Already reflected in
`config/sic_backstop.yaml`; the owner accepts or trims.

### OI-05 — Whole-share lots whose GTC stop was cancelled for an extended-hours exit are unprotected next session — **ACCEPTED (A-03)**
**Evidence.** §14 rule 3 cancels sibling orders before an extended-hours exit; §8.3 re-arms only *fractional* lots.
An unfilled Day extended-hours limit leaves a whole-share lot with no GTC stop at the next open.
**Proposal (amend §8.3).** The pre-open job re-arms **any** lot without a live protective order, and the post-open
verification covers all lots. ADR-0013 implements it that way.

### OI-06 — FINRA TAF rate conflict — **RESOLVED (A-06)**
**Evidence.** FINRA's Schedule A page as rendered on 2026-09-13 shows $0.000166/share, max $8.30; broker fee pages and
FINRA's SR-FINRA-2024-019 fee-adjustment schedule report $0.000195 / $9.79 effective 2026-01-01 (V-10). The SEC
Section 31 rate is unambiguous: $20.60 per million from 2026-04-04 (V-09).
**Resolution.** FINRA's SR-FINRA-2024-019 fee-adjustment schedule (SEC Release 34-101696, approved January 2025) gives
2025 $0.000166/$8.30, 2026 $0.000195/$9.79, 2027 $0.000232/$11.61, 2028 $0.000240/$12.05. The rendered Schedule A page
lags the filing. `config/fees.yaml` now holds effective-dated schedules and `tradeagent/fees.py` selects the row by charge
date (tested). CAT fee row still needs a rate from the owner.

### OI-07 — Seed denylist scope calls (owner review required by §4.4) — **review table in 09-owner-decisions.md §B**
20 entries are flagged `needs_owner_review` (conglomerates BA, RTX, GE, HON, TXT; defense IT/services LDOS, CACI, SAIC,
BAH; PLTR, AXON, OLN, and names affected by recent mergers). Decision: keep, drop, or add per D-14.

### OI-08 — Which experiment do the benchmarks anchor to? (underspecified)
§12 buys SPY/VTI "at the experiment start close". DRY_RUN and PAPER are different experiments (`experiments.id`); the
package anchors each experiment's benchmarks to the close of its own `started_on`. Confirm, or specify that PAPER
inherits DRY_RUN's anchor.

### OI-09 — Budget proration and carry-over (underspecified)
§9 says "prorated to a daily allowance" and names `LLM_ENTRY_BUCKET_PCT` without a value. Proposed: daily allowance =
cap ÷ trading days in the calendar month; unspent allowance carries forward within the month, never across months;
`LLM_ENTRY_BUCKET_PCT = 60`. Recorded in `risk_policy.yaml: budget`.

### OI-10 — Triage thresholds have no config key (underspecified)
§6.4 "configurable thresholds" gate LLM triage but Appendix B has no key. Proposed keys `TRIAGE_SCORE_THRESHOLD = 0.60`
and `TRIAGE_MIN_INTERVAL_MIN = 30` (ADR-0008).

### OI-11 — Appendix B keys listed without defaults
`OVERFILL_BUFFER_PCT`, `MIN_HISTORY_DAYS`, `MAX_NEWS_AGE_HOURS`, `FALLBACK_PRICE_TOLERANCE_PCT`,
`EXT_HOURS_MAX_QUOTE_AGE_SEC`, `EXT_HOURS_MAX_CROSS_PCT`, `TRIGGER_INVALIDATION_PROXIMITY_PCT`,
`DATA_UPGRADE_REVIEW_EQUITY_USD`, `DOSSIER_MAX_HEADLINES`, `DOSSIER_TOKEN_CAP`, `MAX_ORDERS_PER_DAY`,
`MAX_LLM_CALLS_PER_HOUR`, `HALT_AFTER_CONSECUTIVE_REJECTS`. Proposed values are in `config/risk_policy.yaml`, each marked
`(proposed)`. Any of them can be changed before Slice 1 without a phase (no experiment exists yet).

### OI-12 — `candidate_outcomes` anchor instant (underspecified)
§10.1 defines forward returns "for every candidate, traded or not". Anchor proposed: the first eligible SIP print after
the *quant baseline's* `order_eligible_at` for that scan (scanner completion + validation), so forward returns are
comparable with QB entries and never look ahead of what any portfolio could have done. Alternative: anchor at
`signal_observed_at` (earlier, slightly optimistic).

### OI-14 — Concurrent BUY validations could over-commit virtual cash — **CLOSED by ADR-0021**
Found in the owner's review. A ledger CHECK only sees money at fill time; two decisions validated together could both pass.
Resolved by atomic per-portfolio reservation under an advisory lock (migration 0009) with a two-connection race test.

### OI-15 — Supabase Data API / Storage exposure — **CLOSED by ADR-0022**
Trading state moved to schema `trading` (not exposed), client roles revoked, RLS deny-by-default, dedicated worker role
without DELETE, private bucket, boot checks. Tested with locally created `anon`/`authenticated` roles.

## B. Verified — no change needed, recorded for the file
- **PDT retirement 2026-06-04 and Alpaca's removal of PDT logic** — confirmed (V-01). §1/§3.1 stand.
- **Under-$2,000 accounts are 1x, no margin, no shorting** — confirmed (V-02).
- **Basic plan: IEX real-time only, SIP after 15 minutes, 200 req/min, 30 WebSocket symbols, $99 Algo Trader Plus**
  — confirmed (V-07). The spec's "Alpaca says the free plan is for testing" sentence no longer appears in the plan
  page, which now calls Basic "the default option for both Paper and Live trading accounts"; harmless, noted.
- **Extended-hours orders must be `limit` with `day` or `gtc` and `extended_hours=true`; stops/trailing stops do not
  trigger outside regular hours; brackets not in extended hours** — confirmed (V-04). The spec's Day-only choice is compliant.
- **Fractional orders: Day only; no bracket/OCO on fractional** — confirmed by the support table (V-04), subject to OI-01.
- **`client_order_id` max 128 characters** — confirmed (V-05); `decision_id:leg_seq` is 38 characters.
- **Account-configuration fields `max_margin_multiplier ("1"|"2"|"4")`, `no_shorting`, `max_options_trading_level (0–3)`**
  — confirmed (V-06).
- **Paper trading does not simulate regulatory fees, dividends, or latency slippage; partial fills happen randomly 10% of
  the time; paper accounts are now created/deleted rather than reset** — confirmed (V-12). §9 and §15 stand; the boot
  policy re-apply covers a fresh paper account; a new paper account means new API keys (owner action).
- **Alpaca historical bars `feed` values are `sip | iex | boats | otc` (no `delayed_sip` for REST); requesting SIP within
  the last 15 minutes on Basic returns "subscription does not permit querying recent SIP data"** — confirmed (V-13,
  V-14). The scanner must always pass an explicit `end ≤ now − 15 min`.
- **`v2/delayed_sip` WebSocket stream exists** — confirmed (V-14); not used in V1 (ADR-0015).
- **Corporate actions endpoint `GET /v1/corporate-actions` with splits, dividends, mergers, name changes, delisting-type
  events** — confirmed (V-15). The older `/v2/corporate_actions/announcements` is deprecated.
- **Account activities: `FILL` plus `FEE` sub-types `REG`, `TAF`, `CAT`, dividends `DIV*`, `SPLIT`, `NC`** — confirmed
  (V-16); reconciliation replays `FILL` by activity id; on LIVE, `FEE` activities replace estimates.
- **EDGAR APIs (submissions, companyfacts) need no key; fair-access policy applies** — confirmed (V-17).
- **Finnhub free tier 60 calls/min; earnings calendar available on the free tier** — reported by documentation and
  secondary sources (V-11); confirm with a key in Slice 2.
- **Claude model IDs and prices** — confirmed (V-18): `claude-haiku-4-5` $1/$5, `claude-sonnet-5` $2/$10 (standard),
  `claude-opus-5` $5/$25; cache reads at 0.1×.

## C. Internal consistency checks on the spec (no contradictions found that block implementation)
- §7.3 "critique on every decision-model output" and §7.5 "daily review on the triage model" are consistent: daily
  reviews are not decision-model outputs and get no critique; triggered reviews do.
- §8.4 lists `PROPOSED → VALIDATED → … → FILLED` but Alpaca has `accepted`, `pending_new`, `held`, `replaced`,
  `done_for_day`: the adapter maps them (accepted/pending_new/new → SUBMITTED; replaced → CANCELLED with reason
  `replaced`; done_for_day/expired → EXPIRED). Not a contradiction; recorded in `05-schema.md`.
- §8.3 "client_order_id derived from decision_id" and cancel-and-replace (several orders per decision): resolved as
  `decision_id:leg_seq`, unique per leg, still derived.
- §2 "No deposits or withdrawals initiated by the system": the Trading API used here has no funding endpoints; trivially
  satisfied and asserted by a grep test (T-20).
- §17 "virtual-cash cap test against a paper account whose multiplier and balance exceed the experiment's": the paper
  account default is $100k at 2x/4x, so this test is natural in Slice 3.

## D. Repository placement (process, not spec) — OI-13, **actioned**
This package was placed in `mermellla/agency-agents`, a fork of a public collection of agent-persona Markdown files
with its own CI (`lint-agents.yml`, scoped to the persona directories, so it does not run on these files). The trading
system now lives alongside under `docs/`, `config/`, `supabase/`, `src/`, `tests/`. The package was rebuilt as a standalone repository with fresh history (Phase 0 checkpoint commit + review-round commit),
re-verified there (migrations, tests, ruff, mypy, detect-secrets), and delivered as a git bundle and as the orphan branch
`trading-agent-clean`; the GitHub App in this session is not permitted to create repositories, so the owner pushes it to a
new private repository (`docs/phase0/10-repository-move.md`).
