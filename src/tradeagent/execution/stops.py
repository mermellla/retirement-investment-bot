"""§8.3 / A-03 / ADR-0013: pre-open re-arm of broker-side protection for every unprotected open lot, mandatory post-open
verification, software stop when a lot is found naked. DRY_RUN (observe-only broker) records what it would submit."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from tradeagent.adapters.alpaca.broker_null import NoBrokerInDryRun
from tradeagent.domain.enums import OrderPurpose, OrderSide, OrderStatus, OrderType, TimeInForce
from tradeagent.domain.models import OrderRequest
from tradeagent.interfaces import Broker
from tradeagent.persistence.db import Database

log = logging.getLogger("tradeagent.stops")
LIVE_STATUSES = {"alpaca:new", "alpaca:partially_filled", "alpaca:filled"}
PENDING_STATUSES = {"alpaca:accepted", "alpaca:pending_new"}


@dataclass
class StopAction:
    symbol: str
    position_id: UUID
    action: str  # confirmed_existing | armed | gap_market_sell | dry_run_would_arm | no_invalidation | live | pending | software_stop | missing_rearmed
    order_id: UUID | None = None
    broker_status: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


class StopArmer:
    def __init__(
        self,
        db: Database,
        broker: Broker,
        experiment_id: UUID,
        phase_id: UUID,
        portfolio_id: UUID,
        clock: Callable[[], datetime] | None = None,
    ):
        self.db, self.broker = db, broker
        self.experiment_id, self.phase_id, self.portfolio_id = experiment_id, phase_id, portfolio_id
        self.clock = clock or (lambda: datetime.now(tz=UTC))

    # ---- helpers
    def _sell_order(
        self,
        pos: dict[str, Any],
        purpose: OrderPurpose,
        order_type: OrderType,
        stop_price: Decimal | None,
        reason: str,
        tif: TimeInForce | None = None,
    ) -> tuple[UUID, OrderRequest]:
        now = self.clock()
        did = self.db.create_system_decision(
            self.experiment_id,
            self.phase_id,
            self.portfolio_id,
            pos["symbol"],
            "SELL",
            UUID(str(pos["id"])),
            reason,
            self.db.phase_versions(self.phase_id),
            now,
        )
        qty = Decimal(pos["qty"])
        fractional = qty != qty.to_integral_value()
        req = OrderRequest(
            decision_id=did,
            portfolio_id=self.portfolio_id,
            purpose=purpose,
            symbol=pos["symbol"],
            side=OrderSide.SELL,
            order_type=order_type,
            time_in_force=tif or (TimeInForce.DAY if fractional else TimeInForce.GTC),
            qty=qty,
            stop_price=stop_price,
            is_simulated=self.broker.observe_only,
            order_eligible_at=now,
        )
        oid = self.db.record_order(req, self.experiment_id, self.phase_id, UUID(str(pos["id"])), Decimal(0))
        self.db.transition_order(oid, OrderStatus.VALIDATED, reason)
        return oid, req

    async def _submit(self, oid: UUID, req: OrderRequest) -> str:
        """Returns the broker status string; in DRY_RUN records the intent without submitting (§16)."""
        if self.broker.observe_only:
            self.db.transition_order(
                oid, OrderStatus.FILL_PENDING_RECONSTRUCTION, "dry_run: protective order simulated"
            )
            return "dry_run:would_submit"
        try:
            state = await self.broker.submit(req)
        except NoBrokerInDryRun:
            return "dry_run:would_submit"
        self.db.transition_order(
            oid,
            OrderStatus.SUBMITTED,
            state.status_reason or "submitted",
            broker_order_id=state.broker_order_id,
            submitted_at=state.submitted_at or self.clock(),
        )
        return state.status_reason or "submitted"

    # ---- §8.3 pre-open re-arm (A-03: any unprotected lot, fractional or not)
    async def rearm(self, session_date: date, prior_closes: dict[str, Decimal]) -> list[StopAction]:
        actions: list[StopAction] = []
        for pos in self.db.open_positions(self.portfolio_id):
            pid = UUID(str(pos["id"]))
            inv = pos["working_invalidation_price"]
            if inv is None:
                self.db.record_stop_coverage(
                    session_date, pid, None, None, verification_result="missing", incident_code="NO_INVALIDATION_LEVEL"
                )
                actions.append(StopAction(pos["symbol"], pid, "no_invalidation"))
                continue
            inv = Decimal(inv)
            live = None
            for po in self.db.protective_orders(pid):
                state = await self.broker.order_status(po["client_order_id"]) if not self.broker.observe_only else None
                if state is not None and state.status in (OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED):
                    live = (po, state)
                    break
                if state is not None:
                    self.db.transition_order(
                        UUID(str(po["id"])), state.status, f"pre-open refresh: {state.status_reason}"
                    )
            if live is not None:
                po, state = live
                self.db.record_stop_coverage(
                    session_date,
                    pid,
                    None,
                    UUID(str(po["id"])),
                    armed_at=po["submitted_at"],
                    broker_status_at_arm=state.status_reason,
                    confirmed_active_at=self.clock(),
                )
                actions.append(
                    StopAction(pos["symbol"], pid, "confirmed_existing", UUID(str(po["id"])), state.status_reason)
                )
                continue
            prior = prior_closes.get(pos["symbol"])
            if prior is not None and prior <= inv:
                oid, req = self._sell_order(
                    pos,
                    OrderPurpose.SOFTWARE_STOP,
                    OrderType.MARKET,
                    None,
                    f"gap below invalidation {inv} (prior close {prior}); market sell at the open",
                    TimeInForce.DAY,
                )
                status = await self._submit(oid, req)
                self.db.record_stop_coverage(
                    session_date,
                    pid,
                    None,
                    oid,
                    armed_at=self.clock(),
                    broker_status_at_arm=status,
                    incident_code="GAP_BELOW_INVALIDATION",
                    verification_result="software_stop_placed"
                    if not status.startswith("dry_run")
                    else "not_applicable",
                )
                actions.append(
                    StopAction(
                        pos["symbol"],
                        pid,
                        "gap_market_sell",
                        oid,
                        status,
                        {"prior_close": str(prior), "invalidation": str(inv)},
                    )
                )
                continue
            oid, req = self._sell_order(
                pos, OrderPurpose.STOP_REARM, OrderType.STOP, inv, f"pre-open stop re-arm at {inv}"
            )
            status = await self._submit(oid, req)
            confirmed = self.clock() if status in LIVE_STATUSES | PENDING_STATUSES else None
            self.db.record_stop_coverage(
                session_date,
                pid,
                None,
                oid,
                armed_at=self.clock(),
                broker_status_at_arm=status,
                confirmed_active_at=confirmed,
                verification_result="not_applicable" if status.startswith("dry_run") else None,
            )
            actions.append(
                StopAction(
                    pos["symbol"], pid, "dry_run_would_arm" if status.startswith("dry_run") else "armed", oid, status
                )
            )
        return actions

    # ---- §8.3 mandatory post-open verification
    async def verify_post_open(
        self, session_date: date, last_prices: dict[str, Decimal], final: bool
    ) -> list[StopAction]:
        actions: list[StopAction] = []
        for pos in self.db.open_positions(self.portfolio_id):
            pid = UUID(str(pos["id"]))
            inv = pos["working_invalidation_price"]
            if inv is None:
                continue
            inv = Decimal(inv)
            if self.broker.observe_only:
                self.db.record_stop_coverage(
                    session_date,
                    pid,
                    None,
                    None,
                    verified_post_open_at=self.clock(),
                    verification_result="not_applicable",
                )
                actions.append(StopAction(pos["symbol"], pid, "live", None, "dry_run"))
                continue
            status: str | None = None
            live_oid: UUID | None = None
            for po in self.db.protective_orders(pid):
                state = await self.broker.order_status(po["client_order_id"])
                if state is None:
                    continue
                status = state.status_reason
                live_oid = UUID(str(po["id"]))
                if status in LIVE_STATUSES:
                    break
            if status in LIVE_STATUSES:
                self.db.record_stop_coverage(
                    session_date, pid, None, live_oid, verified_post_open_at=self.clock(), verification_result="live"
                )
                actions.append(StopAction(pos["symbol"], pid, "live", live_oid, status))
            elif status in PENDING_STATUSES and not final:
                actions.append(StopAction(pos["symbol"], pid, "pending", live_oid, status))
            else:
                # naked lot: the software is the stop (§8.3, §15)
                price = last_prices.get(pos["symbol"])
                if price is not None and price <= inv:
                    oid, req = self._sell_order(
                        pos,
                        OrderPurpose.SOFTWARE_STOP,
                        OrderType.MARKET,
                        None,
                        f"software stop: last {price} ≤ invalidation {inv}, no live broker stop",
                        TimeInForce.DAY,
                    )
                    st = await self._submit(oid, req)
                    self.db.record_stop_coverage(
                        session_date,
                        pid,
                        None,
                        oid,
                        verified_post_open_at=self.clock(),
                        verification_result="software_stop_placed",
                        incident_code="STOP_MISSING_SOFTWARE_STOP",
                    )
                    actions.append(
                        StopAction(
                            pos["symbol"], pid, "software_stop", oid, st, {"last": str(price), "invalidation": str(inv)}
                        )
                    )
                else:
                    oid, req = self._sell_order(
                        pos,
                        OrderPurpose.STOP_REARM,
                        OrderType.STOP,
                        inv,
                        "post-open: stop missing, re-arming; software monitors until live",
                    )
                    st = await self._submit(oid, req)
                    self.db.record_stop_coverage(
                        session_date,
                        pid,
                        None,
                        oid,
                        verified_post_open_at=self.clock(),
                        verification_result="missing",
                        incident_code="STOP_MISSING_MONITORING",
                    )
                    actions.append(
                        StopAction(
                            pos["symbol"], pid, "missing_rearmed", oid, st, {"last": str(price) if price else None}
                        )
                    )
        return actions
