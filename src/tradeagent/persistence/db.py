"""Database access for the worker (ADR-0002, ADR-0022). One psycopg connection, `search_path = trading, public`,
explicit transactions. No DELETE statements exist anywhere in this package; the worker role could not run them anyway."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import psycopg.types.json
from psycopg.rows import dict_row

from tradeagent.domain.enums import (
    BrokerPolicyResult,
    ExecutionMode,
    HaltScope,
    OrderStatus,
    PortfolioKind,
    ReconcileResult,
)
from tradeagent.domain.models import Instrument, LLMCall, OrderRequest

# YAML config carries dates; jsonb payloads serialise them as ISO strings.
psycopg.types.json.set_json_dumps(lambda obj: json.dumps(obj, default=str, sort_keys=True))


def canonical_json(content: dict[str, Any]) -> dict[str, Any]:
    """The form a jsonb payload has after a database round-trip (dates → ISO strings)."""
    result: dict[str, Any] = json.loads(json.dumps(content, default=str, sort_keys=True))
    return result


REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS_DIR = REPO_ROOT / "supabase" / "migrations"

# The newest migration's sentinel object: boot refuses to run against a schema that lacks it (§10.5, ADR-0019).
SCHEMA_SENTINEL: tuple[str, str, str] = ("20260913000011", "fees", "classification")

PORTFOLIO_SET: tuple[tuple[PortfolioKind, str, bool], ...] = (
    (PortfolioKind.LLM_PRIMARY, "primary", True),
    (PortfolioKind.QUANT_SHADOW, "qb-1.0", False),
    (PortfolioKind.BENCHMARK_CASH, "benchmark-cash", False),
    (PortfolioKind.BENCHMARK_SPY, "benchmark-spy", False),
    (PortfolioKind.BENCHMARK_VTI, "benchmark-vti", False),
    (PortfolioKind.CRITIQUE_SHADOW_PAIRED, "critique-paired", False),
    (PortfolioKind.CRITIQUE_SHADOW_PARALLEL, "critique-parallel", False),
    (PortfolioKind.DETERMINISTIC_EXIT_SHADOW, "deterministic-exit", False),
)
"""§12: every portfolio that runs from day one. Only the primary touches the broker (schema-enforced)."""


def connect(database_url: str) -> psycopg.Connection[dict[str, Any]]:
    conn = psycopg.connect(database_url, row_factory=dict_row, autocommit=False)
    conn.execute("set search_path = trading, public")
    return conn


def latest_migration_version() -> str:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        raise RuntimeError("no migrations found")
    return files[-1].name.split("_", 1)[0]


class Database:
    """Slice 1 repository surface: bootstrap, versions, phases, halts, cash. Grows by slice (see `Ledger` Protocol)."""

    def __init__(self, conn: psycopg.Connection[dict[str, Any]]):
        self.conn = conn

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self.conn.transaction():
            yield

    # ---- schema
    def schema_is_current(self) -> bool:
        _, table, column = SCHEMA_SENTINEL
        row = self.conn.execute(
            "select 1 from information_schema.columns where table_schema = 'trading' and table_name = %s and column_name = %s",
            (table, column),
        ).fetchone()
        return row is not None

    # ---- experiment and portfolios (§3.2, §12)
    def get_experiment(self, name: str) -> dict[str, Any] | None:
        return self.conn.execute("select * from experiments where name = %s", (name,)).fetchone()

    def create_experiment(
        self, name: str, mode: ExecutionMode, equity_start: Decimal, started_on: date
    ) -> dict[str, Any]:
        if mode == ExecutionMode.LIVE:  # belt and braces: the schema refuses it too (ADR-0020)
            raise RuntimeError("LIVE_LOCKED_OUT")
        row = self.conn.execute(
            "insert into experiments (name, execution_mode, equity_start_usd, started_on) values (%s, %s, %s, %s) returning *",
            (name, mode.value, equity_start, started_on),
        ).fetchone()
        assert row is not None
        return row

    def portfolios(self, experiment_id: UUID) -> list[dict[str, Any]]:
        return self.conn.execute(
            "select * from portfolios where experiment_id = %s order by name", (experiment_id,)
        ).fetchall()

    def ensure_portfolios(self, experiment_id: UUID, equity_start: Decimal) -> list[dict[str, Any]]:
        existing = {p["name"] for p in self.portfolios(experiment_id)}
        for kind, name, touches_broker in PORTFOLIO_SET:
            if name not in existing:
                self.conn.execute(
                    "insert into portfolios (experiment_id, kind, name, touches_broker, equity_start_usd) values (%s, %s, %s, %s, %s)",
                    (experiment_id, kind.value, name, touches_broker, equity_start),
                )
        return self.portfolios(experiment_id)

    def ensure_initial_equity(
        self, experiment_id: UUID, phase_id: UUID, portfolio: dict[str, Any], at: datetime
    ) -> bool:
        """Book the §3.2 opening balance once per portfolio; idempotent by key. Returns True when a row was written."""
        key = f"initial_equity:{portfolio['id']}"
        exists = self.conn.execute("select 1 from cash_ledger where idempotency_key = %s", (key,)).fetchone()
        if exists:
            return False
        self.conn.execute(
            """insert into cash_ledger (portfolio_id, experiment_id, experiment_phase_id, at, kind, amount_usd, balance_after_usd, idempotency_key, reason)
               values (%s, %s, %s, %s, 'initial_equity', %s, %s, %s, 'EXPERIMENT_EQUITY_START')""",
            (
                portfolio["id"],
                experiment_id,
                phase_id,
                at,
                portfolio["equity_start_usd"],
                portfolio["equity_start_usd"],
                key,
            ),
        )
        return True

    def cash_balance(self, portfolio_id: UUID) -> Decimal:
        row = self.conn.execute("select portfolio_cash_balance(%s) as b", (portfolio_id,)).fetchone()
        assert row is not None
        return Decimal(row["b"])

    def available_cash(self, portfolio_id: UUID) -> Decimal:
        row = self.conn.execute("select portfolio_available_cash(%s) as b", (portfolio_id,)).fetchone()
        assert row is not None
        return Decimal(row["b"])

    def verify_cash_chain(self, portfolio_id: UUID) -> bool:
        """ADR-0002 projection check: every row's balance equals the previous balance plus its amount."""
        rows = self.conn.execute(
            "select amount_usd, balance_after_usd from cash_ledger where portfolio_id = %s order by id", (portfolio_id,)
        ).fetchall()
        prev = Decimal(0)
        for r in rows:
            if Decimal(r["balance_after_usd"]) != prev + Decimal(r["amount_usd"]):
                return False
            prev = Decimal(r["balance_after_usd"])
        return True

    # ---- version registries (ADR-0019)
    def register_version(
        self, table: str, version: str, content: dict[str, Any], content_hash: str, extra: dict[str, Any] | None = None
    ) -> None:
        """Insert if absent; refuse a reused version string with different content (VERSION_REUSED)."""
        hash_col = "content_hash" if table in ("prompt_versions", "exclusion_list_versions") else None
        row = self.conn.execute(f"select * from {table} where version = %s", (version,)).fetchone()  # noqa: S608 (table from a fixed set)
        if row is not None:
            stored = row[hash_col] if hash_col else row["content"]
            current = content_hash if hash_col else canonical_json(content)
            if stored != current:
                raise RuntimeError(f"VERSION_REUSED: {table} {version} already registered with different content")
            return
        extra = extra or {}
        if table == "prompt_versions":
            self.conn.execute(
                "insert into prompt_versions (version, content_hash, changelog) values (%s, %s, %s)",
                (version, content_hash, extra.get("changelog", "")),
            )
        elif table == "exclusion_list_versions":
            self.conn.execute(
                "insert into exclusion_list_versions (version, content_hash, content, entry_count) values (%s, %s, %s, %s)",
                (version, content_hash, psycopg.types.json.Jsonb(content), extra.get("entry_count", 0)),
            )
        else:
            self.conn.execute(
                f"insert into {table} (version, content) values (%s, %s)",  # noqa: S608
                (version, psycopg.types.json.Jsonb(content)),
            )

    # ---- phases (§13.2)
    def open_phase(self, experiment_id: UUID) -> dict[str, Any] | None:
        return self.conn.execute(
            "select * from experiment_phases where experiment_id = %s and ended_at is null", (experiment_id,)
        ).fetchone()

    def close_phase(self, phase_id: UUID, at: datetime) -> None:
        self.conn.execute("update experiment_phases set ended_at = %s where id = %s", (at, phase_id))

    def insert_phase(self, experiment_id: UUID, fields: dict[str, Any]) -> dict[str, Any]:
        seq_row = self.conn.execute(
            "select coalesce(max(seq), 0) + 1 as seq from experiment_phases where experiment_id = %s", (experiment_id,)
        ).fetchone()
        assert seq_row is not None
        cols = {"experiment_id": experiment_id, "seq": seq_row["seq"], **fields}
        cols["what_changed"] = psycopg.types.json.Jsonb(cols.get("what_changed", {}))
        keys = list(cols)
        row = self.conn.execute(
            f"insert into experiment_phases ({', '.join(keys)}) values ({', '.join(['%s'] * len(keys))}) returning *",  # noqa: S608
            [cols[k] for k in keys],
        ).fetchone()
        assert row is not None
        return row

    # ---- halts (§8.7)
    def record_halt(self, experiment_id: UUID | None, code: str, scope: HaltScope, detail: dict[str, Any]) -> UUID:
        row = self.conn.execute(
            "insert into halts (experiment_id, code, scope, detail) values (%s, %s, %s, %s) returning id",
            (experiment_id, code, scope.value, psycopg.types.json.Jsonb(detail)),
        ).fetchone()
        assert row is not None
        return UUID(str(row["id"]))

    def open_halts(self, experiment_id: UUID | None) -> list[dict[str, Any]]:
        return self.conn.execute(
            "select * from halts where cleared_at is null and (experiment_id = %s or experiment_id is null) order by at",
            (experiment_id,),
        ).fetchall()

    # ---- reconciliation and notifications
    def record_reconciliation(
        self, experiment_id: UUID, mode: ExecutionMode, result: ReconcileResult, diff: dict[str, Any], notes: str
    ) -> None:
        self.conn.execute(
            "insert into reconciliations (experiment_id, execution_mode, result, diff, notes) values (%s, %s, %s, %s, %s)",
            (experiment_id, mode.value, result.value, psycopg.types.json.Jsonb(diff), notes),
        )

    def record_notification(
        self, kind: str, recipient: str, subject: str, provider_message_id: str, status: str, payload: dict[str, Any]
    ) -> None:
        self.conn.execute(
            "insert into notifications (kind, recipient, subject, provider_message_id, status, payload) values (%s, %s, %s, %s, %s, %s)",
            (kind, recipient, subject, provider_message_id, status, psycopg.types.json.Jsonb(payload)),
        )

    # ---- market group (§6.4, §10.1) — Slice 2
    def upsert_instruments(self, instruments: list[Instrument], exclusion_list_version: str) -> None:
        for i in instruments:
            self.conn.execute(
                """insert into instruments (symbol, name, exchange, cik, sic, tradable, fractionable, universe_status, status_reason, exclusion_list_version, last_10k_filed_on, as_of)
                   values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                   on conflict (symbol) do update set name = excluded.name, exchange = excluded.exchange, cik = excluded.cik, sic = excluded.sic,
                     tradable = excluded.tradable, fractionable = excluded.fractionable, universe_status = excluded.universe_status,
                     status_reason = excluded.status_reason, exclusion_list_version = excluded.exclusion_list_version,
                     last_10k_filed_on = excluded.last_10k_filed_on, as_of = now()""",
                (
                    i.symbol,
                    i.name,
                    i.exchange,
                    i.cik,
                    i.sic,
                    i.tradable,
                    i.fractionable,
                    i.universe_status.value,
                    i.status_reason,
                    exclusion_list_version,
                    i.last_10k_filed_on,
                ),
            )

    def upsert_regime(self, trade_date: date, regime: str, scanner_version: str, inputs: dict[str, Any]) -> UUID:
        row = self.conn.execute(
            """insert into regimes (trade_date, regime, scanner_version, inputs) values (%s, %s, %s, %s)
               on conflict (trade_date, scanner_version) do update set regime = excluded.regime, inputs = excluded.inputs, computed_at = now() returning id""",
            (trade_date, regime, scanner_version, psycopg.types.json.Jsonb(inputs)),
        ).fetchone()
        assert row is not None
        return UUID(str(row["id"]))

    def insert_source_status(self, stamps: list[dict[str, Any]]) -> None:
        for s in stamps:
            self.conn.execute(
                "insert into source_status (domain, source, grade, health, detail) values (%s, %s, %s, %s, %s)",
                (s["domain"], s["source"], s["grade"], s["health"], psycopg.types.json.Jsonb(s)),
            )

    def persist_scan(
        self,
        out: Any,
        experiment_id: UUID,
        phase_id: UUID,
        source_status: list[dict[str, Any]],
        entries_halted: list[str],
    ) -> UUID:
        r = out.result
        regime_id = self.upsert_regime(r.started_at.date(), r.regime.value, r.scanner_version, out.regime_inputs)
        self.insert_source_status(source_status)
        self.conn.execute(
            """insert into scans (id, experiment_id, experiment_phase_id, kind, feed_tier, started_at, scanner_completed_at, bars_end_at, regime_id, scanner_version,
               exclusion_list_version, universe_size, candidate_count, source_status, signals_unavailable)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                r.scan_id,
                experiment_id,
                phase_id,
                r.kind,
                r.feed_tier.value,
                r.started_at,
                r.scanner_completed_at,
                r.bars_end_at,
                regime_id,
                r.scanner_version,
                r.exclusion_list_version,
                r.universe_size,
                len(r.candidates),
                psycopg.types.json.Jsonb({"sources": source_status, "entries_halted": entries_halted}),
                psycopg.types.json.Jsonb(r.signals_unavailable),
            ),
        )
        for inst in out.memberships:
            self.conn.execute(
                "insert into universe_memberships (scan_id, symbol, status, reason) values (%s, %s, %s, %s) on conflict do nothing",
                (r.scan_id, inst.symbol, inst.universe_status.value, inst.status_reason),
            )
        for c in r.candidates:
            sig = dict(out.signals.get(c.symbol, {}))
            self.conn.execute(
                """insert into candidates (id, scan_id, symbol, rank, composite_score, signals, strategy_tags, signal_bar_time, signal_observed_at, sip_signal_price,
                   sip_signal_timestamp, iex_price_at_scan, iex_quote_age_sec_at_scan) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    c.candidate_id,
                    c.scan_id,
                    c.symbol,
                    c.rank,
                    c.composite_score,
                    psycopg.types.json.Jsonb(sig),
                    c.strategy_tags,
                    c.signal_bar_time,
                    c.signal_observed_at,
                    c.sip_signal_price,
                    c.sip_signal_timestamp,
                    c.iex_price_at_scan,
                    c.iex_quote_age_sec_at_scan,
                ),
            )
        return UUID(str(r.scan_id))

    def insert_llm_call(
        self, call: LLMCall, experiment_id: UUID, phase_id: UUID, prompt_version: str, decision_id: UUID | None = None
    ) -> None:
        self.conn.execute(
            """insert into llm_calls (experiment_id, experiment_phase_id, decision_id, stage, bucket, model, prompt_version, tokens_in, tokens_out,
               tokens_cached_read, tokens_cached_write, cost_usd, latency_ms, request_hash, response_hash, status)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                experiment_id,
                phase_id,
                decision_id,
                call.stage.value,
                call.bucket.value,
                call.model,
                prompt_version,
                call.tokens_in,
                call.tokens_out,
                call.tokens_cached_read,
                call.tokens_cached_write,
                call.cost_usd,
                call.latency_ms,
                call.request_hash,
                call.response_hash,
                call.status,
            ),
        )

    # ---- execution group — Slice 3
    def primary_portfolio(self, experiment_id: UUID) -> dict[str, Any]:
        row = self.conn.execute(
            "select * from portfolios where experiment_id = %s and kind = 'llm_primary'", (experiment_id,)
        ).fetchone()
        assert row is not None
        return row

    def open_positions(self, portfolio_id: UUID) -> list[dict[str, Any]]:
        return self.conn.execute(
            "select * from positions where portfolio_id = %s and status = 'open' order by symbol", (portfolio_id,)
        ).fetchall()

    def order_by_client_id(self, client_order_id: str) -> dict[str, Any] | None:
        return self.conn.execute("select * from orders where client_order_id = %s", (client_order_id,)).fetchone()

    def order_by_broker_id(self, broker_order_id: str) -> dict[str, Any] | None:
        return self.conn.execute("select * from orders where broker_order_id = %s", (broker_order_id,)).fetchone()

    def open_broker_orders(self, portfolio_id: UUID) -> list[dict[str, Any]]:
        return self.conn.execute(
            "select * from orders where portfolio_id = %s and not is_simulated and status in ('SUBMITTED', 'PARTIALLY_FILLED') order by created_at",
            (portfolio_id,),
        ).fetchall()

    def protective_orders(self, position_id: UUID) -> list[dict[str, Any]]:
        return self.conn.execute(
            """select * from orders where position_id = %s and purpose in ('protective_stop', 'stop_rearm', 'software_stop') and order_is_open(status) order by created_at""",
            (position_id,),
        ).fetchall()

    def record_order(
        self,
        req: OrderRequest,
        experiment_id: UUID,
        phase_id: UUID,
        position_id: UUID | None,
        reserved_notional: Decimal,
    ) -> UUID:
        row = self.conn.execute(
            """insert into orders (decision_id, portfolio_id, experiment_id, experiment_phase_id, position_id, client_order_id, leg_seq, purpose, symbol, side, order_type,
               time_in_force, qty, notional, limit_price, stop_price, take_profit_price, extended_hours, is_simulated, order_eligible_at, expires_at, reserved_notional_usd)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) returning id""",
            (
                req.decision_id,
                req.portfolio_id,
                experiment_id,
                phase_id,
                position_id,
                req.client_order_id,
                req.leg_seq,
                req.purpose.value,
                req.symbol,
                req.side.value,
                req.order_type.value,
                req.time_in_force.value,
                req.qty,
                req.notional,
                req.limit_price,
                req.stop_price,
                req.take_profit_price,
                req.extended_hours,
                req.is_simulated,
                req.order_eligible_at,
                req.expires_at,
                reserved_notional,
            ),
        ).fetchone()
        assert row is not None
        return UUID(str(row["id"]))

    def transition_order(
        self,
        order_id: UUID,
        to_status: OrderStatus,
        reason: str,
        broker_order_id: str | None = None,
        submitted_at: datetime | None = None,
    ) -> None:
        current = self.conn.execute("select status from orders where id = %s", (order_id,)).fetchone()
        assert current is not None
        if current["status"] == to_status.value:
            if broker_order_id:
                self.conn.execute(
                    "update orders set broker_order_id = coalesce(broker_order_id, %s) where id = %s",
                    (broker_order_id, order_id),
                )
            return
        self.conn.execute(
            "update orders set status = %s, status_reason = %s, broker_order_id = coalesce(%s, broker_order_id), submitted_at = coalesce(%s, submitted_at) where id = %s",
            (to_status.value, reason, broker_order_id, submitted_at, order_id),
        )

    def create_system_decision(
        self,
        experiment_id: UUID,
        phase_id: UUID,
        portfolio_id: UUID,
        symbol: str,
        decision: str,
        position_id: UUID | None,
        reason: str,
        versions: dict[str, str],
        at: datetime,
    ) -> UUID:
        row = self.conn.execute(
            """insert into decisions (experiment_id, experiment_phase_id, portfolio_id, position_id, origin, kind, status, decision, ticker, direction, trigger_reason,
               reason_for_exit_if_existing_position, signal_observed_at, risk_validation_completed_at, order_eligible_at,
               prompt_version, exclusion_list_version, config_version, scanner_version, qb_rules_version)
               values (%s, %s, %s, %s, 'system', 'system_exit', 'validated', %s, %s, 'long', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) returning decision_id""",
            (
                experiment_id,
                phase_id,
                portfolio_id,
                position_id,
                decision,
                symbol,
                reason,
                reason,
                at,
                at,
                at,
                versions["prompt_version"],
                versions["exclusion_list_version"],
                versions["config_version"],
                versions["scanner_version"],
                versions["qb_rules_version"],
            ),
        ).fetchone()
        assert row is not None
        return UUID(str(row["decision_id"]))

    def phase_versions(self, phase_id: UUID) -> dict[str, str]:
        row = self.conn.execute(
            "select prompt_version, exclusion_list_version, config_version, scanner_version, qb_rules_version from experiment_phases where id = %s",
            (phase_id,),
        ).fetchone()
        assert row is not None
        return {k: str(v) for k, v in row.items()}

    def record_stop_coverage(
        self, session_date: date, position_id: UUID, lot_id: UUID | None, order_id: UUID | None, **fields: Any
    ) -> None:
        cols = {
            "session_date": session_date,
            "position_id": position_id,
            "lot_id": lot_id,
            "order_id": order_id,
            **fields,
        }
        keys = list(cols)
        updates = ", ".join(f"{k} = excluded.{k}" for k in keys if k not in ("session_date", "position_id"))
        self.conn.execute(
            f"insert into stop_coverage ({', '.join(keys)}) values ({', '.join(['%s'] * len(keys))}) on conflict (session_date, position_id) do update set {updates}",  # noqa: S608
            [cols[k] for k in keys],
        )

    def record_broker_policy_check(
        self,
        experiment_id: UUID | None,
        mode: ExecutionMode,
        expected: dict[str, Any],
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        result: BrokerPolicyResult,
    ) -> None:
        self.conn.execute(
            "insert into broker_policy_checks (experiment_id, execution_mode, expected, observed_before, observed_after, result) values (%s, %s, %s, %s, %s, %s)",
            (
                experiment_id,
                mode.value,
                psycopg.types.json.Jsonb(expected),
                psycopg.types.json.Jsonb(before) if before is not None else None,
                psycopg.types.json.Jsonb(after) if after is not None else None,
                result.value,
            ),
        )

    def last_reconciled_event_at(self, experiment_id: UUID) -> datetime | None:
        row = self.conn.execute(
            "select max(last_reconciled_event_at) as t from reconciliations where experiment_id = %s and result in ('agree', 'repaired')",
            (experiment_id,),
        ).fetchone()
        return row["t"] if row else None

    def record_reconciliation_full(
        self,
        experiment_id: UUID,
        mode: ExecutionMode,
        result: ReconcileResult,
        diff: dict[str, Any],
        notes: str,
        replayed: int,
        last_event_at: datetime | None,
    ) -> None:
        self.conn.execute(
            "insert into reconciliations (experiment_id, execution_mode, result, events_replayed, last_reconciled_event_at, diff, notes) values (%s, %s, %s, %s, %s, %s, %s)",
            (experiment_id, mode.value, result.value, replayed, last_event_at, psycopg.types.json.Jsonb(diff), notes),
        )

    def apply_split(self, position_id: UUID, ratio: Decimal, at: datetime, reason: str) -> None:
        pos = self.conn.execute("select * from positions where id = %s", (position_id,)).fetchone()
        assert pos is not None
        inv = Decimal(1) / ratio
        self.conn.execute(
            """update positions set qty = qty * %s, avg_cost = avg_cost * %s, working_target_price = working_target_price * %s,
               working_invalidation_price = working_invalidation_price * %s, is_fractional = ((qty * %s) <> trunc(qty * %s)) where id = %s""",
            (ratio, inv, inv, inv, ratio, ratio, position_id),
        )
        for lot in self.conn.execute(
            "select * from lots where position_id = %s and qty_remaining > 0", (position_id,)
        ).fetchall():
            new_rem = Decimal(lot["qty_remaining"]) * ratio
            self.conn.execute(
                "update lots set qty_remaining = %s, cost_basis = cost_basis * %s, is_fractional = %s where id = %s",
                (new_rem, inv, new_rem != new_rem.to_integral_value(), lot["id"]),
            )
            self.conn.execute(
                "insert into lot_events (lot_id, at, kind, qty_delta, reason) values (%s, %s, 'split_adjust', %s, %s)",
                (lot["id"], at, new_rem - Decimal(lot["qty_remaining"]), reason),
            )

    def change_symbol(self, position_id: UUID, new_symbol: str, at: datetime) -> None:
        self.conn.execute(
            "insert into instruments (symbol, tradable, fractionable, universe_status, status_reason) values (%s, true, true, 'excluded_universe', 'symbol change; re-evaluated next universe build') on conflict do nothing",
            (new_symbol,),
        )
        self.conn.execute("update positions set symbol = %s where id = %s", (new_symbol, position_id))
        for lot in self.conn.execute("select id from lots where position_id = %s", (position_id,)).fetchall():
            self.conn.execute(
                "insert into lot_events (lot_id, at, kind, qty_delta, reason) values (%s, %s, 'symbol_change', 0, %s)",
                (lot["id"], at, f"to {new_symbol}"),
            )
