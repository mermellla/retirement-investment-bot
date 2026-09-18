"""T-13 broker policy; T-10 reconciliation: replay repairs, unknown ticker / excluded name / manual trade halt, idempotent replay."""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from psycopg.rows import dict_row

from tests.fakes import GOOD, FakeBroker, broker_position, fill_activity
from tests.seed import NOW, Seed
from tradeagent.config import load_settings
from tradeagent.domain.enums import BrokerPolicyResult, ExecutionMode, ReconcileResult
from tradeagent.domain.models import BrokerPolicy
from tradeagent.execution.broker_policy import BrokerPolicyHalt, enforce_broker_policy
from tradeagent.execution.ledger_apply import apply_fill
from tradeagent.execution.reconcile import reconcile
from tradeagent.fees import FeeSchedules
from tradeagent.persistence.db import Database

pytestmark = pytest.mark.db
psycopg = pytest.importorskip("psycopg")


@pytest.fixture
def dbx(db):
    db.row_factory = dict_row  # Database expects dict rows; the seed writes only, so sharing the connection is safe
    return Database(db)


def fees() -> FeeSchedules:
    s = load_settings(env={})
    return FeeSchedules.from_config(s.fees, s.versions.config_version)


# ---- §8.10


def test_policy_match_apply_and_halts(dbx, seed):
    eid = seed.experiment_id
    assert (
        asyncio.run(enforce_broker_policy(dbx, FakeBroker(), BrokerPolicy(), True, eid, ExecutionMode.PAPER))[0]
        == BrokerPolicyResult.MATCH
    )
    loose = FakeBroker(config={**GOOD, "max_margin_multiplier": "4", "no_shorting": False})
    res, after = asyncio.run(enforce_broker_policy(dbx, loose, BrokerPolicy(), True, eid, ExecutionMode.PAPER))
    assert (
        res == BrokerPolicyResult.APPLIED
        and after.max_margin_multiplier == "1"
        and loose.writes == [BrokerPolicy().as_patch()]
    )
    with pytest.raises(BrokerPolicyHalt):
        asyncio.run(
            enforce_broker_policy(
                dbx, FakeBroker(unreadable_config=True), BrokerPolicy(), True, eid, ExecutionMode.PAPER
            )
        )
    with pytest.raises(BrokerPolicyHalt):  # write does not take
        asyncio.run(
            enforce_broker_policy(
                dbx,
                FakeBroker(config={**GOOD, "no_shorting": False}, writable=False),
                BrokerPolicy(),
                True,
                eid,
                ExecutionMode.PAPER,
            )
        )
    with pytest.raises(BrokerPolicyHalt):  # enforcement disabled → halt on mismatch
        asyncio.run(
            enforce_broker_policy(
                dbx, FakeBroker(config={**GOOD, "no_shorting": False}), BrokerPolicy(), False, eid, ExecutionMode.PAPER
            )
        )
    rows = dbx.conn.execute(
        "select result from broker_policy_checks where experiment_id = %s order by id", (eid,)
    ).fetchall()
    assert [r["result"] for r in rows] == ["match", "applied", "halt", "halt", "halt"]


# ---- §8.6 helpers


def submitted_buy(seed: Seed, notional=Decimal("300"), broker_id="brk-1", qty=None):
    d = seed.decision()
    o = seed.order(d, is_simulated=False, notional=notional, reserved_notional_usd=notional)
    seed.transition(o, "VALIDATED")
    seed.transition(o, "SUBMITTED")
    seed.conn.execute("update orders set broker_order_id = %s where id = %s", (broker_id, o))
    return o


def test_replay_repairs_missed_fill(dbx, seed):
    o = submitted_buy(seed)
    at = (NOW + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    broker = FakeBroker(
        positions=[broker_position("AAPL", "3", "100")],
        activities=[fill_activity("act-1", "brk-1", "AAPL", "buy", "3", "100", at)],
    )
    rep = asyncio.run(reconcile(dbx, broker, seed.experiment_id, ExecutionMode.PAPER, fees()))
    assert rep.result == ReconcileResult.REPAIRED and rep.replayed == 1 and rep.unexplained == []
    pos = dbx.open_positions(seed.primary_id)
    assert len(pos) == 1 and Decimal(pos[0]["qty"]) == 3 and Decimal(pos[0]["avg_cost"]) == 100
    assert dbx.conn.execute("select status, reserved_notional_usd from orders where id = %s", (o,)).fetchone() == {
        "status": "FILLED",
        "reserved_notional_usd": Decimal("0"),
    }
    assert dbx.cash_balance(seed.primary_id) == Decimal("200") and dbx.available_cash(seed.primary_id) == Decimal("200")
    # second boot: same activity → nothing replayed, agree
    rep2 = asyncio.run(reconcile(dbx, broker, seed.experiment_id, ExecutionMode.PAPER, fees()))
    assert rep2.result == ReconcileResult.AGREE and rep2.replayed == 0
    assert dbx.conn.execute("select count(*) from fills where broker_fill_id = 'act-1'").fetchone()["count"] == 1


def test_unknown_ticker_halts(dbx, seed):
    rep = asyncio.run(
        reconcile(
            dbx, FakeBroker(positions=[broker_position("TSLA", "2")]), seed.experiment_id, ExecutionMode.PAPER, fees()
        )
    )
    assert rep.result == ReconcileResult.HALT and rep.unexplained[0]["kind"] == "position_unknown_to_ledger"
    assert (
        dbx.conn.execute("select result from reconciliations order by ran_at desc limit 1").fetchone()["result"]
        == "halt"
    )


def test_excluded_name_position_halts(dbx, seed):
    rep = asyncio.run(
        reconcile(
            dbx, FakeBroker(positions=[broker_position("XOM", "1")]), seed.experiment_id, ExecutionMode.PAPER, fees()
        )
    )
    assert rep.result == ReconcileResult.HALT and rep.unexplained[0]["kind"] == "position_in_excluded_name"


def test_manual_trade_halts(dbx, seed):
    """A fill with no known client_order_id and a broker quantity no replay produces."""
    submitted_buy(seed)
    at = (NOW + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    broker = FakeBroker(
        positions=[broker_position("AAPL", "5", "100")],
        activities=[
            fill_activity("act-1", "brk-1", "AAPL", "buy", "3", "100", at),
            fill_activity("act-manual", "manual-99", "AAPL", "buy", "2", "100", at),
        ],
    )
    rep = asyncio.run(reconcile(dbx, broker, seed.experiment_id, ExecutionMode.PAPER, fees()))
    kinds = {u["kind"] for u in rep.unexplained}
    assert rep.result == ReconcileResult.HALT and kinds == {"fill_without_known_order", "quantity_mismatch"}


def test_broker_cancelled_order_refreshed_and_dry_run_flat_agrees(dbx, seed):
    o = submitted_buy(seed)
    broker = FakeBroker()
    broker.orders[
        dbx.conn.execute("select client_order_id from orders where id = %s", (o,)).fetchone()["client_order_id"]
    ] = {"order_id": uuid.uuid4(), "broker_id": "brk-1", "status": "canceled", "submitted_at": NOW, "request": None}
    rep = asyncio.run(reconcile(dbx, broker, seed.experiment_id, ExecutionMode.PAPER, fees()))
    assert (
        rep.result == ReconcileResult.REPAIRED
        and dbx.conn.execute("select status from orders where id = %s", (o,)).fetchone()["status"] == "CANCELLED"
    )
    assert dbx.available_cash(seed.primary_id) == Decimal("500")  # reservation released with the cancel
    from tradeagent.adapters.alpaca.broker_null import NullBroker

    assert (
        asyncio.run(reconcile(dbx, NullBroker(), seed.experiment_id, ExecutionMode.DRY_RUN, fees())).result
        == ReconcileResult.AGREE
    )


# ---- ledger apply: FIFO lots, fees, cash chain


def test_apply_fill_buy_reduce_close_with_fees(dbx, seed):
    o = submitted_buy(seed, notional=Decimal("200"))
    order = dbx.conn.execute("select * from orders where id = %s", (o,)).fetchone()
    a = apply_fill(
        dbx,
        order=order,
        qty=Decimal("2"),
        price=Decimal("100"),
        fill_at=NOW + timedelta(minutes=5),
        fill_source="broker",
        broker_fill_id="f1",
        fees=fees(),
        settles_on=date(2026, 9, 15),
    )
    assert a is not None and a.cash_delta == Decimal("-200") and not a.position_closed
    assert (
        apply_fill(
            dbx,
            order=order,
            qty=Decimal("2"),
            price=Decimal("100"),
            fill_at=NOW,
            fill_source="broker",
            broker_fill_id="f1",
            fees=fees(),
            settles_on=None,
        )
        is None
    )  # idempotent
    d2 = seed.decision(decision="SELL")
    so = seed.order(d2, is_simulated=False, side="sell", purpose="exit", notional=None, qty=Decimal("2"))
    seed.transition(so, "VALIDATED")
    seed.transition(so, "SUBMITTED")
    sorder = dbx.conn.execute("select * from orders where id = %s", (so,)).fetchone()
    b = apply_fill(
        dbx,
        order=sorder,
        qty=Decimal("0.5"),
        price=Decimal("110"),
        fill_at=NOW + timedelta(hours=1),
        fill_source="broker",
        broker_fill_id="f2",
        fees=fees(),
        settles_on=None,
    )
    assert (
        b is not None
        and not b.position_closed
        and b.cash_delta == Decimal("55") - b.customer_fees
        and b.unverified_fees == Decimal("0.00")
    )
    c = apply_fill(
        dbx,
        order=sorder,
        qty=Decimal("1.5"),
        price=Decimal("120"),
        fill_at=NOW + timedelta(hours=2),
        fill_source="broker",
        broker_fill_id="f3",
        fees=fees(),
        settles_on=None,
    )
    assert c is not None and c.position_closed
    pos = dbx.conn.execute("select status, qty from positions where id = %s", (c.position_id,)).fetchone()
    assert pos["status"] == "closed" and Decimal(pos["qty"]) == 0
    lots = dbx.conn.execute(
        "select qty_remaining, closed_at from lots where position_id = %s", (c.position_id,)
    ).fetchall()
    assert all(Decimal(lot["qty_remaining"]) == 0 and lot["closed_at"] for lot in lots)
    kinds = {
        r["kind"]: r["classification"]
        for r in dbx.conn.execute(
            "select kind, classification from fees where fill_id in (%s, %s)", (b.fill_id, c.fill_id)
        ).fetchall()
    }
    assert kinds == {
        "sec_section_31": "customer_debited",
        "finra_taf": "customer_debited",
        "cat": "pass_through_unverified",
    }
    assert (
        dbx.verify_cash_chain(seed.primary_id)
        and dbx.cash_balance(seed.primary_id) == Decimal("500") - 200 + 55 + 180 - b.customer_fees - c.customer_fees
    )
