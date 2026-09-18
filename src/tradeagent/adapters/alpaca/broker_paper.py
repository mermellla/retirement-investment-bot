"""AlpacaPaperBroker (§8, ADR-0020): the only broker that submits orders in this codebase, and only to the paper host.

Idempotency (§8.3, §8.8): every submit carries our client_order_id; if Alpaca already holds an order with that id
(422 on POST, or a pre-check hit) the existing order is returned and nothing is resubmitted."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid5

from tradeagent.adapters.alpaca.client import AlpacaClient, AlpacaHttpError
from tradeagent.domain.enums import ExecutionMode, OrderStatus
from tradeagent.domain.models import AccountConfiguration, BrokerPolicy, OrderRequest, OrderState, Position

ORDER_NS = UUID("6f1c9c1e-0000-4000-8000-000000000001")

# Alpaca order status → §8.4 state (docs/phase0/05-schema.md "Broker status mapping")
STATUS_MAP: dict[str, OrderStatus] = {
    "accepted": OrderStatus.SUBMITTED,
    "pending_new": OrderStatus.SUBMITTED,
    "new": OrderStatus.SUBMITTED,
    "accepted_for_bidding": OrderStatus.SUBMITTED,
    "held": OrderStatus.SUBMITTED,
    "stopped": OrderStatus.SUBMITTED,
    "calculated": OrderStatus.SUBMITTED,
    "pending_cancel": OrderStatus.SUBMITTED,
    "pending_replace": OrderStatus.SUBMITTED,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELLED,
    "replaced": OrderStatus.CANCELLED,
    "rejected": OrderStatus.REJECTED,
    "suspended": OrderStatus.REJECTED,
    "expired": OrderStatus.EXPIRED,
    "done_for_day": OrderStatus.EXPIRED,
}


def order_state(o: dict[str, Any]) -> OrderState:
    raw = str(o.get("status", ""))
    return OrderState(
        order_id=uuid5(ORDER_NS, str(o["id"])),
        client_order_id=str(o.get("client_order_id", "")),
        status=STATUS_MAP.get(raw, OrderStatus.SUBMITTED),
        status_reason=f"alpaca:{raw}",
        broker_order_id=str(o["id"]),
        submitted_at=_ts(o.get("submitted_at")),
        filled_qty=Decimal(str(o.get("filled_qty") or "0")),
        avg_fill_price=(Decimal(str(o["filled_avg_price"])) if o.get("filled_avg_price") else None),
    )


def _ts(v: Any) -> datetime | None:
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")) if v else None


def order_body(req: OrderRequest) -> dict[str, Any]:
    body: dict[str, Any] = {
        "symbol": req.symbol,
        "side": req.side.value,
        "type": req.order_type.value,
        "time_in_force": req.time_in_force.value,
        "client_order_id": req.client_order_id,
        "extended_hours": req.extended_hours,
    }
    if req.qty is not None:
        body["qty"] = str(req.qty)
    else:
        body["notional"] = str(req.notional)
    if req.limit_price is not None:
        body["limit_price"] = str(req.limit_price)
    if req.stop_price is not None:
        body["stop_price"] = str(req.stop_price)
    if req.take_profit_price is not None and req.stop_price is not None:
        body["order_class"] = "bracket"
        body["take_profit"] = {"limit_price": str(req.take_profit_price)}
        body["stop_loss"] = {"stop_price": str(req.stop_price)}
        body.pop("stop_price")
    return body


class AlpacaPaperBroker:
    mode = ExecutionMode.PAPER
    observe_only = False

    def __init__(self, client: AlpacaClient):
        if not client.authenticated:
            raise RuntimeError("ALPACA_CREDENTIALS_MISSING")
        self.client = client

    async def submit(self, request: OrderRequest) -> OrderState:
        if request.is_simulated:
            raise RuntimeError("simulated orders never reach the broker (§8.9)")
        existing = await self.order_status(request.client_order_id)
        if existing is not None:
            return existing
        try:
            return order_state(self.client.post("/v2/orders", order_body(request)))
        except AlpacaHttpError as exc:
            if exc.status in (409, 422) and "client_order_id" in exc.body:
                again = await self.order_status(request.client_order_id)
                if again is not None:
                    return again
            raise

    async def cancel(self, order_id: UUID) -> OrderState:
        broker_id = self._broker_id(order_id)
        self.client.delete(f"/v2/orders/{broker_id}")
        return order_state(self.client.get(f"/v2/orders/{broker_id}"))

    async def replace(self, order_id: UUID, request: OrderRequest) -> OrderState:
        body: dict[str, Any] = {"client_order_id": request.client_order_id}
        if request.qty is not None:
            body["qty"] = str(request.qty)
        if request.limit_price is not None:
            body["limit_price"] = str(request.limit_price)
        if request.stop_price is not None:
            body["stop_price"] = str(request.stop_price)
        return order_state(self.client.patch(f"/v2/orders/{self._broker_id(order_id)}", body))

    async def order_status(self, client_order_id: str) -> OrderState | None:
        try:
            return order_state(self.client.get("/v2/orders:by_client_order_id", {"client_order_id": client_order_id}))
        except AlpacaHttpError as exc:
            if exc.status == 404:
                return None
            raise

    async def order_by_broker_id(self, broker_order_id: str) -> OrderState | None:
        try:
            return order_state(self.client.get(f"/v2/orders/{broker_order_id}"))
        except AlpacaHttpError as exc:
            if exc.status == 404:
                return None
            raise

    async def open_orders(self) -> list[OrderState]:
        return [
            order_state(o) for o in self.client.get("/v2/orders", {"status": "open", "limit": 500, "nested": "false"})
        ]

    async def positions(self) -> list[Position]:
        return [
            Position(
                portfolio_id=UUID(int=0),
                symbol=p["symbol"],
                entry_decision_id=UUID(int=0),
                qty=Decimal(str(p["qty"])),
                avg_cost=Decimal(str(p["avg_entry_price"])),
            )
            for p in self.client.get("/v2/positions")
        ]

    async def activities_since(self, since: datetime | None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        token: str | None = None
        for _ in range(50):
            params: dict[str, Any] = {"activity_types": "FILL", "direction": "asc", "page_size": 100}
            if since:
                params["after"] = since.isoformat()
            if token:
                params["page_token"] = token
            page = self.client.get("/v2/account/activities", params)
            out.extend(page)
            if len(page) < 100:
                break
            token = str(page[-1]["id"])
        return out

    async def account_configuration(self) -> AccountConfiguration:
        return AccountConfiguration.model_validate(self.client.get("/v2/account/configurations"))

    async def write_account_configuration(self, policy: BrokerPolicy) -> AccountConfiguration:
        return AccountConfiguration.model_validate(self.client.patch("/v2/account/configurations", policy.as_patch()))

    def _broker_id(self, order_id: UUID) -> str:
        raise NotImplementedError("resolve broker ids through the ledger (orders.broker_order_id); see Executor")
