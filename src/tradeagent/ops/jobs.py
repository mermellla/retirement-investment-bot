"""Scheduler job wiring (Slice 2): pre-open and intraday scans. Adapters are built from settings and the environment
here and nowhere else (composition root, ADR-0015)."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import partial
from typing import Any
from uuid import UUID

from tradeagent.adapters.alpaca.assets import AlpacaAssets
from tradeagent.adapters.alpaca.client import AlpacaClient
from tradeagent.adapters.alpaca.market_data import RequestBudget, make_market_data
from tradeagent.adapters.alpaca.news import AlpacaNews
from tradeagent.adapters.edgar.client import EdgarClient, last_10k_filed_on, sic_of
from tradeagent.adapters.finnhub.earnings import FinnhubEarnings
from tradeagent.adapters.typesafe.jev import JevClient
from tradeagent.config import Settings
from tradeagent.data.registry import SourceRegistry
from tradeagent.domain.enums import FeedTier
from tradeagent.exclusions import ExclusionScreen
from tradeagent.ops.scheduler import Scheduler
from tradeagent.persistence.db import Database
from tradeagent.scanner.runner import ScanRunner
from tradeagent.scanner.scanner import Scanner
from tradeagent.scanner.signals.catalyst import CatalystClassifier, KeywordRule
from tradeagent.universe.builder import EdgarFacts, Floors

log = logging.getLogger("tradeagent.jobs")


def build_catalyst_classifier(settings: Settings, env: dict[str, str], prompt_version: str) -> CatalystClassifier:
    cat = settings.scanner["catalyst"]
    jev = JevClient(
        env.get("TYPESAFE_API_KEY"),
        model=str(cat.get("model", "jev-latest")),
        prompt_version=prompt_version,
        cost_per_1k_tokens_usd=Decimal(str(settings.risk.budget.jev_cost_per_1k_tokens_usd)),
    )
    kw = KeywordRule(cat["keyword_fallback"]["positive"], cat["keyword_fallback"]["negative"])
    return CatalystClassifier(
        jev, kw, str(cat["classifier"]), float(cat["material_threshold"]), int(cat["max_headlines_per_symbol"])
    )


def build_runner(
    settings: Settings,
    env: dict[str, str],
    db: Database,
    client: AlpacaClient,
    experiment_id: UUID,
    phase_id: UUID,
    prompt_version: str,
) -> ScanRunner:
    plan = settings.risk.market_data.market_data_plan
    budget = RequestBudget()
    sip, iex = make_market_data(client, plan, budget)
    registry = SourceRegistry()
    registry.register("bars_quotes", sip.health, iex.health if iex is not sip else None, required_for_entries=True)
    news = AlpacaNews(client)
    registry.register("news", news.health)
    ua = env.get("EDGAR_USER_AGENT")
    if not ua:
        raise RuntimeError(
            "EDGAR_USER_AGENT is required (SEC fair-access policy: app name plus a contact email); set it in the environment"
        )
    edgar = EdgarClient(ua)
    registry.register("filings", edgar.health)
    finnhub = FinnhubEarnings(env.get("FINNHUB_API_KEY"))
    registry.register("earnings_calendar", finnhub.health)
    catalyst = build_catalyst_classifier(settings, env, prompt_version)
    if catalyst.jev is not None:
        registry.register("judgments", catalyst.jev.health)
    screen = ExclusionScreen.from_config(settings.exclusions, settings.sic_backstop)
    u = settings.risk.universe
    floors = Floors(
        Decimal(str(u.min_price_usd)),
        Decimal(str(u.min_avg_dollar_volume_usd)),
        u.min_history_days,
        u.require_fractionable,
    )
    tier = FeedTier.SIP_REALTIME if plan == "algo_trader_plus" else FeedTier.SIP_DELAYED
    scanner = Scanner(
        settings.scanner,
        tier,
        settings.risk.scanner.scan_top_n,
        settings.versions.exclusion_list_version,
        catalyst,
        lambda: datetime.now(tz=UTC),
    )
    assets = AlpacaAssets(client)

    def edgar_facts(shaped: list[Any]) -> dict[str, EdgarFacts]:
        cmap = edgar.ticker_map()
        out: dict[str, EdgarFacts] = {}
        for a in shaped:
            cik = cmap.get(a.symbol)
            if cik is None:
                continue
            try:
                sub = edgar.submissions(cik)
            except Exception as exc:
                log.warning("EDGAR submissions %s failed: %s", a.symbol, exc)
                continue
            out[a.symbol] = EdgarFacts(cik, sic_of(sub), last_10k_filed_on(sub), str(sub.get("name") or a.name))
        return out

    def earnings(start: date, end: date) -> dict[str, date]:
        if not finnhub.available:
            return {}
        return {sym: min(e.on for e in evs) for sym, evs in finnhub.calendar(start, end).items()}

    return ScanRunner(
        db,
        scanner,
        sip,
        iex,
        registry,
        floors,
        screen,
        experiment_id,
        phase_id,
        prompt_version,
        headlines_fn=news.headlines,
        earnings_fn=earnings,
        assets_fn=assets.active_us_equities,
        edgar_fn=edgar_facts,
        budget=budget,
        history_days=u.min_history_days,
        news_age_hours=settings.risk.staleness.max_news_age_hours,
    )


def register_scan_jobs(scheduler: Scheduler, runner: ScanRunner) -> None:
    async def preopen(at: datetime) -> None:
        runner.run_and_persist("preopen", at.astimezone(UTC), at.date())

    async def intraday(at: datetime) -> None:
        runner.run_and_persist("intraday", at.astimezone(UTC), at.date())

    scheduler.register("preopen_scan", preopen)
    scheduler.register("intraday_scan", partial(intraday))


def register_execution_jobs(
    scheduler: Scheduler,
    db: Database,
    broker: Any,
    client: AlpacaClient,
    iex: Any,
    experiment_id: UUID,
    phase_id: UUID,
    sip: Any,
) -> None:
    """Slice 3: corporate actions → stop re-arm → post-open verification (ADR-0013 sequence)."""
    from datetime import timedelta

    from tradeagent.adapters.alpaca.corporate_actions import AlpacaCorporateActions
    from tradeagent.execution.corporate import apply_corporate_actions
    from tradeagent.execution.stops import StopArmer

    primary = db.primary_portfolio(experiment_id)
    portfolio_id = UUID(str(primary["id"]))
    armer = StopArmer(db, broker, experiment_id, phase_id, portfolio_id)
    ca = AlpacaCorporateActions(client)

    def symbols() -> list[str]:
        return [p["symbol"] for p in db.open_positions(portfolio_id)]

    async def corporate_actions(at: datetime) -> None:
        syms = symbols()
        if not syms:
            return
        splits, changes = ca.for_symbols(syms, at.date() - timedelta(days=7), at.date())
        with db.transaction():
            applied = apply_corporate_actions(db, portfolio_id, at.date(), splits, changes)
        log.info("corporate actions applied: %s", applied)

    async def stop_rearm(at: datetime) -> None:
        syms = symbols()
        prior: dict[str, Decimal] = {}
        if syms:
            end = at.astimezone(UTC) - sip.embargo
            for b in sip.bars(syms, "1Day", end - timedelta(days=6), end):
                prior[b.symbol] = b.close
        with db.transaction():
            actions = await armer.rearm(at.date(), prior)
        log.info("stop re-arm: %s", [(a.symbol, a.action, a.broker_status) for a in actions])

    async def verify(at: datetime, final: bool) -> None:
        syms = symbols()
        last = {s: t.price for s, t in iex.latest_trades(syms).items()} if syms else {}
        with db.transaction():
            actions = await armer.verify_post_open(at.date(), last, final)
        log.info(
            "post-open verification (final=%s): %s", final, [(a.symbol, a.action, a.broker_status) for a in actions]
        )

    scheduler.register("corporate_actions", corporate_actions)
    scheduler.register("stop_rearm", stop_rearm)
    scheduler.register("post_open_verify_1", partial(verify, final=False))
    scheduler.register("post_open_verify_2", partial(verify, final=True))
