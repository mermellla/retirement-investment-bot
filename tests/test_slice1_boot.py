"""Slice 1: boot spine (T-21, T-54 application half, RECONCILE_HALT on unknown broker state, LIVE refusal)."""

from __future__ import annotations

import shutil
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from tests.fakes import FakeBroker
from tradeagent.config import load_settings
from tradeagent.domain.enums import ExecutionMode
from tradeagent.domain.models import Position
from tradeagent.execution.broker_factory import make_broker
from tradeagent.interfaces import LiveLockedOutError
from tradeagent.ops import boot as bootmod
from tradeagent.ops.boot import BootHalt, boot
from tradeagent.persistence.db import Database, connect

pytestmark = pytest.mark.db
psycopg = pytest.importorskip("psycopg")
REPO = Path(__file__).resolve().parents[1]
LOCAL_ENV = {"TRADEAGENT_LOCAL_DB": "true"}


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private copy of config/ and prompts/ so tests can mutate versions without touching the repo."""
    cfg, prm = tmp_path / "config", tmp_path / "prompts"
    shutil.copytree(REPO / "config", cfg)
    shutil.copytree(REPO / "prompts", prm)
    monkeypatch.setattr("tradeagent.versioning.registry.PROMPTS_DIR", prm)
    monkeypatch.setattr("tradeagent.versioning.phases.PHASE_CHANGE_PATH", cfg / "phase_change.yaml")
    return tmp_path


@pytest.fixture
def dbx(migrated_db_url):
    conn = connect(migrated_db_url)
    yield Database(conn)
    conn.rollback()
    conn.close()


def settings_from(workdir: Path):
    return load_settings(workdir / "config", env={})


class FlatBroker(FakeBroker):
    """Slice 1 boot tests run in DRY_RUN: the null-broker posture (observe only, nothing at the broker)."""

    mode = ExecutionMode.DRY_RUN
    observe_only = True


def do_boot(dbx: Database, workdir: Path, name: str, code_version: str = "sha-1"):
    env = {**LOCAL_ENV, "EXPERIMENT_ID": name}
    return boot(settings_from(workdir), env, dbx, FlatBroker(), code_version=code_version, today=date(2026, 9, 14))  # type: ignore[arg-type]


def test_first_boot_creates_experiment_portfolios_balances_versions_phase(dbx, workdir):
    name = f"exp-{uuid.uuid4().hex[:6]}"
    r = do_boot(dbx, workdir, name)
    assert r.opened_new_phase and r.phase_seq == 1 and r.portfolios == 8 and r.opening_balances_written == 8
    assert r.reconciliation.value == "agree"
    for p in dbx.portfolios(r.experiment_id):
        assert dbx.cash_balance(uuid.UUID(str(p["id"]))) == Decimal("500")
    phase = dbx.open_phase(r.experiment_id)
    assert (
        phase["category"] == "initial"
        and phase["code_version"] == "sha-1"
        and phase["migration_version"] == "20260913000011"
    )
    # second boot with nothing changed: same phase, no new balances
    r2 = do_boot(dbx, workdir, name)
    assert not r2.opened_new_phase and r2.phase_id == r.phase_id and r2.opening_balances_written == 0


def test_config_change_without_reason_halts(dbx, workdir):
    name = f"exp-{uuid.uuid4().hex[:6]}"
    do_boot(dbx, workdir, name)
    rp = workdir / "config" / "risk_policy.yaml"
    d = yaml.safe_load(rp.read_text())
    d["scanner"]["scan_top_n"] = 30
    rp.write_text(yaml.safe_dump(d))
    with pytest.raises(BootHalt) as exc:
        do_boot(dbx, workdir, name)
    assert exc.value.code == "PHASE_REASON_MISSING"
    exp = dbx.get_experiment(name)
    assert [h["code"] for h in dbx.open_halts(uuid.UUID(str(exp["id"])))] == ["PHASE_REASON_MISSING"]


def test_config_change_with_reason_opens_phase_and_decisions_carry_it(dbx, workdir):
    name = f"exp-{uuid.uuid4().hex[:6]}"
    r1 = do_boot(dbx, workdir, name)
    rp = workdir / "config" / "risk_policy.yaml"
    d = yaml.safe_load(rp.read_text())
    d["scanner"]["scan_top_n"] = 30
    rp.write_text(yaml.safe_dump(d))
    new_cfg = settings_from(workdir).versions.config_version
    (workdir / "config" / "phase_change.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "sequence": 2,
                "category": "strategy",
                "reason": "raise SCAN_TOP_N to 30",
                "for_versions": {"config_version": new_cfg},
            }
        )
    )
    r2 = do_boot(dbx, workdir, name)
    assert r2.opened_new_phase and r2.phase_seq == 2 and r2.phase_id != r1.phase_id
    old = dbx.conn.execute("select ended_at from experiment_phases where id = %s", (r1.phase_id,)).fetchone()
    assert old["ended_at"] is not None
    new = dbx.open_phase(r2.experiment_id)
    assert new["config_version"] == new_cfg and new["what_changed"]["config_version"]["to"] == new_cfg
    # T-54: a decision recorded now must carry phase 2's versions (schema refuses the old ones)
    primary = next(p for p in dbx.portfolios(r2.experiment_id) if p["kind"] == "llm_primary")
    with pytest.raises(psycopg.Error, match="VERSION_DRIFT|PHASE_CLOSED"):
        with dbx.transaction():
            dbx.conn.execute(
                """insert into decisions (experiment_id, experiment_phase_id, portfolio_id, origin, kind, status, decision, no_action_reason,
                   prompt_version, exclusion_list_version, config_version, scanner_version, qb_rules_version)
                   values (%s, %s, %s, 'system', 'entry', 'no_action', 'NO_ACTION', 'test', %s, %s, %s, %s, %s)""",
                (
                    r2.experiment_id,
                    r1.phase_id,
                    primary["id"],
                    new["prompt_version"],
                    new["exclusion_list_version"],
                    new["config_version"],
                    new["scanner_version"],
                    new["qb_rules_version"],
                ),
            )


def test_code_change_opens_phase_only_with_reason(dbx, workdir):
    name = f"exp-{uuid.uuid4().hex[:6]}"
    do_boot(dbx, workdir, name, code_version="sha-1")
    with pytest.raises(BootHalt):
        do_boot(dbx, workdir, name, code_version="sha-2")
    (workdir / "config" / "phase_change.yaml").write_text(
        yaml.safe_dump(
            {"schema_version": 2, "sequence": 2, "category": "correctness", "reason": "bug fix", "for_versions": {}}
        )
    )
    r = do_boot(dbx, workdir, name, code_version="sha-2")
    assert r.opened_new_phase and dbx.open_phase(r.experiment_id)["category"] == "correctness"
    with pytest.raises(BootHalt, match="PHASE_REASON_MISSING"):  # the same record cannot justify a third phase
        do_boot(dbx, workdir, name, code_version="sha-3")


def test_unknown_broker_position_halts(dbx, workdir):
    name = f"exp-{uuid.uuid4().hex[:6]}"
    broker = FlatBroker(
        [Position(portfolio_id=uuid.UUID(int=0), symbol="TSLA", entry_decision_id=uuid.UUID(int=0), qty=Decimal("3"))]
    )
    with pytest.raises(BootHalt) as exc:
        boot(
            settings_from(workdir),
            {**LOCAL_ENV, "EXPERIMENT_ID": name},
            dbx,
            broker,
            code_version="sha-1",
            today=date(2026, 9, 14),
        )  # type: ignore[arg-type]
    assert exc.value.code == "RECONCILE_HALT" and exc.value.detail["unexplained"][0]["symbol"] == "TSLA"
    rec = dbx.conn.execute("select result from reconciliations order by ran_at desc limit 1").fetchone()
    assert rec["result"] == "halt"


def test_exposure_checks_required_without_local_flag(dbx, workdir):
    with pytest.raises(BootHalt) as exc:
        boot(settings_from(workdir), {"EXPERIMENT_ID": "x"}, dbx, FlatBroker(), code_version="sha-1")  # type: ignore[arg-type]
    assert exc.value.code == "SUPABASE_EXPOSURE_HALT"


def test_live_refused_by_factory_and_loader(workdir, monkeypatch):
    settings = settings_from(workdir)
    monkeypatch.setattr(settings.risk.execution, "execution_mode", ExecutionMode.LIVE)
    with pytest.raises(LiveLockedOutError):
        make_broker(settings, {})
    assert make_broker(settings_from(workdir), {}).observe_only is True  # DRY_RUN → NullBroker


def test_reused_version_with_different_content_refused(dbx, workdir):
    name = f"exp-{uuid.uuid4().hex[:6]}"
    do_boot(dbx, workdir, name)
    sc = workdir / "config" / "scanner.yaml"
    d = yaml.safe_load(sc.read_text())
    d["composite_score"]["weights"]["momentum_20d"] = 0.25  # content changes, version string does not
    sc.write_text(yaml.safe_dump(d))
    with pytest.raises(RuntimeError, match="VERSION_REUSED"):
        do_boot(dbx, workdir, name)


def test_boot_report_notes_dry_run(dbx, workdir):
    r = do_boot(dbx, workdir, f"exp-{uuid.uuid4().hex[:6]}")
    assert any("DRY_RUN" in n for n in r.notes)
    assert bootmod.latest_migration_version() == "20260913000011"
