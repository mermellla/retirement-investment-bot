# Test plan and test matrix

Levels: **DB** (schema under test, no app code), **Unit** (pure Python), **Contract** (adapter against recorded or mocked
HTTP), **Integration** (worker components against the local database with mocked adapters), **Paper** (against the
Alpaca paper API with paper keys, owner-run, DRY_RUN observe-only or PAPER). **Pre-impl** = writable before feature code
exists; ✔ = already written and passing (64 tests in `tests/`).

## §17 deliverables → tests
| T | Deliverable / invariant (§17, §10.2) | Level | Pre-impl | Test(s) | Slice |
|---|---|---|---|---|---|
| T-01 | Virtual-cash cap: order above virtual cash rejected although the broker would accept it | DB ✔ + Integration + Paper | ✔ DB | test_reservation_required_and_bounded ✔ ($600 order on $500 refused at insert); `test_risk_desk_virtual_cash_cap` (mock broker reporting $100k BP); paper run in S3 | S4 |
| T-01c | Concurrency: two simultaneous BUY validations cannot collectively reserve more than available; reservation released on cancel/reject/expiry, converted on fill (ADR-0021) | DB | ✔ | tests/test_reservation.py (7) incl. test_concurrent_buys_cannot_over_reserve ✔ | P0 |
| T-02 | Position limits 40 / 10–30 / 5; overfill guard | Unit | yes | `test_sizing_limits`, `test_overfill_buffer_whole_share` | S4 |
| T-03 | Horizon bounds and latency floor rejections | Unit | yes | `test_horizon_bounds`, `test_latency_floor_rejects_under_30min` | S4 |
| T-04 | Executed order never differs from validated decision; idempotent resubmission | DB + Unit | ✔ | test_sell_entry_order_forbidden ✔, test_order_eligibility_must_match_decision ✔; test_submit_new_then_idempotent_resubmit ✔, test_submit_race_422_resolves_to_existing_order ✔, test_simulated_orders_never_reach_the_broker ✔ | S3 ✔ |
| T-05 | Budget ledger: exhaust entry bucket → NO_ACTION for entries, exits still run, `BUDGET_OVERAGE_EXIT` logged | Integration | yes (mock LLM cost) | `test_budget_entry_exhaustion_blocks_entries_not_exits` | S4 |
| T-06 | Exclusion layer: seed denylist + SIC backstop; excluded name rejected at scanner prefilter **and** risk desk | Unit + Integration | ✔ screen + prefilter; desk pending | tests/test_exclusions.py ✔; tests/test_slice2_universe.py ✔ (XOM excluded, default-deny skipped, SPAC universe-excluded); `test_risk_desk_rejects_excluded_even_if_proposed` | S2 ✔ / S4 |
| T-07 | `ETHICAL_HOLD`: list update catches a held name → no ADD, REDUCE/SELL allowed, no re-entry | Integration | yes | `test_ethical_hold_semantics` | S4 |
| T-08 | Scanner embargo: SIP adapter refuses `end > now−15m`; `signal_observed_at` = retrievable time; 15-min cadence request budget | Unit + Contract ✔ | ✔ | tests/test_slice2_market_data.py (embargo, retrievable_at, pagination, budget, plan flip, IEX quotes) ✔; tests/test_slice2_scan_db.py (observed_at ≥ bar time and ≥ scan start) ✔ | S2 ✔ |
| T-09 | Signals: tier-aware availability; dropped signals recorded as `signal_unavailable_reason`; composite formula fixtures | Unit ✔ | ✔ | tests/test_slice2_signals.py (momentum/RSI/ATR/breakout on synthetic series, tier availability, z-scores, composite renormalisation and penalties, strategy tags) ✔ | S2 ✔ |
| T-10 | Reconciliation: replay repairs a missed fill; unknown ticker halts; excluded-name position halts; manual trade halts; duplicate broker events idempotent | Integration | yes (mock activities) | test_replay_repairs_missed_fill ✔, test_unknown_ticker_halts ✔, test_excluded_name_position_halts ✔, test_manual_trade_halts ✔, test_broker_cancelled_order_refreshed_and_dry_run_flat_agrees ✔, test_apply_fill_buy_reduce_close_with_fees ✔; test_duplicate_broker_fill_rejected ✔ | S3 ✔ |
| T-11 | Source registry: failover within a domain; bars/quotes down halts entries; `source_status` stamped on every scan | Unit ✔ | ✔ | tests/test_slice2_catalyst_registry.py::test_registry_failover_and_entry_halt ✔; scans carry `source_status` and `entries_halted` ✔ | S2 ✔ |
| T-12 | Trust grades: research-grade price never used for sizing/stops/staleness | Unit | yes | `test_risk_desk_ignores_research_grade_prices` | S4 |
| T-13 | Broker policy: matching config proceeds; mismatch written and re-read; unreadable halts | Integration + Paper | yes (mock broker) | test_policy_match_apply_and_halts ✔ (match / applied and re-read / unreadable halts / write refused halts / enforcement off + mismatch halts); test_broker_policy_match (model) ✔ | S3 ✔ |
| T-14 | Forecast resolution on synthetic SIP data: target first; invalidation first; time stop; both-in-one-minute resolved by ticks; both with no ticks → AMBIGUOUS; immutability | Unit + DB | yes (synthetic bars/ticks) | `test_resolve_target_first`, `test_resolve_invalidation_first`, `test_resolve_time_stop`, `test_resolve_both_touch_by_ticks`, `test_resolve_both_touch_no_ticks_ambiguous`; test_working_levels_can_move… ✔, test_forecast_contract_immutable ✔ | S7 |
| T-14b | Fill model: ask/bid with no added spread; trade fallback ± half-spread; `SIM_ADDITIONAL_SLIPPAGE_BPS` separate and labeled; reconstruction picks first eligible print after eligibility; no print → expires | Unit + DB | ✔ DB rules; unit pending | test_spread_double_count_rejected ✔, test_fill_spread_rules ✔; `test_reconstruct_ask_fill`, `test_reconstruct_trade_fallback`, `test_reconstruct_slippage_separate`, `test_reconstruct_first_eligible_print`, `test_reconstruct_expires_without_print` | S4 |
| T-15 | Staleness: `REJECTED_STALE_DATA` on stale SIP bar / IEX trade; fallback price tolerance | Unit | yes | `test_staleness_rejections`, `test_fallback_tolerance` | S4 |
| T-16 | Pre-open stop re-arm with post-open verification; lot-without-stop → software stop + incident | Integration + Paper probe | yes (mock broker statuses accepted/new) | test_rearm_every_unprotected_lot_fractional_or_not ✔, test_post_open_verification_live_pending_and_software_stop ✔, test_post_open_missing_above_invalidation_rearms_and_monitors ✔, test_gap_below_invalidation_sells_at_open ✔, test_dry_run_records_intent_without_submitting ✔; paper probe (ADR-0013) waits for keys | S3 ✔ (probe pending) |
| T-17 | Runaway guards: max orders/day, LLM calls/hour, consecutive rejects halt; every halt emails | Integration | yes | `test_guards_halt_and_email` | S4 |
| T-18 | Early-warning stream: focus-set cap (positions before candidates, hysteresis); disconnect → REST fallback + gap logged | Unit + Integration | yes | `test_focus_set_cap_positions_first`, `test_stream_disconnect_fallback` | S6 |
| T-19 | `MARKET_DATA_PLAN` switch: mocked real-time SIP adapter changes no scanner code | Unit ✔ | ✔ | test_plan_flip_selects_tiers ✔ (adapter half); test_scanner_modules_import_no_market_data_adapter ✔ (scanner modules import no concrete adapter) | S2 ✔ |
| T-20 | No secrets, no live URL, no funding endpoint, no live key env read in the codebase | Unit (grep) | ✔ | test_no_live_or_secret_strings ✔ | P0 |
| T-55 | Supabase exposure: `anon`/`authenticated` cannot read, write or call anything in `trading`; worker role cannot DELETE; `public` empty; boot checks for Data-API exposure and bucket privacy (ADR-0022) | DB ✔ + Integration ✔ | ✔ | tests/test_security.py (4) ✔; tests/test_slice1_checks.py (exposed → halt, 401/404 → pass, public bucket → halt, missing → created private, keys missing → halt) ✔ | P0 / S1 ✔ |
| T-57 | Jev judgment engine (ADR-0023): typed answers, accounting rows, cost when priced, unavailable without key/on error; catalyst classifier Jev path, keyword fallback, `jev_down` marking | Unit ✔ | ✔ | tests/test_jev.py (3), tests/test_slice2_catalyst_registry.py (3) ✔ | S2 ✔ |
| T-56 | Fee engine: effective-dated schedule selection, per-trade cap, unknown date raises (ADR-0012, A-06) | Unit | ✔ | tests/test_fees.py (3) ✔ | P0 |
| T-21 | No self-modification: worker has no write path to prompts/config; boot halts on version drift without a fresh justification; reused version string with different content refused | Integration ✔ | ✔ | tests/test_slice1_boot.py (config change without reason halts; with reason opens phase; code change; stale sequence; VERSION_REUSED) | S1 ✔ |
| T-22 | `settles_on` = T+1 from the calendar; not used for eligibility | Unit | yes | reconcile passes `settles_on` through `apply_fill` as metadata only (test_replay_repairs_missed_fill ✔); calendar T+1 arithmetic test_add_trading_days ✔ | S3 ✔ |
| T-23 | Prompt v1 states every §7.7 constraint; strict JSON; invalid output → `REJECTED_INVALID_OUTPUT` | Unit | yes (golden prompt test) | `test_prompt_constraints_present`, `test_invalid_output_rejected` | S5 |
| T-24 | Regime classifier fixtures for all seven outcomes | Unit ✔ | ✔ | test_regime_classifier_v1 (5 parametrised) + test_regime_risk_off_and_unknown ✔ | S2 ✔ |
| T-25 | Pipeline timestamps stamped in order; `llm_calls` rows with cost per call from `usage` | Integration (mock LLM) | yes | `test_pipeline_timeline_and_costs` | S5 |
| T-26 | Dossier: token cap, provenance tags, drift fields, slim vs full | Unit | yes | `test_dossier_composition` | S5 |
| T-27 | Reviews: daily on triage model; triggers (proximity, news, time stop, earnings, regime); trigger priority | Integration | yes | `test_daily_review_uses_triage_model`, `test_triggered_review_priority` | S6 |
| T-28 | Exits before entries; broker stop wins; siblings cancelled; rotation sells first | Integration | yes | `test_cycle_order_exits_first`, `test_broker_stop_final`, `test_sibling_cancel_before_exit`, `test_rotation_sequence` | S6 |
| T-29 | Analysis job: §13.3 report with block-bootstrap intervals, quintile calibration, per-phase counts, both P&L views, ADR-0016 estimator | Unit (synthetic cohort) | yes | `test_report_sections_present`, `test_block_bootstrap_weekly`, `test_calibration_quintiles`, `test_two_pnl_views` | S7 |
| T-30 | Intraday setups: ORB variants and gap continuation definitions on synthetic 15-min bars | Unit ✔ | ✔ | test_orb_sip_confirmed_and_not, test_orb_iex_assisted_requires_fresh_tight_quote, test_gap_continuation ✔ (same-day time stop enforced by the risk desk, S4) | S2 ✔ |
| T-31 | Extended-hours exit: Day limit anchored to fresh IEX quote, bounded cross, no quote → wait; overnight never | Unit + DB | ✔ DB; unit pending | test_ext_hours_exit_must_be_day_limit ✔; `test_ext_hours_anchor_and_cross`, `test_ext_hours_waits_without_quote` | S6 |
| T-32 | QB-1.0: rules, `order_eligible_at` = scan completion + validation, no drift judgment | Integration | yes | `test_qb_rules_v1`, `test_qb_eligibility_earlier_than_llm` | S4 |
| T-33 | Fees and dividends applied in every mode; two P&L views | Unit | yes | `test_fee_engine_schedule`, `test_dividend_credit` | S4 |
| T-34 | Context blob stored, hash permanent, prune after 90 days touches only the blob | Integration (mock storage) | yes | `test_context_prune` | S5 |
| T-35 | Scheduler follows the Alpaca calendar (holiday, half-day) | Unit ✔ | ✔ | tests/test_slice1_checks.py (calendar parse, T+1 over a holiday, half-day scan count, holiday skip) | S1 ✔ |
| T-36 | Daily digest email content and notification audit row | Unit | yes | `test_daily_digest` | S4 |
| T-37 | Approval-link flow with expiry (LIVE only) | Integration | yes | `test_approval_link_expiry` | S9 (gated) |
| T-38 | Benchmarks: SPY/VTI bought at start close, marked daily; cash flat | Unit | yes | `test_benchmarks` | S4 |
| T-39 | Shadows: critique-changed proposal spawns paired + parallel entries; discretionary exit leaves the deterministic twin running | Integration | yes | `test_critique_shadow_spawn`, `test_det_exit_twin_independent` | S6 |
| T-40..52 | §15 edge cases: holidays/half-days; halted stock (no repricing); split/symbol change before re-arm; delisting force-close; IPO exclusion; `REJECTED_BROKER` never resized; Supabase outage full halt; Alpaca outage entries-only halt; crash mid-order; crash with pending reconstruction resumes; default-deny after the fact; $2,000 crossing keeps 1x; paper reset re-applies policy | Integration | yes | one test per case; delivered: split/symbol change before re-arm (test_corporate_actions_split_and_symbol_change ✔); the rest arrive with S4–S6 | S3–S6 |
| T-53 | Candidate outcomes: every candidate, traded or not, gets forward returns at each horizon | Integration | yes | `test_candidate_outcomes_complete` | S7 |
| T-54 | Phase test: version bump opens a phase; subsequent decisions carry it | DB ✔ + Integration ✔ | ✔ | test_prompt_bump_opens_new_phase_and_decisions_carry_it ✔; test_config_change_with_reason_opens_phase_and_decisions_carry_it ✔ | S1 ✔ |

## Already written and passing (130)
- `tests/test_migrations.py` (3): all §10.1 tables exist; RLS everywhere; Postgres enums == Python enums.
- `tests/test_db_invariants.py` (39, incl. 7 parametrized no-delete cases): every schema-enforced invariant listed in `05-schema.md`.
- `tests/test_slice2_*.py` (30) and `tests/test_jev.py` (3): Slice 2 market data, universe, signals/regime/composite, catalyst + registry + import graph, DB end-to-end scan.
- `tests/test_slice1_boot.py` (9) and `tests/test_slice1_checks.py` (9): Slice 1 boot spine, exposure checks, NullBroker contract, calendar, scheduler.
- `tests/test_reservation.py` (7): ADR-0021 reservation bounds, release, conversion, no growth, sells reserve nothing, two-connection race.
- `tests/test_security.py` (4): ADR-0022 client roles denied, worker cannot delete, `public` empty.
- `tests/test_fees.py` (3): effective-dated fee schedules.
- `tests/test_config.py` (8): spec defaults, five-field broker policy, model IDs, LIVE refused, config hash, versions, fee schedules, exclusions schema, SIC codes real.
- `tests/test_models.py` (8): proposal/timeline/order/fill/broker-policy validation.
- `tests/test_exclusions.py` (5): both enforcement paths of the screen.
- `tests/test_lockout_grep.py` (1): T-20 — no live URL, live key names, funding endpoints, or secret-looking literals in code/config.

## Running
```
pg_ctlcluster 16 main start   # or any PostgreSQL 16 with createdb rights
export TRADEAGENT_TEST_ADMIN_URL='postgresql://<admin-user>:<password>@localhost:5432/postgres'
python -m pytest -q
```
DB tests create a throw-away database per session, apply every migration, and run each test in a rolled-back transaction.
