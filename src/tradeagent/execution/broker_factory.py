"""Broker selection by EXECUTION_MODE (ADR-0020). There is no LIVE implementation and no LIVE credential lookup."""

from __future__ import annotations

from tradeagent.adapters.alpaca.broker_null import NullBroker
from tradeagent.adapters.alpaca.broker_paper import AlpacaPaperBroker
from tradeagent.adapters.alpaca.client import AlpacaClient, AlpacaCredentials
from tradeagent.config import Settings
from tradeagent.domain.enums import ExecutionMode
from tradeagent.interfaces import Broker, LiveLockedOutError


def paper_credentials(env: dict[str, str]) -> AlpacaCredentials | None:
    key, secret = env.get("ALPACA_PAPER_KEY"), env.get("ALPACA_PAPER_SECRET")
    if key and secret:
        return AlpacaCredentials(key, secret)
    return None


def make_broker(settings: Settings, env: dict[str, str]) -> Broker:
    mode = settings.risk.execution.execution_mode
    if mode == ExecutionMode.LIVE:
        raise LiveLockedOutError("LIVE is not enabled in this codebase (ADR-0020)")
    creds = paper_credentials(env)
    if mode == ExecutionMode.DRY_RUN:
        return NullBroker(AlpacaClient(creds) if creds else None)
    if creds is None:
        raise RuntimeError("PAPER mode requires ALPACA_PAPER_KEY / ALPACA_PAPER_SECRET in the environment")
    return AlpacaPaperBroker(AlpacaClient(creds))
