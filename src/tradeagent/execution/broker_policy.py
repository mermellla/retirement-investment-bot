"""§8.10 broker policy: read → (write → re-read) → halt. Runs at PAPER initialization and on every boot (A-01)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from tradeagent.domain.enums import BrokerPolicyResult, ExecutionMode, HaltScope
from tradeagent.domain.models import AccountConfiguration, BrokerPolicy
from tradeagent.interfaces import Broker
from tradeagent.persistence.db import Database


class BrokerPolicyHalt(RuntimeError):
    def __init__(self, detail: dict[str, Any]):
        super().__init__(f"BROKER_POLICY_HALT: {detail}")
        self.detail = detail
        self.scope = HaltScope.ALL


async def enforce_broker_policy(
    db: Database,
    broker: Broker,
    policy: BrokerPolicy,
    enforce_writes: bool,
    experiment_id: UUID | None,
    mode: ExecutionMode,
) -> tuple[BrokerPolicyResult, AccountConfiguration | None]:
    expected = policy.as_patch()
    try:
        before = await broker.account_configuration()
    except Exception as exc:
        db.record_broker_policy_check(experiment_id, mode, expected, None, None, BrokerPolicyResult.HALT)
        raise BrokerPolicyHalt({"reason": "account configuration unreadable", "error": str(exc)}) from exc
    if policy.matches(before):
        db.record_broker_policy_check(
            experiment_id, mode, expected, before.model_dump(), None, BrokerPolicyResult.MATCH
        )
        return BrokerPolicyResult.MATCH, before
    if not enforce_writes:
        db.record_broker_policy_check(experiment_id, mode, expected, before.model_dump(), None, BrokerPolicyResult.HALT)
        raise BrokerPolicyHalt(
            {"reason": "configuration differs and BROKER_POLICY_ENFORCE is false", "observed": before.model_dump()}
        )
    try:
        await broker.write_account_configuration(policy)
        after = await broker.account_configuration()
    except Exception as exc:
        db.record_broker_policy_check(experiment_id, mode, expected, before.model_dump(), None, BrokerPolicyResult.HALT)
        raise BrokerPolicyHalt(
            {"reason": "write or re-read failed", "error": str(exc), "observed": before.model_dump()}
        ) from exc
    if policy.matches(after):
        db.record_broker_policy_check(
            experiment_id, mode, expected, before.model_dump(), after.model_dump(), BrokerPolicyResult.APPLIED
        )
        return BrokerPolicyResult.APPLIED, after
    db.record_broker_policy_check(
        experiment_id, mode, expected, before.model_dump(), after.model_dump(), BrokerPolicyResult.HALT
    )
    raise BrokerPolicyHalt({"reason": "configuration still differs after write", "observed_after": after.model_dump()})
