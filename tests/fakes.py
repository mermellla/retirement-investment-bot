"""In-memory broker for Slice 3+ tests: records submissions, serves configurable statuses, positions, activities."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from tradeagent.domain.enums import ExecutionMode
from tradeagent.domain.models import AccountConfiguration, BrokerPolicy, OrderRequest, OrderState, Position

GOOD = dict(
    max_margin_multiplier="1",
    no_shorting=True,
    max_options_trading_level=0,
    fractional_trading=True,
    disable_overnight_trading=True,
)


class FakeBroker:
    mode = ExecutionMode.PAPER
    observe_only = False

    def __init__(
        self,
        positions: list[Position] | None = None,
        activities: list[dict[str, Any]] | None = None,
        config: dict[str, Any] | None = None,
        submit_status: str = "accepted",
        status_sequence: dict[str, list[str]] | None = None,
        unreadable_config: bool = False,
        writable: bool = True,
    ):
        self._positions = positions or []
        self._activities = activities or []
        self.config = dict(config) if config is not None else dict(GOOD)
        self.unreadable = unreadable_config
        self.writable = writable
        self.submit_status = submit_status
        self.status_sequence = status_sequence or {}
        self.orders: dict[str, dict[str, Any]] = {}  # client_order_id → record
        self.submitted: list[OrderRequest] = []
        self.writes: list[dict[str, Any]] = []

    def _state(self, cid: str) -> OrderState:
        rec = self.orders[cid]
        seq = self.status_sequence.get(cid)
        raw = seq.pop(0) if seq else rec["status"]
        if seq is not None and not seq:
            seq.append(raw)
        rec["status"] = raw
        from tradeagent.adapters.alpaca.broker_paper import STATUS_MAP

        return OrderState(
            order_id=rec["order_id"],
            client_order_id=cid,
            status=STATUS_MAP[raw],
            status_reason=f"alpaca:{raw}",
            broker_order_id=rec["broker_id"],
            submitted_at=rec["submitted_at"],
        )

    async def submit(self, request: OrderRequest) -> OrderState:
        cid = request.client_order_id
        if cid not in self.orders:
            self.orders[cid] = {
                "order_id": uuid4(),
                "broker_id": f"brk-{len(self.orders) + 1}",
                "status": self.submit_status,
                "submitted_at": datetime.now(tz=UTC),
                "request": request,
            }
            self.submitted.append(request)
        return self._state(cid)

    async def cancel(self, order_id: UUID) -> OrderState:
        raise NotImplementedError

    async def replace(self, order_id: UUID, request: OrderRequest) -> OrderState:
        raise NotImplementedError

    async def order_status(self, client_order_id: str) -> OrderState | None:
        return self._state(client_order_id) if client_order_id in self.orders else None

    async def order_by_broker_id(self, broker_order_id: str) -> OrderState | None:
        for cid, rec in self.orders.items():
            if rec["broker_id"] == broker_order_id:
                return self._state(cid)
        return None

    async def open_orders(self) -> list[OrderState]:
        return [
            self._state(cid)
            for cid, rec in self.orders.items()
            if rec["status"] in ("accepted", "new", "partially_filled")
        ]

    async def positions(self) -> list[Position]:
        return list(self._positions)

    async def activities_since(self, since: datetime | None) -> list[dict[str, Any]]:
        return [
            a
            for a in self._activities
            if since is None or datetime.fromisoformat(a["transaction_time"].replace("Z", "+00:00")) > since
        ]

    async def account_configuration(self) -> AccountConfiguration:
        if self.unreadable:
            raise RuntimeError("503 from broker")
        return AccountConfiguration.model_validate(self.config)

    async def write_account_configuration(self, policy: BrokerPolicy) -> AccountConfiguration:
        self.writes.append(policy.as_patch())
        if self.writable:
            self.config.update(policy.as_patch())
        return AccountConfiguration.model_validate(self.config)


def broker_position(symbol: str, qty: str, cost: str = "100") -> Position:
    return Position(
        portfolio_id=UUID(int=0), symbol=symbol, entry_decision_id=UUID(int=0), qty=Decimal(qty), avg_cost=Decimal(cost)
    )


def fill_activity(
    activity_id: str,
    order_broker_id: str,
    symbol: str,
    side: str,
    qty: str,
    price: str,
    at: str,
    kind: str = "fill",
    leaves: str = "0",
) -> dict[str, Any]:
    return {
        "id": activity_id,
        "activity_type": "FILL",
        "transaction_time": at,
        "type": kind,
        "price": price,
        "qty": qty,
        "side": side,
        "symbol": symbol,
        "leaves_qty": leaves,
        "order_id": order_broker_id,
        "cum_qty": qty,
        "order_status": "filled" if kind == "fill" else "partially_filled",
    }
