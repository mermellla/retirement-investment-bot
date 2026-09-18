"""`tradeagent` command line: boot-check | run | verify-projections."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import date
from uuid import UUID

from tradeagent.adapters.alpaca.calendar import StaticCalendar, default_window
from tradeagent.config import LiveLockedOut, load_settings
from tradeagent.execution.broker_factory import make_broker, paper_credentials
from tradeagent.ops.boot import BootHalt, boot
from tradeagent.ops.scheduler import Scheduler
from tradeagent.persistence.db import Database, connect

log = logging.getLogger("tradeagent")


def _code_version(env: dict[str, str]) -> str:
    return env.get("RAILWAY_GIT_COMMIT_SHA") or env.get("TRADEAGENT_CODE_VERSION") or "dev"


def _calendar(env: dict[str, str]) -> StaticCalendar:
    from tradeagent.adapters.alpaca.calendar import AlpacaCalendar
    from tradeagent.adapters.alpaca.client import AlpacaClient

    creds = paper_credentials(env)
    if creds is None:
        raise SystemExit("ALPACA_PAPER_KEY/SECRET are required to load the trading calendar (§11); DRY_RUN reads only")
    start, end = default_window(date.today())
    return AlpacaCalendar(AlpacaClient(creds), start, end)


def cmd_boot_check(env: dict[str, str]) -> int:
    settings = load_settings(env=env)
    db = Database(connect(env["DATABASE_URL"]))
    broker = make_broker(settings, env)
    try:
        report = boot(settings, env, db, broker, code_version=_code_version(env))
    except BootHalt as halt:
        log.error("HALT %s (%s): %s", halt.code, halt.scope.value, halt.detail)
        return 2
    log.info(
        "boot ok: experiment=%s phase=%s (seq %s, new=%s) portfolios=%s opening_balances=%s",
        report.experiment_id,
        report.phase_id,
        report.phase_seq,
        report.opened_new_phase,
        report.portfolios,
        report.opening_balances_written,
    )
    for c in report.checks:
        log.info("check %s: %s", c.name, c.detail)
    for n in report.notes:
        log.warning(n)
    return 0


def cmd_run(env: dict[str, str]) -> int:
    rc = cmd_boot_check(env)
    if rc != 0:
        return rc
    settings = load_settings(env=env)
    scheduler = Scheduler(_calendar(env), settings.risk.scanner.scan_interval_min)
    log.info(
        "scheduler started with %d job(s); next event %s", len(scheduler.jobs), scheduler.next_event(scheduler.clock())
    )
    asyncio.run(scheduler.run_forever())
    return 0


def cmd_probe_fractional_stop(env: dict[str, str]) -> int:
    """ADR-0013 empirical probe (OI-01): one fractional Day stop on the paper account, status sequence recorded, then cancelled.
    Requires PAPER keys and --confirm; never runs in this codebase's LIVE (there is none)."""
    import json
    import time as _time

    from tradeagent.adapters.alpaca.broker_paper import AlpacaPaperBroker
    from tradeagent.adapters.alpaca.client import AlpacaClient, AlpacaHttpError

    if env.get("TRADEAGENT_PROBE_CONFIRM") != "yes":
        raise SystemExit("set TRADEAGENT_PROBE_CONFIRM=yes to submit one fractional stop order to the PAPER account")
    creds = paper_credentials(env)
    if creds is None:
        raise SystemExit("ALPACA_PAPER_KEY/SECRET required")
    client = AlpacaClient(creds)
    broker = AlpacaPaperBroker(client)
    symbol = env.get("TRADEAGENT_PROBE_SYMBOL", "AAPL")
    # a fractional long must exist to sell against: buy 0.5 share notional-free market order first, then arm a far stop
    trace: list[dict[str, object]] = []
    try:
        buy = client.post(
            "/v2/orders",
            {
                "symbol": symbol,
                "qty": "0.5",
                "side": "buy",
                "type": "market",
                "time_in_force": "day",
                "client_order_id": f"probe-buy-{int(_time.time())}",
            },
        )
        trace.append({"step": "buy", "status": buy.get("status"), "id": buy.get("id")})
        _time.sleep(3)
        last = client.get(
            "/v2/stocks/trades/latest", {"symbols": symbol, "feed": "iex"}, base="https://data.alpaca.markets"
        )["trades"][symbol]["p"]
        stop = client.post(
            "/v2/orders",
            {
                "symbol": symbol,
                "qty": "0.5",
                "side": "sell",
                "type": "stop",
                "time_in_force": "day",
                "stop_price": f"{float(last) * 0.5:.2f}",
                "client_order_id": f"probe-stop-{int(_time.time())}",
            },
        )
        trace.append({"step": "stop_submit", "status": stop.get("status"), "id": stop.get("id")})
        for i in range(3):
            _time.sleep(2)
            st = asyncio.run(broker.order_by_broker_id(str(stop["id"])))
            trace.append({"step": f"stop_status_{i}", "status": st.status_reason if st else None})
        client.delete(f"/v2/orders/{stop['id']}")
        trace.append({"step": "stop_cancel", "ok": True})
    except AlpacaHttpError as exc:
        trace.append({"step": "error", "status": exc.status, "body": exc.body})
    print(json.dumps({"symbol": symbol, "trace": trace}, indent=2))
    return 0


def cmd_verify_projections(env: dict[str, str]) -> int:
    db = Database(connect(env["DATABASE_URL"]))
    rows = db.conn.execute("select id, name from portfolios").fetchall()
    bad = [r["name"] for r in rows if not db.verify_cash_chain(UUID(str(r["id"])))]
    print("PROJECTION_DRIFT" if bad else "ok", bad)
    return 2 if bad else 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="tradeagent")
    parser.add_argument("command", choices=["boot-check", "run", "verify-projections", "probe-fractional-stop"])
    args = parser.parse_args(argv)
    env = dict(os.environ)
    try:
        return {"boot-check": cmd_boot_check, "run": cmd_run, "verify-projections": cmd_verify_projections}[
            args.command
        ](env)
    except LiveLockedOut as exc:
        log.error("%s", exc)
        return 3


if __name__ == "__main__":
    sys.exit(main())
