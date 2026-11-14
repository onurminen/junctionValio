from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class PolicyDecision(str, Enum):
    AUTO_APPROVED = "AUTO_APPROVED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    REJECTED = "REJECTED"


class ApprovalStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


def normalize_hard_constraints(values: list[str]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        constraint = value.strip()
        if not constraint or len(constraint) > 500:
            raise ValueError("hard constraints must contain 1 to 500 characters")
        normalized.append(constraint)
    return normalized


class RequestLine(BaseModel):
    query: str = Field(..., min_length=1)
    quantity: Decimal = Field(..., gt=0)
    unit: str | None = None
    category: str | None = None
    excluded_allergen_ids: list[str] = Field(default_factory=list, max_length=32)
    selected_product_id: str | None = None

    @field_validator("unit")
    @classmethod
    def normalize_unit(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip().upper()

    @field_validator("category")
    @classmethod
    def normalize_category(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("excluded_allergen_ids")
    @classmethod
    def normalize_excluded_allergen_ids(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            code = value.strip().upper()
            if not code:
                raise ValueError("excluded allergen IDs must not be blank")
            if code not in normalized:
                normalized.append(code)
        return normalized


class BuyerRequest(BaseModel):
    agent_id: str = Field(..., min_length=1)
    customer_id: str = Field(..., min_length=1)
    requested_date: date
    line_items: list[RequestLine] = Field(..., min_length=1)
    buyer_text: str | None = None
    hard_constraints: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("hard_constraints")
    @classmethod
    def normalize_hard_constraints(cls, values: list[str]) -> list[str]:
        return normalize_hard_constraints(values)


class CandidateProduct(BaseModel):
    product_id: str
    name: str
    brand: str | None = None
    category: str | None = None
    sales_unit: str | None = None
    base_unit: str | None = None
    allowed_lot_size: Decimal | None = None
    country_of_origin: str | None = None
    allergen_classifications: dict[str, list[str]] = Field(default_factory=dict)
    relevance_score: int = 0


class VerifiedOffer(BaseModel):
    product_id: str
    unit_price: Decimal | None = None
    comparison_price: Decimal | None = None
    currency: str | None = None
    available_amount: Decimal | None = None
    available_date: str | None = None
    verified: bool = False


class PurchasePlan(BaseModel):
    plan_id: str
    agent_id: str
    customer_id: str
    delivery_date: date
    lines: list[dict[str, Any]] = Field(default_factory=list)
    total_value: Decimal | None = None
    currency: str | None = None
    verified: bool = False
    policy_decision: PolicyDecision | None = None
    policy_version: str = "v1"
    policy_reasons: list[str] = Field(default_factory=list)
    policy_facts: dict[str, Any] = Field(default_factory=dict)


class PolicyEvaluation(BaseModel):
    decision: PolicyDecision
    policy_version: str = "v1"
    reasons: list[str] = Field(default_factory=list)
    facts: dict[str, Any] = Field(default_factory=dict)


class ApprovalRecord(BaseModel):
    approval_id: str
    plan_id: str
    snapshot_hash: str
    status: ApprovalStatus
    approver: str | None = None
    created_at: str
    decided_at: str | None = None
    comment: str | None = None


class ApprovalReview(BaseModel):
    approval: ApprovalRecord
    snapshot: dict[str, Any]
