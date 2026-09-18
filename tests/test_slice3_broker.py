"""AlpacaPaperBroker against mocked HTTP: idempotent submit (T-04), status mapping, activities paging, policy patch body."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
import respx

from tradeagent.adapters.alpaca.broker_paper import STATUS_MAP, AlpacaPaperBroker, order_body
from tradeagent.adapters.alpaca.client import PAPER_TRADING_URL, AlpacaClient, AlpacaCredentials
from tradeagent.domain.enums import OrderPurpose, OrderSide, OrderStatus, OrderType, TimeInForce
from tradeagent.domain.models import BrokerPolicy, OrderRequest

NOW = datetime(2026, 9, 18, 13, 0, tzinfo=UTC)


def broker() -> AlpacaPaperBroker:
    return AlpacaPaperBroker(AlpacaClient(AlpacaCredentials("k", "s")))


def req(**kw) -> OrderRequest:
    base = dict(
        decision_id=uuid4(),
        portfolio_id=uuid4(),
        purpose=OrderPurpose.ENTRY,
        symbol="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        notional=Decimal("100"),
        is_simulated=False,
        order_eligible_at=NOW,
    )
    base.update(kw)
    return OrderRequest(**base)


def alpaca_order(cid: str, status: str = "accepted", oid: str = "b-1") -> dict:
    return {
        "id": oid,
        "client_order_id": cid,
        "status": status,
        "submitted_at": "2026-09-18T13:00:00Z",
        "filled_qty": "0",
        "filled_avg_price": None,
    }


@respx.mock
def test_submit_new_then_idempotent_resubmit():
    r = req()
    lookup = respx.get(f"{PAPER_TRADING_URL}/v2/orders:by_client_order_id").mock(
        side_effect=[
            httpx.Response(404, json={"message": "order not found"}),
            httpx.Response(200, json=alpaca_order(r.client_order_id, "new")),
        ]
    )
    post = respx.post(f"{PAPER_TRADING_URL}/v2/orders").mock(
        return_value=httpx.Response(200, json=alpaca_order(r.client_order_id))
    )
    first = asyncio.run(broker().submit(r))
    assert first.status == OrderStatus.SUBMITTED and first.broker_order_id == "b-1" and post.call_count == 1
    assert json.loads(post.calls[0].request.content)["client_order_id"] == r.client_order_id
    second = asyncio.run(broker().submit(r))
    assert second.broker_order_id == "b-1" and post.call_count == 1 and lookup.call_count == 2  # no second POST


@respx.mock
def test_submit_race_422_resolves_to_existing_order():
    r = req()
    respx.get(f"{PAPER_TRADING_URL}/v2/orders:by_client_order_id").mock(
        side_effect=[
            httpx.Response(404, json={}),
            httpx.Response(200, json=alpaca_order(r.client_order_id, "new", "b-9")),
        ]
    )
    respx.post(f"{PAPER_TRADING_URL}/v2/orders").mock(
        return_value=httpx.Response(422, json={"code": 40010001, "message": "client_order_id must be unique"})
    )
    assert asyncio.run(broker().submit(r)).broker_order_id == "b-9"


def test_simulated_orders_never_reach_the_broker():
    with pytest.raises(RuntimeError):
        asyncio.run(broker().submit(req(is_simulated=True)))


def test_status_mapping_covers_alpaca_lifecycle():
    assert STATUS_MAP["accepted"] == OrderStatus.SUBMITTED and STATUS_MAP["new"] == OrderStatus.SUBMITTED
    assert STATUS_MAP["partially_filled"] == OrderStatus.PARTIALLY_FILLED and STATUS_MAP["filled"] == OrderStatus.FILLED
    assert (
        STATUS_MAP["replaced"] == OrderStatus.CANCELLED
        and STATUS_MAP["done_for_day"] == OrderStatus.EXPIRED
        and STATUS_MAP["rejected"] == OrderStatus.REJECTED
    )


def test_order_body_shapes():
    frac = order_body(
        req(
            purpose=OrderPurpose.STOP_REARM,
            side=OrderSide.SELL,
            order_type=OrderType.STOP,
            qty=Decimal("0.5"),
            notional=None,
            stop_price=Decimal("95"),
        )
    )
    assert frac == {
        "symbol": "AAPL",
        "side": "sell",
        "type": "stop",
        "time_in_force": "day",
        "client_order_id": frac["client_order_id"],
        "extended_hours": False,
        "qty": "0.5",
        "stop_price": "95",
    }
    br = order_body(
        req(
            purpose=OrderPurpose.BRACKET_ENTRY,
            order_type=OrderType.MARKET,
            qty=Decimal("2"),
            notional=None,
            time_in_force=TimeInForce.GTC,
            stop_price=Decimal("95"),
            take_profit_price=Decimal("110"),
        )
    )
    assert (
        br["order_class"] == "bracket"
        and br["take_profit"] == {"limit_price": "110"}
        and br["stop_loss"] == {"stop_price": "95"}
        and "stop_price" not in br
    )


@respx.mock
def test_activities_pagination_and_positions_and_policy():
    page1 = [
        {
            "id": f"a{i}",
            "activity_type": "FILL",
            "transaction_time": "2026-09-18T13:00:00Z",
            "type": "fill",
            "price": "1",
            "qty": "1",
            "side": "buy",
            "symbol": "AAPL",
            "leaves_qty": "0",
            "order_id": "b",
        }
        for i in range(100)
    ]
    page2 = [
        {
            "id": "a100",
            "activity_type": "FILL",
            "transaction_time": "2026-09-18T13:01:00Z",
            "type": "fill",
            "price": "1",
            "qty": "1",
            "side": "buy",
            "symbol": "AAPL",
            "leaves_qty": "0",
            "order_id": "b",
        }
    ]
    acts = respx.get(f"{PAPER_TRADING_URL}/v2/account/activities").mock(
        side_effect=[httpx.Response(200, json=page1), httpx.Response(200, json=page2)]
    )
    assert (
        len(asyncio.run(broker().activities_since(NOW))) == 101
        and acts.calls[1].request.url.params["page_token"] == "a99"
    )
    respx.get(f"{PAPER_TRADING_URL}/v2/positions").mock(
        return_value=httpx.Response(200, json=[{"symbol": "AAPL", "qty": "1.5", "avg_entry_price": "101.2"}])
    )
    pos = asyncio.run(broker().positions())
    assert pos[0].symbol == "AAPL" and pos[0].qty == Decimal("1.5")
    patch = respx.patch(f"{PAPER_TRADING_URL}/v2/account/configurations").mock(
        return_value=httpx.Response(200, json={**BrokerPolicy().as_patch(), "suspend_trade": False})
    )
    cfg = asyncio.run(broker().write_account_configuration(BrokerPolicy()))
    assert (
        BrokerPolicy().matches(cfg) and json.loads(patch.calls[0].request.content)["disable_overnight_trading"] is True
    )
