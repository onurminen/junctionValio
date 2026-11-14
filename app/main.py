from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field

from app.adapters.valio_client import ValioClient
from app.config import VALIO_API_KEY, VALIO_BASE_URL
from app.domain import BuyerRequest, PolicyDecision, PurchasePlan, RequestLine
from app.services.approval_service import ApprovalService
from app.services.catalog_service import CatalogService
from app.services.policy_engine import PolicyEngine

app = FastAPI(title="Trusted Trade", version="0.1.0")

catalog_service = CatalogService()
policy_engine = PolicyEngine()
approval_service = ApprovalService()


class RequestInterpretationRequest(BaseModel):
    text: str
    customer_id: str = "customer-001"
    agent_id: str = "demo-agent"
    requested_date: date | None = None


class PurchasePlanRequest(BaseModel):
    agent_id: str = "demo-agent"
    customer_id: str = "customer-001"
    requested_date: date
    line_items: list[dict[str, Any]] = Field(default_factory=list)
    hard_constraints: list[str] = Field(default_factory=list)


class ApprovalDecisionRequest(BaseModel):
    approval_id: str
    approver: str
    approved: bool
    snapshot: dict[str, Any]


def _parse_quantity(value: str) -> Decimal:
    match = re.search(r"(\d+(?:[.,]\d+)?)", value)
    if not match:
        return Decimal("1")
    return Decimal(match.group(1).replace(",", "."))


def _infer_request(text: str, customer_id: str, agent_id: str, requested_date: date | None) -> BuyerRequest:
    normalized = text.lower()
    keywords = ["chocolate", "milk", "cheese", "yogurt", "fruit", "butter", "juice"]
    query = next((word for word in keywords if word in normalized), "chocolate")
    quantity = _parse_quantity(text)
    if requested_date is None:
        requested_date = date.today()

    return BuyerRequest(
        agent_id=agent_id,
        customer_id=customer_id,
        requested_date=requested_date,
        line_items=[RequestLine(query=query, quantity=quantity, unit="pcs")],
        buyer_text=text,
        hard_constraints=["delivery on requested date"] if "delivery" in normalized else [],
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/requests/parse")
def parse_request(payload: RequestInterpretationRequest) -> dict[str, Any]:
    request = _infer_request(payload.text, payload.customer_id, payload.agent_id, payload.requested_date)
    choices = catalog_service.search(request.line_items[0].query, limit=3)
    return {
        "request": request.model_dump(mode="json"),
        "candidate_products": [product.model_dump(mode="json") for product in choices],
    }


@app.get("/api/catalog/search")
def catalog_search(query: str, limit: int = 5) -> dict[str, Any]:
    products = catalog_service.search(query, limit=limit)
    return {"query": query, "results": [product.model_dump(mode="json") for product in products]}


@app.post("/api/purchase-plan/build")
def build_purchase_plan(payload: PurchasePlanRequest) -> dict[str, Any]:
    request = BuyerRequest(
        agent_id=payload.agent_id,
        customer_id=payload.customer_id,
        requested_date=payload.requested_date,
        line_items=[RequestLine(query=item["query"], quantity=Decimal(str(item["quantity"])), unit=item.get("unit"), category=item.get("category")) for item in payload.line_items] or [RequestLine(query="chocolate", quantity=Decimal("10"), unit="pcs")],
        buyer_text="Generated purchase plan",
        hard_constraints=payload.hard_constraints,
    )

    candidate_products: list[dict[str, Any]] = []
    for line in request.line_items:
        matches = catalog_service.search(line.query, limit=3)
        if matches:
            candidate_products.append({
                "product_id": matches[0].product_id,
                "name": matches[0].name,
                "category": matches[0].category,
                "sales_unit": matches[0].sales_unit,
                "relevance_score": matches[0].relevance_score,
            })

    total_value = Decimal("0")
    verified = False
    if VALIO_BASE_URL and VALIO_API_KEY:
        try:
            client = ValioClient()
            product_ids = [item["product_id"] for item in candidate_products if "product_id" in item]
            if product_ids:
                price_response = client.fetch_price(request.customer_id, request.requested_date, product_ids)
                availability_response = client.fetch_availability(request.customer_id, request.requested_date, product_ids)
                verified = True
                for product in price_response.get("products", []):
                    product_id = product.get("productId")
                    quantity = product.get("quantity", 1)
                    if product.get("catalogPrice"):
                        value = Decimal(str(product["catalogPrice"]["value"])) * Decimal(str(quantity))
                        total_value += value
        except Exception:
            verified = False
            total_value = None

    policy = policy_engine.evaluate(request, candidate_products, total_value, verified)
    plan = PurchasePlan(
        plan_id=f"plan-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
        customer_id=request.customer_id,
        delivery_date=request.requested_date,
        lines=[{"query": line.query, "quantity": str(line.quantity), "unit": line.unit, "candidate": candidate} for line, candidate in zip(request.line_items, candidate_products, strict=False)],
        total_value=total_value,
        currency="EUR" if total_value is not None else None,
        verified=verified,
        policy_decision=policy.decision,
    )
    return {
        "plan": plan.model_dump(mode="json"),
        "policy": policy.model_dump(mode="json"),
    }


@app.post("/api/approvals/request")
def request_approval(plan: dict[str, Any]) -> dict[str, Any]:
    approval = approval_service.create_request(plan["plan_id"], plan)
    return {"approval": approval.model_dump(mode="json")}


@app.post("/api/approvals/decide")
def decide_approval(payload: ApprovalDecisionRequest) -> dict[str, Any]:
    approval = approval_service.decide(payload.approval_id, payload.approver, payload.approved, payload.snapshot)
    return {"approval": approval.model_dump(mode="json")}


@app.post("/api/orders/submit")
def submit_order(plan: dict[str, Any], approved: bool = False) -> dict[str, Any]:
    if not approved:
        return {"status": "PENDING_APPROVAL", "supplier_order_id": None, "message": "Order is not approved and cannot be submitted"}
    if not (VALIO_BASE_URL and VALIO_API_KEY):
        return {
            "status": "SUBMISSION_UNKNOWN",
            "supplier_order_id": None,
            "message": "The Valio mock backend is not configured in this environment; no order was sent.",
        }
    client = ValioClient()
    try:
        response = client.submit_order({"order": plan})
        return {"status": "CONFIRMED", "supplier_order_id": response.get("orderId"), "raw": response}
    except Exception as exc:  # pragma: no cover - network failure path expected in integration
        return {"status": "FAILED", "supplier_order_id": None, "message": str(exc)}
