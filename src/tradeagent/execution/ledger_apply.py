"""Applying fills to the virtual ledger (ADR-0002): one transaction per fill under the portfolio lock —
fill row → lot events → position projection → fees → cash event. Used by reconciliation replay (§8.6) and, from
Slice 4, by the executor. Idempotent by broker_fill_id / reconstructed fill id."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg

from tradeagent.domain.enums import OrderSide
from tradeagent.fees import FeeSchedules
from tradeagent.persistence.db import Database

CENT = Decimal("0.000001")


@dataclass(frozen=True)
class AppliedFill:
    fill_id: UUID
    position_id: UUID
    cash_delta: Decimal
    customer_fees: Decimal
    unverified_fees: Decimal
    position_closed: bool


def apply_fill(
    db: Database,
    *,
    order: dict[str, Any],
    qty: Decimal,
    price: Decimal,
    fill_at: datetime,
    fill_source: str,
    broker_fill_id: str | None,
    fees: FeeSchedules | None,
    settles_on: date | None,
    reconstruction_basis: str | None = None,
    half_spread_estimate: Decimal | None = None,
) -> AppliedFill | None:
    """Returns None when the fill was already applied (idempotent replay)."""
    conn = db.conn
    if broker_fill_id and conn.execute("select 1 from fills where broker_fill_id = %s", (broker_fill_id,)).fetchone():
        return None
    portfolio_id = UUID(str(order["portfolio_id"]))
    conn.execute("select lock_portfolio(%s)", (portfolio_id,))
    notional = (qty * price).quantize(CENT)
    side = OrderSide(order["side"])
    # fills are append-only (§8.5): the position row must exist before the fill that references it is written
    pos = conn.execute(
        "select * from positions where portfolio_id = %s and symbol = %s and status = 'open'",
        (portfolio_id, order["symbol"]),
    ).fetchone()
    if pos is None:
        if side != OrderSide.BUY:
            raise psycopg.errors.CheckViolation(
                f"SELL_WITHOUT_POSITION: no open position for {order['symbol']} in portfolio {portfolio_id}"
            )
        pos = conn.execute(
            """insert into positions (portfolio_id, experiment_id, opened_in_phase_id, symbol, entry_decision_id, opened_at, qty, avg_cost, is_fractional)
               values (%s, %s, %s, %s, %s, %s, 0, 0, false) returning *""",
            (
                portfolio_id,
                order["experiment_id"],
                order["experiment_phase_id"],
                order["symbol"],
                order["decision_id"],
                fill_at,
            ),
        ).fetchone()
        assert pos is not None
    row = conn.execute(
        """insert into fills (order_id, portfolio_id, experiment_id, experiment_phase_id, position_id, symbol, side, qty, price, notional, fill_at, fill_source,
           broker_fill_id, reconstruction_basis, half_spread_estimate) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) returning id""",
        (
            order["id"],
            portfolio_id,
            order["experiment_id"],
            order["experiment_phase_id"],
            pos["id"],
            order["symbol"],
            side.value,
            qty,
            price,
            notional,
            fill_at,
            fill_source,
            broker_fill_id,
            reconstruction_basis,
            half_spread_estimate,
        ),
    ).fetchone()
    assert row is not None
    fill_id = UUID(str(row["id"]))

    # ---- position projection and lots
    closed = False
    if side == OrderSide.BUY:
        old_qty, old_cost = Decimal(pos["qty"]), Decimal(pos["avg_cost"] or 0)
        new_qty = old_qty + qty
        new_cost = ((old_qty * old_cost + notional) / new_qty).quantize(CENT) if new_qty else Decimal(0)
        is_fractional = new_qty != new_qty.to_integral_value()
        conn.execute(
            "update positions set qty = %s, avg_cost = %s, is_fractional = %s where id = %s",
            (new_qty, new_cost, is_fractional, pos["id"]),
        )
        lot = conn.execute(
            "insert into lots (position_id, portfolio_id, opened_fill_id, qty_opened, qty_remaining, cost_basis, is_fractional, opened_at) values (%s, %s, %s, %s, %s, %s, %s, %s) returning id",
            (pos["id"], portfolio_id, fill_id, qty, qty, price, qty != qty.to_integral_value(), fill_at),
        ).fetchone()
        assert lot is not None
        conn.execute(
            "insert into lot_events (lot_id, at, kind, qty_delta, price, fill_id, reason) values (%s, %s, 'open', %s, %s, %s, 'buy fill')",
            (lot["id"], fill_at, qty, price, fill_id),
        )
    else:
        remaining = qty
        for lot in conn.execute(
            "select * from lots where position_id = %s and qty_remaining > 0 order by opened_at, id", (pos["id"],)
        ).fetchall():
            if remaining <= 0:
                break
            take = min(remaining, Decimal(lot["qty_remaining"]))
            left = Decimal(lot["qty_remaining"]) - take
            conn.execute(
                "update lots set qty_remaining = %s, closed_at = %s where id = %s",
                (left, fill_at if left == 0 else None, lot["id"]),
            )
            conn.execute(
                "insert into lot_events (lot_id, at, kind, qty_delta, price, fill_id, reason) values (%s, %s, %s, %s, %s, %s, 'sell fill')",
                (lot["id"], fill_at, "close" if left == 0 else "reduce", -take, price, fill_id),
            )
            remaining -= take
        if remaining > 0:
            raise psycopg.errors.CheckViolation(f"SELL_EXCEEDS_POSITION: {order['symbol']} over by {remaining}")
        new_qty = Decimal(pos["qty"]) - qty
        closed = new_qty == 0
        conn.execute(
            "update positions set qty = %s, status = %s, closed_at = %s where id = %s",
            (new_qty, "closed" if closed else "open", fill_at if closed else None, pos["id"]),
        )
    if order.get("position_id") is None:
        conn.execute("update orders set position_id = %s where id = %s", (pos["id"], order["id"]))

    # ---- fees (§9, ADR-0012, A-07)
    customer = unverified = Decimal(0)
    if fees is not None:
        lines = (
            fees.for_sell(fill_at.date(), qty, notional)
            if side == OrderSide.SELL
            else fees.for_buy(fill_at.date(), qty)
        )
        for line in lines:
            conn.execute(
                "insert into fees (fill_id, portfolio_id, kind, amount_usd, rate_basis, fee_schedule_version, classification) values (%s, %s, %s, %s, %s, %s, %s) on conflict do nothing",
                (
                    fill_id,
                    portfolio_id,
                    line.kind.value,
                    line.amount_usd,
                    psycopg.types.json.Jsonb(line.rate_basis),
                    fees.version,
                    line.classification,
                ),
            )
            if line.classification == "customer_debited":
                customer += line.amount_usd
            else:
                unverified += line.amount_usd

    # ---- cash (only customer-debited fees move cash; A-07)
    delta = (-notional if side == OrderSide.BUY else notional) - customer
    balance = db.cash_balance(portfolio_id) + delta
    conn.execute(
        """insert into cash_ledger (portfolio_id, experiment_id, experiment_phase_id, at, kind, amount_usd, balance_after_usd, settles_on, fill_id, reason, idempotency_key)
           values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            portfolio_id,
            order["experiment_id"],
            order["experiment_phase_id"],
            fill_at,
            side.value,
            delta,
            balance,
            settles_on,
            fill_id,
            f"{side.value} {qty} {order['symbol']} @ {price}",
            f"fill:{fill_id}",
        ),
    )
    return AppliedFill(fill_id, UUID(str(pos["id"])), delta, customer, unverified, closed)
