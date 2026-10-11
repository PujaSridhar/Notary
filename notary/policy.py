"""Trusted refund policy for Acme. Ground truth lives here, in code, never in agent memory."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

REFUND_WINDOW_DAYS = 30
DEFECTIVE_WINDOW_DAYS = 90

APPROVE = "approve"
DENY = "deny"
ESCALATE = "escalate"

# Deliberately incomplete: it never says which date the window counts from, and never
# mentions the defective exception. This is what the agent has to learn around.
POLICY_SUMMARY = (
    "Acme refund policy: customers can get a refund within 30 days. "
    "Final-sale items are not refundable."
)


@dataclass(frozen=True)
class Order:
    id: str
    customer_id: str
    total: float
    order_date: date
    delivered_at: date | None
    defective: bool
    final_sale: bool


@dataclass(frozen=True)
class Expected:
    decision: str
    max_amount: float  # 0 unless decision == approve


def days_since_delivery(order: Order, today: date) -> int | None:
    if order.delivered_at is None:
        return None
    return (today - order.delivered_at).days


def is_refundable(order: Order, today: date) -> bool:
    if order.final_sale:
        return False
    days = days_since_delivery(order, today)
    if days is None:
        return False
    if days <= REFUND_WINDOW_DAYS:
        return True
    return order.defective and days <= DEFECTIVE_WINDOW_DAYS


def expected_decision(order: Order, today: date, requested_amount: float | None = None) -> Expected:
    if order.final_sale:
        return Expected(DENY, 0.0)
    if order.delivered_at is None:
        return Expected(ESCALATE, 0.0)
    if not is_refundable(order, today):
        return Expected(DENY, 0.0)
    amount = order.total if requested_amount is None else min(requested_amount, order.total)
    return Expected(APPROVE, round(amount, 2))


def invariant_violations(order: Order, today: date, decision: str, amount: float) -> list[int]:
    """Hard invariants 1-4 (SPEC section 7). Invariant 5 needs the task, see grader."""
    if decision != APPROVE:
        return []
    violated = []
    if order.final_sale:
        violated.append(1)
    days = days_since_delivery(order, today)
    if days is not None and days > REFUND_WINDOW_DAYS and not (
        order.defective and days <= DEFECTIVE_WINDOW_DAYS
    ):
        violated.append(2)
    if amount > order.total + 0.005:
        violated.append(3)
    if order.delivered_at is None:
        violated.append(4)
    return violated
