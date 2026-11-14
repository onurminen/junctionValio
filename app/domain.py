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


class RequestLine(BaseModel):
    query: str = Field(..., min_length=1)
    quantity: Decimal = Field(..., gt=0)
    unit: str | None = None
    category: str | None = None
    selected_product_id: str | None = None

    @field_validator("unit")
    @classmethod
    def normalize_unit(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip().upper()


class BuyerRequest(BaseModel):
    agent_id: str = Field(..., min_length=1)
    customer_id: str = Field(..., min_length=1)
    requested_date: date
    line_items: list[RequestLine] = Field(..., min_length=1)
    buyer_text: str | None = None
    hard_constraints: list[str] = Field(default_factory=list)


class CandidateProduct(BaseModel):
    product_id: str
    name: str
    brand: str | None = None
    category: str | None = None
    sales_unit: str | None = None
    base_unit: str | None = None
    allowed_lot_size: Decimal | None = None
    country_of_origin: str | None = None
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
    comment: str | None = None
