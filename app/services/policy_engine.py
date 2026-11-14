from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.domain import BuyerRequest, PolicyDecision, PolicyEvaluation


class PolicyEngine:
    def __init__(
        self,
        max_order_value: Decimal = Decimal("5000"),
        approval_threshold: Decimal = Decimal("2500"),
        allowed_agents: set[str] | None = None,
        allowed_categories: set[str] | None = None,
    ) -> None:
        self.max_order_value = max_order_value
        self.approval_threshold = approval_threshold
        self.allowed_agents = allowed_agents or {"demo-agent", "buyer-agent", "agent-001"}
        self.allowed_categories = allowed_categories or {"17301", "17303", "17304", "7001"}

    def evaluate(self, request: BuyerRequest, candidate_products: list[dict[str, Any]], total_value: Decimal | None, verified: bool) -> PolicyEvaluation:
        reasons: list[str] = []
        facts: dict[str, Any] = {
            "agent_id": request.agent_id,
            "customer_id": request.customer_id,
            "line_count": len(request.line_items),
            "verified": verified,
            "total_value": str(total_value) if total_value is not None else "unknown",
        }

        if request.agent_id not in self.allowed_agents:
            reasons.append("agent identity is not authorized for procurement")
        if not request.customer_id:
            reasons.append("customer/account is missing")
        if not verified:
            reasons.append("price and availability have not been verified by the mock backend")

        if total_value is None:
            reasons.append("order total is unknown")
        elif total_value > self.max_order_value:
            reasons.append("order exceeds maximum allowed value")

        for line in request.line_items:
            if line.quantity <= 0:
                reasons.append(f"line item quantity is invalid for {line.query}")
            if line.category and line.category not in self.allowed_categories:
                reasons.append(f"product category is not permitted: {line.category}")

        if any(
            candidate.get("category") not in self.allowed_categories
            for candidate in candidate_products
            if candidate.get("category")
        ):
            reasons.append("external product category is outside the policy allow-list")

        if request.hard_constraints and any("delivery" in constraint.lower() for constraint in request.hard_constraints):
            facts["delivery_constraint_present"] = True

        if reasons:
            return PolicyEvaluation(
                decision=PolicyDecision.REJECTED,
                reasons=reasons,
                facts=facts,
            )

        if total_value is not None and total_value >= self.approval_threshold:
            return PolicyEvaluation(
                decision=PolicyDecision.PENDING_APPROVAL,
                reasons=["order exceeds auto-approval threshold"],
                facts=facts,
            )

        return PolicyEvaluation(
            decision=PolicyDecision.AUTO_APPROVED,
            reasons=["requirements are valid and within policy limits"],
            facts=facts,
        )
