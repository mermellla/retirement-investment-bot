"""T-16 pre-open re-arm and post-open verification (ADR-0013, A-03); T-41 corporate actions; DRY_RUN observe-only."""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from psycopg.rows import dict_row

from tests.fakes import FakeBroker
from tests.seed import NOW, Seed
from tradeagent.adapters.alpaca.broker_null import NullBroker
from tradeagent.adapters.alpaca.corporate_actions import SplitAction, SymbolChange, parse_actions
from tradeagent.execution.corporate import apply_corporate_actions
from tradeagent.execution.stops import StopArmer
from tradeagent.persistence.db import Database

pytestmark = pytest.mark.db
DAY = date(2026, 9, 18)


@pytest.fixture
def dbx(db):
    db.row_factory = dict_row  # Database expects dict rows; the seed writes only, so sharing the connection is safe
    return Database(db)


def open_position(seed: Seed, symbol: str, qty: str, invalidation: str | None = "95") -> uuid.UUID:
    seed.conn.execute(
        "insert into instruments (symbol, tradable, fractionable, universe_status) values (%s, true, true, 'eligible') on conflict do nothing",
        (symbol,),
    )
    d = seed.decision()
    pid = uuid.uuid4()
    q = Decimal(qty)
    seed.conn.execute(
        """insert into positions (id, portfolio_id, experiment_id, opened_in_phase_id, symbol, entry_decision_id, opened_at, qty, avg_cost, is_fractional, working_invalidation_price, working_target_price)
           values (%s, %s, %s, %s, %s, %s, %s, %s, 100, %s, %s, 110)""",
        (
            pid,
            seed.primary_id,
            seed.experiment_id,
            seed.phase_id,
            symbol,
            d,
            NOW,
            q,
            q != q.to_integral_value(),
            invalidation,
        ),
    )
    return pid


def armer(dbx: Database, seed: Seed, broker) -> StopArmer:
    return StopArmer(
        dbx, broker, seed.experiment_id, seed.phase_id, seed.primary_id, clock=lambda: NOW + timedelta(days=1)
    )


def test_rearm_every_unprotected_lot_fractional_or_not(dbx, seed):
    frac = open_position(seed, "AAPL", "1.5")
    whole = open_position(
        seed, "MSFT", "2"
    )  # whole-share lot whose GTC stop was cancelled for an ext-hours exit (A-03)
    naked = open_position(seed, "NVDA", "1", invalidation=None)
    broker = FakeBroker(submit_status="accepted")
    actions = asyncio.run(
        armer(dbx, seed, broker).rearm(DAY, {"AAPL": Decimal("100"), "MSFT": Decimal("100"), "NVDA": Decimal("100")})
    )
    by = {a.symbol: a for a in actions}
    assert by["AAPL"].action == "armed" and by["MSFT"].action == "armed" and by["NVDA"].action == "no_invalidation"
    reqs = {r.symbol: r for r in broker.submitted}
    assert (
        reqs["AAPL"].time_in_force.value == "day"
        and reqs["AAPL"].qty == Decimal("1.5")
        and reqs["AAPL"].stop_price == Decimal("95")
    )
    assert reqs["MSFT"].time_in_force.value == "gtc" and reqs["MSFT"].order_type.value == "stop"
    cov = {
        r["position_id"]: r
        for r in dbx.conn.execute("select * from stop_coverage where session_date = %s", (DAY,)).fetchall()
    }
    assert cov[frac]["confirmed_active_at"] is not None and cov[frac]["broker_status_at_arm"] == "alpaca:accepted"
    assert cov[naked]["incident_code"] == "NO_INVALIDATION_LEVEL"
    dec = dbx.conn.execute("select origin, kind, decision from decisions where position_id = %s", (whole,)).fetchone()
    assert (dec["origin"], dec["kind"], dec["decision"]) == ("system", "system_exit", "SELL")
    # second run: existing live stop is confirmed, nothing resubmitted
    actions2 = asyncio.run(armer(dbx, seed, broker).rearm(DAY, {}))
    assert {a.symbol: a.action for a in actions2 if a.symbol == "AAPL"} == {"AAPL": "confirmed_existing"} and len(
        broker.submitted
    ) == 2


def test_gap_below_invalidation_sells_at_open(dbx, seed):
    pid = open_position(seed, "AAPL", "1.5", invalidation="95")
    broker = FakeBroker()
    actions = asyncio.run(armer(dbx, seed, broker).rearm(DAY, {"AAPL": Decimal("90")}))
    assert (
        actions[0].action == "gap_market_sell"
        and broker.submitted[0].order_type.value == "market"
        and broker.submitted[0].purpose.value == "software_stop"
    )
    assert (
        dbx.conn.execute("select incident_code from stop_coverage where position_id = %s", (pid,)).fetchone()[
            "incident_code"
        ]
        == "GAP_BELOW_INVALIDATION"
    )


def test_post_open_verification_live_pending_and_software_stop(dbx, seed):
    live = open_position(seed, "AAPL", "1.5")
    stuck = open_position(seed, "MSFT", "0.7")
    broker = FakeBroker(submit_status="accepted")
    a = armer(dbx, seed, broker)
    asyncio.run(a.rearm(DAY, {}))
    cids = {r.symbol: r.client_order_id for r in broker.submitted}
    broker.status_sequence = {cids["AAPL"]: ["new"], cids["MSFT"]: ["accepted", "accepted", "accepted"]}
    first = {
        x.symbol: x.action
        for x in asyncio.run(a.verify_post_open(DAY, {"AAPL": Decimal("100"), "MSFT": Decimal("100")}, final=False))
    }
    assert first == {"AAPL": "live", "MSFT": "pending"}
    final = {
        x.symbol: x
        for x in asyncio.run(a.verify_post_open(DAY, {"AAPL": Decimal("100"), "MSFT": Decimal("94")}, final=True))
    }
    assert final["MSFT"].action == "software_stop" and final["MSFT"].broker_status == "alpaca:accepted"
    soft = [r for r in broker.submitted if r.purpose.value == "software_stop"]
    assert len(soft) == 1 and soft[0].order_type.value == "market" and soft[0].qty == Decimal("0.7")
    cov = {
        r["position_id"]: r
        for r in dbx.conn.execute("select * from stop_coverage where session_date = %s", (DAY,)).fetchall()
    }
    assert (
        cov[live]["verification_result"] == "live"
        and cov[stuck]["verification_result"] == "software_stop_placed"
        and cov[stuck]["incident_code"] == "STOP_MISSING_SOFTWARE_STOP"
    )


def test_post_open_missing_above_invalidation_rearms_and_monitors(dbx, seed):
    pid = open_position(seed, "AAPL", "1.5")
    broker = FakeBroker()
    a = armer(dbx, seed, broker)
    asyncio.run(a.rearm(DAY, {}))
    cid = broker.submitted[0].client_order_id
    broker.status_sequence = {cid: ["canceled"]}
    out = asyncio.run(a.verify_post_open(DAY, {"AAPL": Decimal("100")}, final=True))
    assert (
        out[0].action == "missing_rearmed"
        and len(broker.submitted) == 2
        and broker.submitted[1].order_type.value == "stop"
    )
    assert (
        dbx.conn.execute("select incident_code from stop_coverage where position_id = %s", (pid,)).fetchone()[
            "incident_code"
        ]
        == "STOP_MISSING_MONITORING"
    )


def test_dry_run_records_intent_without_submitting(dbx, seed):
    open_position(seed, "AAPL", "1.5")
    a = armer(dbx, seed, NullBroker())
    actions = asyncio.run(a.rearm(DAY, {"AAPL": Decimal("100")}))
    assert actions[0].action == "dry_run_would_arm" and actions[0].broker_status == "dry_run:would_submit"
    o = dbx.conn.execute(
        "select is_simulated, status, purpose from orders where id = %s", (actions[0].order_id,)
    ).fetchone()
    assert o["is_simulated"] is True and o["status"] == "FILL_PENDING_RECONSTRUCTION" and o["purpose"] == "stop_rearm"
    assert asyncio.run(a.verify_post_open(DAY, {}, final=True))[0].action == "live"


def test_corporate_actions_split_and_symbol_change(dbx, seed):
    pid = open_position(seed, "AAPL", "3", invalidation="90")
    seed.conn.execute("update positions set working_target_price = 120 where id = %s", (pid,))
    d = dbx.conn.execute("select entry_decision_id from positions where id = %s", (pid,)).fetchone()[
        "entry_decision_id"
    ]
    o = seed.order(d, is_simulated=False, notional=Decimal("300"), reserved_notional_usd=Decimal("300"))
    seed.transition(o, "VALIDATED")
    splits, changes = parse_actions(
        {
            "corporate_actions": {
                "forward_splits": [
                    {
                        "symbol": "AAPL",
                        "ex_date": "2026-09-18",
                        "new_rate": "4",
                        "old_rate": "1",
                        "process_date": "2026-09-18",
                    }
                ],
                "name_changes": [{"old_symbol": "AAPL", "new_symbol": "AAPQ", "process_date": "2026-09-18"}],
            }
        }
    )
    assert splits[0].ratio == 4 and changes[0].new_symbol == "AAPQ"
    applied = apply_corporate_actions(dbx, seed.primary_id, DAY, splits, changes)
    pos = dbx.conn.execute(
        "select symbol, qty, avg_cost, working_invalidation_price, working_target_price from positions where id = %s",
        (pid,),
    ).fetchone()
    assert (
        pos["symbol"],
        Decimal(pos["qty"]),
        Decimal(pos["avg_cost"]),
        Decimal(pos["working_invalidation_price"]),
        Decimal(pos["working_target_price"]),
    ) == ("AAPQ", Decimal("12"), Decimal("25"), Decimal("22.5"), Decimal("30"))
    assert [a["kind"] for a in applied] == ["forward_split", "symbol_change"]
    assert apply_corporate_actions(dbx, seed.primary_id, DAY, splits, []) == []  # idempotent
    future = [SplitAction("AAPQ", "reverse_split", DAY + timedelta(days=3), Decimal(1), Decimal(10))]
    assert (
        apply_corporate_actions(
            dbx, seed.primary_id, DAY, future, [SymbolChange("AAPQ", "ZZZ", DAY + timedelta(days=3))]
        )
        == []
    )
