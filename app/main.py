from __future__ import annotations

import re
import secrets
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from app.adapters.valio_client import ValioClient, ValioRateLimited
from app.config import (
    BASE_DIR,
    TRUSTED_TRADE_APPROVER_TOKENS,
    VALIO_API_KEY,
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
    line_items: list[RequestLine] = Field(..., min_length=1, max_length=100)
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


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def _api_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Europe/Helsinki"))
    return parsed


def _assess_candidate(
    line: RequestLine,
    candidate: dict[str, Any],
    price: dict[str, Any] | None,
    availability: dict[str, Any] | None,
    availability_checked_at: Any,
    requested_date: date,
) -> dict[str, Any]:
    price = price or {}
    availability = availability or {}
    catalog_price = price.get("catalogPrice")
    if not isinstance(catalog_price, dict):
        catalog_price = {}
    unit_price = _decimal_or_none(catalog_price.get("value"))
    currency = catalog_price.get("currency") or price.get("currency")
    price_ok = bool(
        price
        and not price.get("error", True)
        and unit_price is not None
        and unit_price >= 0
        and currency
    )
    line_total = unit_price * line.quantity if price_ok and unit_price is not None else None

    schedules = availability.get("scheduleLines")
    schedule = schedules[0] if isinstance(schedules, list) and len(schedules) == 1 and isinstance(schedules[0], dict) else {}
    sales_unit = candidate.get("sales_unit")
    unit_matches = bool(
        line.unit
        and sales_unit
        and line.unit.upper() == sales_unit.upper()
        and (schedule.get("salesUnit") or "").upper() == line.unit.upper()
    )
    lot_size = _decimal_or_none(candidate.get("allowed_lot_size"))
    lot_size_matches = bool(
        lot_size is not None
        and lot_size > 0
        and line.quantity % lot_size == 0
    )
    available_amount = _decimal_or_none(schedule.get("availableAmount"))
    available_date = schedule.get("availableDate")
    closing_time = availability.get("closingTime")
    availability_status = "AVAILABILITY_UNCHECKED"
    availability_unknown = False
    is_terminal = False

    if availability:
        if availability.get("salesBlock", True):
            availability_status = "SALES_BLOCKED"
        elif not schedules:
            availability_status = "UNAVAILABLE"
        elif len(schedules) != 1 or not schedule:
            availability_status = "INVALID_SCHEDULE"
        elif schedule.get("salesBlock", True):
            availability_status = "SALES_BLOCKED"
        else:
            is_terminal = (
                available_amount is not None
                and available_amount >= Decimal("999999")
            )
            if not is_terminal and available_amount is None:
                availability_status = "AVAILABILITY_UNKNOWN"
            elif not is_terminal and available_amount < line.quantity:
                availability_status = "INSUFFICIENT_STOCK"
            else:
                schedule_date: date | None = None
                if isinstance(available_date, str):
                    try:
                        schedule_date = date.fromisoformat(available_date[:10])
                    except ValueError:
                        pass
                if schedule_date is None:
                    availability_status = "DELIVERY_DATE_UNKNOWN"
                elif schedule_date > requested_date:
                    availability_status = "DELIVERY_TOO_LATE"
                else:
                    if is_terminal:
                        availability_status = "TERMINAL_AVAILABILITY_UNKNOWN"
                        availability_unknown = True
                    else:
                        availability_status = "AVAILABLE"

                    if availability_status in {"AVAILABLE", "TERMINAL_AVAILABILITY_UNKNOWN"}:
                        cutoff = _api_datetime(closing_time)
                        checked_at = _api_datetime(availability_checked_at)
                        if closing_time and cutoff is None:
                            availability_status = "CUTOFF_UNKNOWN"
                            availability_unknown = True
                        elif cutoff and checked_at is None:
                            availability_status = "CUTOFF_UNKNOWN"
                            availability_unknown = True
                        elif cutoff and checked_at and checked_at > cutoff:
                            availability_status = "CUTOFF_PASSED"

    availability_eligible = availability_status in {
        "AVAILABLE",
        "TERMINAL_AVAILABILITY_UNKNOWN",
        "CUTOFF_UNKNOWN",
    }
    eligible = price_ok and unit_matches and lot_size_matches and availability_eligible
    reasons: list[str] = []
    if not price_ok:
        reasons.append("price is missing or invalid")
    if not unit_matches:
        reasons.append("requested unit does not match the product sales unit")
    if not lot_size_matches:
        reasons.append("quantity does not match the product's allowed lot size")
    if availability_status == "TERMINAL_AVAILABILITY_UNKNOWN":
        reasons.append("supplier availability is unknown; the quantity is not capped by this snapshot")
    elif availability_status == "CUTOFF_UNKNOWN":
        reasons.append("the delivery cutoff could not be verified")
    elif availability_status != "AVAILABLE":
        reasons.append(availability_status.lower().replace("_", " "))

    return {
        "product_id": candidate["product_id"],
        "name": candidate["name"],
        "category": candidate.get("category"),
        "relevance_score": candidate.get("relevance_score", 0),
        "sales_unit": sales_unit,
        "quantity": str(line.quantity),
        "unit": line.unit,
        "unit_price": str(unit_price) if unit_price is not None else None,
        "line_total": str(line_total) if line_total is not None else None,
        "currency": currency,
        "available_amount": (
            str(available_amount)
            if available_amount is not None and not is_terminal
            else None
        ),
        "availability_quantity_is_unknown": is_terminal,
        "available_date": available_date,
        "closing_time": closing_time,
        "availability_checked_at": availability_checked_at,
        "availability_status": availability_status,
        "price_verified": price_ok,
        "availability_verified": availability_status == "AVAILABLE",
        "lot_size": str(lot_size) if lot_size is not None else None,
        "eligible": eligible,
        "review_reasons": reasons,
    }


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

    candidate_lists: list[list[dict[str, Any]]] = []
    for line in request.line_items:
        matches = catalog_service.search(line.query, limit=5)
        candidates = [candidate.model_dump(mode="json") for candidate in matches]
        if line.selected_product_id:
            selected = next(
                (item for item in candidates if item["product_id"] == line.selected_product_id),
                None,
            )
            if selected is None:
                raise HTTPException(
                    status_code=422,
                    detail="selected_product_id must be one of the catalog matches for this query",
                )
            candidates = [selected]
        candidate_lists.append(candidates)

    product_ids = list(dict.fromkeys(
        candidate["product_id"]
        for candidates in candidate_lists
        for candidate in candidates
    ))
    price_by_id: dict[str, dict[str, Any]] = {}
    availability_by_id: dict[str, dict[str, Any]] = {}

    if product_ids and VALIO_API_KEY:
        client = ValioClient()
        try:
            price_response = client.fetch_price(request.customer_id, request.requested_date, product_ids)
            availability_response = client.fetch_availability(request.customer_id, request.requested_date, product_ids)
        except ValioRateLimited as exc:
            audit_service.record(
                actor=request.agent_id,
                action="price_availability_checked",
                outcome="rate_limited",
                entity_type="customer",
                entity_id=request.customer_id,
                metadata={"retry_after_seconds": exc.retry_after},
            )
            raise HTTPException(
                status_code=429,
                detail="Valio API rate limit reached; wait before retrying",
                headers={"Retry-After": str(exc.retry_after)},
            ) from exc
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
        audit_service.record(
            actor=request.agent_id,
            action="price_availability_checked",
            outcome="success",
            entity_type="customer",
            entity_id=request.customer_id,
            metadata={
                "candidate_count": len(product_ids),
                "price_date": request.requested_date.isoformat(),
            },
        )

    lines: list[dict[str, Any]] = []
    candidate_products: list[dict[str, Any]] = []
    total_value = Decimal("0")
    currencies: set[str] = set()
    complete = True
    verified = True
    availability_unknown = False
    for line, candidates in zip(request.line_items, candidate_lists, strict=True):
        assessments = [
            _assess_candidate(
                line=line,
                candidate=candidate,
                price=price_by_id.get(candidate["product_id"]),
                availability=availability_by_id.get(candidate["product_id"]),
                availability_checked_at=availability_response.get("timeNow") if availability_by_id else None,
                requested_date=request.requested_date,
            )
            for candidate in candidates
        ]
        selected = next((item for item in assessments if item["eligible"]), None)
        if selected is None:
            complete = False
            verified = False
            lines.append({
                "query": line.query,
                "quantity": str(line.quantity),
                "unit": line.unit,
                "selected_product_id": line.selected_product_id,
                "status": "NO_ELIGIBLE_CANDIDATE",
                "alternatives": assessments,
            })
            continue

        chosen_candidate = next(
            candidate
            for candidate in candidates
            if candidate["product_id"] == selected["product_id"]
        )
        candidate_products.append(chosen_candidate)
        total_value += Decimal(selected["line_total"])
        if selected["currency"]:
            currencies.add(selected["currency"])
        line_unknown = selected["availability_status"] != "AVAILABLE"
        availability_unknown = availability_unknown or line_unknown
        verified = verified and not line_unknown
        lines.append({
            "query": line.query,
            "product_id": selected["product_id"],
            "name": selected["name"],
            "quantity": str(line.quantity),
            "unit": line.unit,
            "sales_unit": selected["sales_unit"],
            "unit_price": selected["unit_price"],
            "line_total": selected["line_total"],
            "currency": selected["currency"],
            "available_amount": selected["available_amount"],
            "availability_quantity_is_unknown": selected["availability_quantity_is_unknown"],
            "available_date": selected["available_date"],
            "closing_time": selected["closing_time"],
            "availability_checked_at": selected["availability_checked_at"],
            "availability_status": selected["availability_status"],
            "status": "PENDING_HUMAN_APPROVAL",
            "review_reasons": selected["review_reasons"],
            "alternatives": assessments,
        })

    if not complete or len(currencies) != 1:
        verified = False
        total_value_result = None
    else:
        total_value_result = total_value
    currency = next(iter(currencies)) if len(currencies) == 1 else None

    policy = policy_engine.evaluate(
        request,
        candidate_products,
        total_value_result,
        verified,
        currency,
        availability_unknown=availability_unknown,
        complete=complete,
    )
    plan = PurchasePlan(
        plan_id=f"plan-{uuid4().hex}",
        agent_id=request.agent_id,
        customer_id=request.customer_id,
        delivery_date=request.requested_date,
        lines=lines,
        total_value=total_value_result,
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
            "total_value": str(total_value_result) if total_value_result is not None else None,
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
