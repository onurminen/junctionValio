from __future__ import annotations

import re
import secrets
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from app.adapters.valio_client import ValioClient
from app.config import (
    BASE_DIR,
    TRUSTED_TRADE_APPROVER_TOKENS,
    VALIO_API_KEY,
    VALIO_BASE_URL,
)
from app.database import database
from app.domain import BuyerRequest, PurchasePlan, RequestLine
from app.services.approval_service import ApprovalService
from app.services.audit_service import AuditService
from app.services.catalog_service import CatalogService
from app.services.policy_engine import PolicyEngine

app = FastAPI(title="Trusted Trade", version="0.1.0")

catalog_service = CatalogService()
policy_engine = PolicyEngine()
approval_service = ApprovalService()
audit_service = AuditService()


@app.middleware("http")
async def add_correlation_id(
    request: Request,
    call_next: RequestResponseEndpoint,
) -> Response:
    supplied_id = request.headers.get("X-Correlation-ID", "")
    correlation_id = (
        supplied_id
        if supplied_id
        and len(supplied_id) <= 80
        and all(char.isalnum() or char in "._:-" for char in supplied_id)
        else str(uuid4())
    )
    with audit_service.correlation(correlation_id):
        response = await call_next(request)
    response.headers["X-Correlation-ID"] = correlation_id
    return response


class RequestInterpretationRequest(BaseModel):
    text: str
    customer_id: str = "customer-001"
    agent_id: str = "demo-agent"
    requested_date: date | None = None


class PurchasePlanRequest(BaseModel):
    agent_id: str = "demo-agent"
    customer_id: str = "customer-001"
    requested_date: date
    line_items: list[RequestLine] = Field(..., min_length=1)
    hard_constraints: list[str] = Field(default_factory=list)


class ApprovalDecisionRequest(BaseModel):
    approval_id: str
    approver: str
    approved: bool


class ApprovalRequest(BaseModel):
    plan_id: str


def _parse_quantity(value: str) -> Decimal:
    match = re.search(r"(\d+(?:[.,]\d+)?)", value)
    if not match:
        return Decimal("1")
    return Decimal(match.group(1).replace(",", "."))


def _approver_tokens() -> dict[str, str]:
    tokens: dict[str, str] = {}
    for entry in TRUSTED_TRADE_APPROVER_TOKENS.split(","):
        approver, separator, token = entry.partition(":")
        if separator and approver.strip() and token.strip():
            tokens[approver.strip()] = token.strip()
    return tokens


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


@app.get("/", include_in_schema=False)
def demo_ui() -> FileResponse:
    return FileResponse(Path(BASE_DIR, "app", "static", "index.html"))


@app.post("/api/requests/parse")
def parse_request(payload: RequestInterpretationRequest) -> dict[str, Any]:
    request = _infer_request(payload.text, payload.customer_id, payload.agent_id, payload.requested_date)
    choices = catalog_service.search(request.line_items[0].query, limit=3)
    audit_service.record(
        actor=request.agent_id,
        action="request_interpreted",
        outcome="success" if choices else "no_candidates",
        entity_type="customer",
        entity_id=request.customer_id,
        metadata={"line_count": len(request.line_items), "candidate_count": len(choices)},
    )
    return {
        "request": request.model_dump(mode="json"),
        "candidate_products": [product.model_dump(mode="json") for product in choices],
    }


@app.get("/api/catalog/search")
def catalog_search(query: str, limit: int = 5) -> dict[str, Any]:
    products = catalog_service.search(query, limit=limit)
    audit_service.record(
        actor="demo-user",
        action="catalog_searched",
        outcome="success" if products else "no_results",
        metadata={"result_count": len(products)},
    )
    return {"query": query, "results": [product.model_dump(mode="json") for product in products]}


@app.post("/api/purchase-plan/build")
def build_purchase_plan(payload: PurchasePlanRequest) -> dict[str, Any]:
    request = BuyerRequest(
        agent_id=payload.agent_id,
        customer_id=payload.customer_id,
        requested_date=payload.requested_date,
        line_items=payload.line_items,
        buyer_text="Generated purchase plan",
        hard_constraints=payload.hard_constraints,
    )

    chosen_products: list[dict[str, Any] | None] = []
    for line in request.line_items:
        matches = catalog_service.search(line.query, limit=3)
        chosen_products.append(
            matches[0].model_dump(mode="json") if matches else None
        )

    product_ids = list(dict.fromkeys(
        candidate["product_id"] for candidate in chosen_products if candidate
    ))
    price_by_id: dict[str, dict[str, Any]] = {}
    availability_by_id: dict[str, dict[str, Any]] = {}
    total_value: Decimal | None = None
    currency: str | None = None
    verified = False
    lines: list[dict[str, Any]] = []

    if product_ids and VALIO_API_KEY:
        client = ValioClient()
        try:
            price_response = client.fetch_price(request.customer_id, request.requested_date, product_ids)
            availability_response = client.fetch_availability(request.customer_id, request.requested_date, product_ids)
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(
                status_code=502,
                detail="Valio price/availability request failed or returned invalid JSON",
            ) from exc

        if not isinstance(price_response.get("products"), list) or not isinstance(availability_response.get("products"), list):
            raise HTTPException(status_code=502, detail="Valio returned an invalid price/availability response")
        price_by_id = {
            entry["productId"]: entry
            for entry in price_response["products"]
            if isinstance(entry, dict) and isinstance(entry.get("productId"), str)
        }
        availability_by_id = {
            entry["productId"]: entry
            for entry in availability_response["products"]
            if isinstance(entry, dict) and isinstance(entry.get("productId"), str)
        }

    total_value = (
        Decimal("0")
        if product_ids and all(candidate is not None for candidate in chosen_products)
        else None
    )
    verified = total_value is not None
    currencies: set[str] = set()
    candidate_products: list[dict[str, Any]] = []
    for line, candidate in zip(request.line_items, chosen_products, strict=True):
        if candidate is None:
            verified = False
            total_value = None
            lines.append({
                "query": line.query,
                "quantity": str(line.quantity),
                "unit": line.unit,
                "status": "NO_CANDIDATE",
            })
            continue

        candidate_products.append(candidate)
        product_id = candidate["product_id"]
        price = price_by_id.get(product_id, {})
        catalog_price = price.get("catalogPrice")
        availability = availability_by_id.get(product_id, {})
        schedules = availability.get("scheduleLines", [])
        schedule = schedules[0] if len(schedules) == 1 and isinstance(schedules[0], dict) else {}
        product_currency = (
            (catalog_price or {}).get("currency")
            or price.get("currency")
        )
        unit_price = Decimal(str(catalog_price["value"])) if catalog_price and catalog_price.get("value") is not None else None
        line_total = unit_price * line.quantity if unit_price is not None else None
        if product_currency:
            currencies.add(product_currency)
        if total_value is not None and line_total is not None:
            total_value += line_total
        else:
            total_value = None

        unit_matches = bool(
            line.unit
            and candidate.get("sales_unit")
            and line.unit.upper() == candidate["sales_unit"].upper()
            and schedule.get("salesUnit", "").upper() == line.unit.upper()
        )
        lot_size = Decimal(str(candidate["allowed_lot_size"])) if candidate.get("allowed_lot_size") is not None else None
        lot_matches = lot_size is None or line.quantity % lot_size == 0
        available_amount = Decimal(str(schedule["availableAmount"])) if schedule.get("availableAmount") is not None else None
        available_date = schedule.get("availableDate")
        date_matches = False
        if isinstance(available_date, str):
            try:
                date_matches = date.fromisoformat(available_date[:10]) <= request.requested_date
            except ValueError:
                date_matches = False
        is_terminal = (availability.get("logisticModel") or "").casefold() == "terminal"
        stock_ok = bool(
            not is_terminal
            and availability
            and not availability.get("salesBlock", True)
            and not schedule.get("salesBlock", True)
            and available_amount is not None
            and available_amount >= line.quantity
            and date_matches
        )
        price_ok = bool(
            price
            and not price.get("error", True)
            and catalog_price
            and product_currency
        )
        line_verified = price_ok and stock_ok and unit_matches and lot_matches
        verified = verified and line_verified
        lines.append({
            "query": line.query,
            "product_id": product_id,
            "name": candidate["name"],
            "quantity": str(line.quantity),
            "unit": line.unit,
            "sales_unit": candidate.get("sales_unit"),
            "unit_price": str(unit_price) if unit_price is not None else None,
            "line_total": str(line_total) if line_total is not None else None,
            "currency": product_currency,
            "available_amount": str(available_amount) if available_amount is not None else None,
            "available_date": available_date,
            "availability_checked_at": availability_response.get("timeNow") if availability_by_id else None,
            "status": "VERIFIED" if line_verified else "UNVERIFIED",
            "checks": {
                "price": price_ok,
                "availability": stock_ok,
                "unit_match": unit_matches,
                "lot_size_match": lot_matches,
            },
        })

    if len(currencies) != 1:
        verified = False
        if len(currencies) > 1:
            total_value = None
    else:
        currency = next(iter(currencies))

    policy = policy_engine.evaluate(request, candidate_products, total_value, verified, currency)
    plan = PurchasePlan(
        plan_id=f"plan-{uuid4().hex}",
        agent_id=request.agent_id,
        customer_id=request.customer_id,
        delivery_date=request.requested_date,
        lines=lines,
        total_value=total_value,
        currency=currency,
        verified=verified,
        policy_decision=policy.decision,
    )
    plan_data = plan.model_dump(mode="json")
    database.save_plan(
        plan_id=plan.plan_id,
        snapshot=plan_data,
        decision=policy.decision.value,
        agent_id=request.agent_id,
        customer_id=request.customer_id,
    )
    audit_service.record(
        actor=request.agent_id,
        action="purchase_plan_evaluated",
        outcome=policy.decision.value,
        entity_type="purchase_plan",
        entity_id=plan.plan_id,
        metadata={
            "verified": verified,
            "total_value": str(total_value) if total_value is not None else None,
            "currency": currency,
            "line_count": len(lines),
            "reasons": policy.reasons,
        },
    )
    return {
        "plan": plan_data,
        "policy": policy.model_dump(mode="json"),
    }


@app.post("/api/approvals/request")
def request_approval(payload: ApprovalRequest) -> dict[str, Any]:
    try:
        approval = approval_service.create_request(payload.plan_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    audit_service.record(
        actor="system",
        action="approval_requested",
        outcome="pending",
        entity_type="approval",
        entity_id=approval.approval_id,
        metadata={"plan_id": approval.plan_id, "snapshot_hash": approval.snapshot_hash},
    )
    return {"approval": approval.model_dump(mode="json")}


@app.post("/api/approvals/decide")
def decide_approval(
    payload: ApprovalDecisionRequest,
    approver_token: str = Header(default="", alias="X-Approver-Token"),
) -> dict[str, Any]:
    expected_token = _approver_tokens().get(payload.approver)
    if expected_token is None:
        raise HTTPException(status_code=401, detail="Approver identity is not configured")
    if not secrets.compare_digest(approver_token, expected_token):
        raise HTTPException(status_code=401, detail="Invalid approver credentials")
    try:
        approval = approval_service.decide(payload.approval_id, payload.approver, payload.approved)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    audit_service.record(
        actor=payload.approver,
        action="approval_decided",
        outcome=approval.status.value,
        entity_type="approval",
        entity_id=approval.approval_id,
        metadata={"plan_id": approval.plan_id},
    )
    return {"approval": approval.model_dump(mode="json")}


@app.get("/api/approvals")
def list_approvals(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    return {
        "approvals": [
            record.model_dump(mode="json")
            for record in approval_service.list_records(limit)
        ]
    }


@app.get("/api/audit")
def list_audit_events(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    return {"events": audit_service.list_events(limit)}
