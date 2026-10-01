"""End-to-end example: an order-fulfilment job wired against the in-memory store.

Run it directly::

    python -m etf.examples.order_job

It demonstrates the happy path, a human-in-the-loop pause/approve, and a failure +
restart-with-config-change — all against :class:`~etf.stores.memory.InMemoryStateStore`,
so no MongoDB is required. Swap in ``BeanieStateStore`` for production.
"""

from __future__ import annotations

import asyncio

from etf import (
    Actor,
    BatchStatus,
    EtfConfig,
    JobDefinition,
    JobLauncher,
    JobOperator,
    JobParameters,
    JobRegistry,
    LaunchRequest,
    Step,
    StepContext,
    StepResult,
    ValidationDecision,
)
from etf.audit import StoreBackedAuditSink
from etf.locking import InMemoryLockProvider
from etf.stores.memory import InMemoryStateStore


class ReserveInventory(Step):
    name = "reserve_inventory"

    async def execute(self, ctx: StepContext) -> StepResult:
        order_id = ctx.params.get("order_id")
        amount = float(str(ctx.params.get("amount", 0)))
        # Idempotent: only reserve once even if the step re-runs after a crash/resume.
        if not ctx.get("reserved"):
            ctx.put("reserved", True)
            await ctx.checkpoint()  # durable resume point

        # High-value orders need a human to approve before we charge.
        if amount >= 1000:
            outcome = await ctx.request_validation(
                reason="High-value order requires approval",
                payload={"order_id": order_id, "amount": amount},
                required_role="ops-approver",
            )
            # Only reached after a human decision arrives and the run resumes.
            if not outcome.approved:
                raise RuntimeError("order rejected by approver")
        return StepResult.completed(write_count=1)


class ChargePayment(Step):
    name = "charge_payment"

    async def execute(self, ctx: StepContext) -> StepResult:
        # Demonstrate restart-with-config-change: fail unless a gateway is configured.
        gateway = ctx.params.get("payment_gateway")
        if not gateway:
            raise RuntimeError("no payment_gateway configured")
        return StepResult.completed(write_count=1)


class Ship(Step):
    name = "ship"

    async def execute(self, ctx: StepContext) -> StepResult:
        return StepResult.completed(write_count=1)


def build() -> tuple[JobLauncher, JobOperator, InMemoryStateStore]:
    store = InMemoryStateStore()
    registry = JobRegistry()
    registry.register(
        JobDefinition(name="fulfill_order", steps=[ReserveInventory(), ChargePayment(), Ship()])
    )
    cfg = EtfConfig(
        store=store,
        audit=StoreBackedAuditSink(store),
        lock_provider=InMemoryLockProvider(),
        registry=registry,
    )
    return JobLauncher(cfg), JobOperator(cfg), store


async def main() -> None:
    launcher, operator, store = build()
    await store.initialize()

    # 1) Launch a high-value order that will pause for approval and fail at payment.
    run = await launcher.launch(
        LaunchRequest(
            job_name="fulfill_order",
            parameters=JobParameters(
                identifying={"order_id": "A-1001"},
                non_identifying={"amount": 2500},  # no payment_gateway yet -> will fail
            ),
            idempotency_key="demo-1",
            requested_by=Actor.service("checkout"),
        )
    )
    print(f"launched run {run.id[:8]} -> status={run.status.value}")  # AWAITING_VALIDATION

    # 2) A human approves the high-value order.
    status = await operator.get_status(run.id)
    assert status.open_validation is not None
    run = await operator.submit_validation_decision(
        status.open_validation.id,
        ValidationDecision.approve(
            Actor.human("u-42", "Dana", roles={"ops-approver"})
        ),
    )
    print(f"after approval -> status={run.status.value}")  # FAILED (no gateway)

    # 3) Restart with a config change: supply the payment gateway.
    run = await operator.restart(run.instance_id, parameter_overrides={"payment_gateway": "stripe"})
    print(f"after restart   -> status={run.status.value}")  # COMPLETED
    assert run.status is BatchStatus.COMPLETED

    # 4) Inspect the audit trail.
    trail = await operator.get_audit_trail(run.id)
    print(f"\naudit trail for final run ({len(trail)} events):")
    for e in trail:
        print(f"  #{e.sequence:<2} {e.event_type.value:<22} step={e.step_name or '-'}")


if __name__ == "__main__":
    asyncio.run(main())
