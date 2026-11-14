from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.config import (
    TRUSTED_TRADE_AGENT_CUSTOMERS,
    TRUSTED_TRADE_ALLOWED_CATEGORIES,
    TRUSTED_TRADE_REQUIRE_HUMAN_APPROVAL,
    TRUSTED_TRADE_MAX_LINE_QUANTITY,
    TRUSTED_TRADE_MAX_ORDER_VALUE,
)
from app.domain import BuyerRequest, PolicyDecision, PolicyEvaluation


def _parse_agent_customers(value: str) -> dict[str, set[str]]:
    rules: dict[str, set[str]] = {}
    for entry in value.split(","):
        agent, separator, customers = entry.strip().partition(":")
        if not separator or not agent or not customers:
            continue
        rules[agent.strip()] = {
            customer.strip() for customer in customers.split("|") if customer.strip()
        }
    return rules


class PolicyEngine:
    def __init__(
        self,
        max_order_value: Decimal | None = None,
        max_line_quantity: Decimal | None = None,
        agent_customers: dict[str, set[str]] | None = None,
        allowed_categories: set[str] | None = None,
        require_human_approval: bool | None = None,
    ) -> None:
        self.max_order_value = (
            max_order_value
            if max_order_value is not None
            else Decimal(TRUSTED_TRADE_MAX_ORDER_VALUE)
            if TRUSTED_TRADE_MAX_ORDER_VALUE
            else None
        )
        self.max_line_quantity = (
            max_line_quantity
            if max_line_quantity is not None
            else Decimal(TRUSTED_TRADE_MAX_LINE_QUANTITY)
            if TRUSTED_TRADE_MAX_LINE_QUANTITY
            else None
        )
        if (self.max_order_value is not None and self.max_order_value <= 0) or (
            self.max_line_quantity is not None and self.max_line_quantity <= 0
        ):
            raise ValueError("policy order and quantity limits must be positive")
        self.agent_customers = (
            agent_customers
            if agent_customers is not None
            else _parse_agent_customers(TRUSTED_TRADE_AGENT_CUSTOMERS)
        )
        self.allowed_categories = (
            allowed_categories
            if allowed_categories is not None
            else {
                category.strip()
                for category in TRUSTED_TRADE_ALLOWED_CATEGORIES.split(",")
                if category.strip()
            }
            or None
        )
        self.require_human_approval = (
            require_human_approval
            if require_human_approval is not None
            else TRUSTED_TRADE_REQUIRE_HUMAN_APPROVAL
        )

    def evaluate(
        self,
        request: BuyerRequest,
        candidate_products: list[dict[str, Any]],
        total_value: Decimal | None,
        verified: bool,
        currency: str | None,
        availability_unknown: bool = False,
        complete: bool = True,
    ) -> PolicyEvaluation:
        reasons: list[str] = []
        review_reasons: list[str] = []
        facts: dict[str, Any] = {
            "agent_id": request.agent_id,
            "customer_id": request.customer_id,
            "line_count": len(request.line_items),
            "line_constraints": [
                {
                    "query": line.query,
                    "category": line.category,
                    "excluded_allergen_ids": line.excluded_allergen_ids,
                }
                for line in request.line_items
            ],
            "verified": verified,
            "total_value": str(total_value) if total_value is not None else "unknown",
            "currency": currency,
            "policy_categories_configured": self.allowed_categories is not None,
            "max_line_quantity": (
                str(self.max_line_quantity) if self.max_line_quantity is not None else None
            ),
            "max_order_value": (
                str(self.max_order_value) if self.max_order_value is not None else None
            ),
            "human_approval_required": self.require_human_approval,
        }

        allowed_customers = self.agent_customers.get(request.agent_id)
        if allowed_customers is None or request.customer_id not in allowed_customers:
            reasons.append("agent is not authorized for this customer/account")
        if not complete:
            reasons.append("not all requested lines have an eligible priced candidate")
        if not verified and not availability_unknown:
            reasons.append("required price or availability facts are not verified")
        if availability_unknown:
            review_reasons.append("terminal availability is unknown; human review is required")

        if total_value is None:
            reasons.append("order total is unknown")
        elif self.max_order_value is not None and total_value > self.max_order_value:
            reasons.append("order exceeds maximum allowed value")
        if currency != "EUR":
            reasons.append("currency is unknown or unsupported")

        for line in request.line_items:
            if line.quantity <= 0 or (
                self.max_line_quantity is not None
                and line.quantity > self.max_line_quantity
            ):
                reasons.append(f"line item quantity is invalid for {line.query}")
            if (
                self.allowed_categories is not None
                and line.category
                and line.category not in self.allowed_categories
            ):
                reasons.append(f"product category is not permitted: {line.category}")

        if any(
            self.allowed_categories is not None
            and (
                not candidate.get("category")
                or candidate.get("category") not in self.allowed_categories
            )
            for candidate in candidate_products
        ):
            reasons.append("external product category is outside the policy allow-list")

        if request.hard_constraints:
            facts["unverified_hard_constraint_count"] = len(request.hard_constraints)
            reasons.append(
                "buyer hard constraints must use supported structured fields so they can be verified"
            )

        if reasons:
            return PolicyEvaluation(
                decision=PolicyDecision.REJECTED,
                reasons=reasons,
                facts=facts,
            )

        if self.require_human_approval or availability_unknown:
            return PolicyEvaluation(
                decision=PolicyDecision.PENDING_APPROVAL,
                reasons=review_reasons or ["order requires human approval before commitment"],
                facts=facts,
            )

        return PolicyEvaluation(
            decision=PolicyDecision.AUTO_APPROVED,
            reasons=["requirements are valid and within policy limits"],
            facts=facts,
        )
