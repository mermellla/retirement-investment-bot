"""§8.6 boot-time reconciliation: replay known broker activity into the ledger, repair explained differences, halt on
anything unexplained (manual trade, unknown ticker, excluded name, quantity no replay produces)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from tradeagent.domain.enums import ExecutionMode, OrderStatus, ReconcileResult
from tradeagent.execution.ledger_apply import apply_fill
from tradeagent.fees import FeeSchedules
from tradeagent.interfaces import Broker
from tradeagent.persistence.db import Database

Q = Decimal("0.000000001")


@dataclass
class ReconcileReport:
    result: ReconcileResult
    replayed: int = 0
    repaired: list[str] = field(default_factory=list)
    unexplained: list[dict[str, Any]] = field(default_factory=list)
    last_event_at: datetime | None = None

    def diff(self) -> dict[str, Any]:
        return {"replayed": self.replayed, "repaired": self.repaired, "unexplained": self.unexplained}


def _ts(v: Any) -> datetime:
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


async def _resolve_order(db: Database, broker: Broker, broker_order_id: str) -> dict[str, Any] | None:
    order = db.order_by_broker_id(broker_order_id)
    if order is not None:
        return order
    lookup = getattr(broker, "order_by_broker_id", None)
    if lookup is None:
        return None
    state = await lookup(broker_order_id)
    if state is None or not state.client_order_id:
        return None
    order = db.order_by_client_id(state.client_order_id)
    if order is not None:
        db.conn.execute(
            "update orders set broker_order_id = coalesce(broker_order_id, %s) where id = %s",
            (broker_order_id, order["id"]),
        )
    return order


async def reconcile(
    db: Database,
    broker: Broker,
    experiment_id: UUID,
    mode: ExecutionMode,
    fees: FeeSchedules | None,
    settles_on_fn: Any = None,
) -> ReconcileReport:
    rep = ReconcileReport(ReconcileResult.AGREE)
    primary = db.primary_portfolio(experiment_id)
    portfolio_id = UUID(str(primary["id"]))
    since = db.last_reconciled_event_at(experiment_id)
    rep.last_event_at = since

    # 1–2. replay every FILL whose order maps to a known client_order_id (idempotent by activity id)
    for act in await broker.activities_since(since):
        if act.get("activity_type") != "FILL":
            continue
        order = await _resolve_order(db, broker, str(act.get("order_id")))
        at = _ts(act["transaction_time"])
        if order is None:
            rep.unexplained.append(
                {
                    "kind": "fill_without_known_order",
                    "activity_id": act.get("id"),
                    "symbol": act.get("symbol"),
                    "qty": act.get("qty"),
                    "side": act.get("side"),
                }
            )
            continue
        applied = apply_fill(
            db,
            order=order,
            qty=Decimal(str(act["qty"])),
            price=Decimal(str(act["price"])),
            fill_at=at,
            fill_source="broker",
            broker_fill_id=str(act["id"]),
            fees=fees,
            settles_on=(settles_on_fn(at.date()) if settles_on_fn else None),
        )
        if applied is not None:
            rep.replayed += 1
            rep.repaired.append(
                f"replayed fill {act['id']} {act['side']} {act['qty']} {act['symbol']} @ {act['price']}"
            )
            target = (
                OrderStatus.FILLED
                if act.get("type") == "fill" or Decimal(str(act.get("leaves_qty") or 0)) == 0
                else OrderStatus.PARTIALLY_FILLED
            )
            if order["status"] in ("SUBMITTED", "PARTIALLY_FILLED") and target.value != order["status"]:
                db.transition_order(
                    UUID(str(order["id"])),
                    target,
                    f"reconcile: activity {act['id']}",
                    broker_order_id=str(act.get("order_id")),
                )
        rep.last_event_at = max(rep.last_event_at, at) if rep.last_event_at else at

    # 3–5. compare positions (§3.2: broker is authoritative about what is held; the ledger must explain it)
    broker_positions = {p.symbol: p for p in await broker.positions()}
    ledger_positions = {p["symbol"]: p for p in db.open_positions(portfolio_id)}
    for sym, bp in broker_positions.items():
        inst = db.conn.execute("select universe_status from instruments where symbol = %s", (sym,)).fetchone()
        if inst is not None and inst["universe_status"] == "excluded_ethical":
            rep.unexplained.append({"kind": "position_in_excluded_name", "symbol": sym, "qty": str(bp.qty)})
            continue
        lp = ledger_positions.get(sym)
        if lp is None:
            rep.unexplained.append({"kind": "position_unknown_to_ledger", "symbol": sym, "qty": str(bp.qty)})
        elif Decimal(lp["qty"]).quantize(Q) != bp.qty.quantize(Q):
            rep.unexplained.append(
                {"kind": "quantity_mismatch", "symbol": sym, "broker_qty": str(bp.qty), "ledger_qty": str(lp["qty"])}
            )
    for sym, lp in ledger_positions.items():
        if sym not in broker_positions and mode == ExecutionMode.PAPER:
            rep.unexplained.append({"kind": "position_missing_at_broker", "symbol": sym, "ledger_qty": str(lp["qty"])})

    # open orders: unknown at the broker → unexplained; ledger-open but gone at the broker → refresh from the broker
    broker_open = {o.client_order_id: o for o in await broker.open_orders()}
    for cid, o in broker_open.items():
        if db.order_by_client_id(cid) is None:
            rep.unexplained.append(
                {"kind": "open_order_unknown_to_ledger", "client_order_id": cid, "broker_order_id": o.broker_order_id}
            )
    for lo in db.open_broker_orders(portfolio_id):
        if lo["client_order_id"] in broker_open:
            continue
        state = await broker.order_status(lo["client_order_id"])
        if state is None:
            rep.unexplained.append(
                {"kind": "ledger_open_order_missing_at_broker", "client_order_id": lo["client_order_id"]}
            )
        elif state.status in (OrderStatus.CANCELLED, OrderStatus.EXPIRED, OrderStatus.REJECTED):
            db.transition_order(UUID(str(lo["id"])), state.status, f"reconcile: {state.status_reason}")
            rep.repaired.append(f"order {lo['client_order_id']} → {state.status.value}")
        elif state.status == OrderStatus.FILLED and lo["status"] != "FILLED":
            rep.unexplained.append(
                {"kind": "filled_at_broker_without_fill_activity", "client_order_id": lo["client_order_id"]}
            )

    if rep.unexplained:
        rep.result = ReconcileResult.HALT
    elif rep.replayed or rep.repaired:
        rep.result = ReconcileResult.REPAIRED
    db.record_reconciliation_full(
        experiment_id,
        mode,
        rep.result,
        rep.diff(),
        "§8.6 replay-or-halt",
        rep.replayed,
        rep.last_event_at if rep.result != ReconcileResult.HALT else since,
    )
    return rep
