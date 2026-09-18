"""§15 corporate actions applied before the pre-open stop re-arm: splits and symbol changes on open positions."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from tradeagent.adapters.alpaca.corporate_actions import SplitAction, SymbolChange
from tradeagent.persistence.db import Database


def apply_corporate_actions(
    db: Database, portfolio_id: UUID, session_date: date, splits: list[SplitAction], changes: list[SymbolChange]
) -> list[dict[str, Any]]:
    applied: list[dict[str, Any]] = []
    positions = {p["symbol"]: p for p in db.open_positions(portfolio_id)}
    now = datetime.now(tz=UTC)
    for sp in splits:
        pos = positions.get(sp.symbol)
        if pos is None or sp.ex_date > session_date:
            continue
        key = f"split:{sp.symbol}:{sp.ex_date}:{sp.new_rate}/{sp.old_rate}"
        if db.conn.execute(
            "select 1 from lot_events e join lots l on l.id = e.lot_id where l.position_id = %s and e.kind = 'split_adjust' and e.reason = %s",
            (pos["id"], key),
        ).fetchone():
            continue
        db.apply_split(UUID(str(pos["id"])), sp.ratio, now, key)
        applied.append({"kind": sp.kind, "symbol": sp.symbol, "ratio": str(sp.ratio)})
    for ch in changes:
        pos = positions.get(ch.old_symbol)
        if pos is None or ch.process_date > session_date:
            continue
        db.change_symbol(UUID(str(pos["id"])), ch.new_symbol, now)
        applied.append({"kind": "symbol_change", "from": ch.old_symbol, "to": ch.new_symbol})
    return applied
