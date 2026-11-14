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
    APP_ENV,
    BASE_DIR,
    TRUSTED_TRADE_AGENT_TOKENS,
    TRUSTED_TRADE_APPROVER_TOKENS,
    VALIO_API_KEY,
)
from app.database import database
from app.domain import (
    ApprovalRecord,
    ApprovalReview,
    BuyerRequest,
    CandidateProduct,
    PurchasePlan,
    RequestLine,
)
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
    text: str = Field(..., min_length=1, max_length=4000)
    customer_id: str = "customer-001"
    agent_id: str = "demo-agent"
    requested_date: date | None = None


class InterpretedRequestLine(BaseModel):
    query: str | None = None
    quantity: Decimal | None = None
    unit: str | None = None
    requested_unit_text: str | None = None


class StructuredRequestInterpretation(BaseModel):
    agent_id: str
    customer_id: str
    requested_date: date | None = None
    line_items: list[InterpretedRequestLine] = Field(default_factory=list)
    buyer_text: str
    missing_fields: list[str] = Field(default_factory=list)
    clarification_questions: list[str] = Field(default_factory=list)
    multiple_items_detected: bool = False


class RequestInterpretationResponse(BaseModel):
    request: StructuredRequestInterpretation
    candidate_products: list[CandidateProduct]
    clarification_required: bool


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


class ApprovalResponse(BaseModel):
    approval: ApprovalRecord


class ApprovalListResponse(BaseModel):
    approvals: list[ApprovalRecord]


_NATURAL_UNITS = {
    "bar", "bars", "bottle", "bottles", "box", "boxes", "case", "cases",
    "cl", "crate", "crates", "each", "g", "gram", "grams", "kg", "kgs",
    "kilogram", "kilograms", "l", "liter", "liters", "litre", "litres",
    "ml", "pack", "packs", "pc", "pcs", "piece", "pieces", "unit", "units",
}
_CURRENCY_WORDS = {
    "cent", "cents", "dollar", "dollars", "eur", "euro", "euros", "gbp",
    "pound", "pounds", "usd", "budget", "cost", "maximum", "price", "spend",
    "under",
}
_REQUEST_STOP_WORDS = {
    "a", "an", "and", "available", "based", "before", "by", "customer",
    "deliver", "delivery", "find", "for", "get", "i", "in", "item", "items",
    "like", "me", "meets", "need", "of", "on", "option", "order", "our",
    "please", "price", "product", "products", "purchase", "requirements",
    "the", "that", "to", "want", "we", "with", "would",
}
_WEEKDAYS = {
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
    "sunday", "tomorrow", "today", "next", "this", "week", "weekend",
    "jan", "january", "feb", "february", "mar", "march", "apr", "april",
    "may", "jun", "june", "jul", "july", "aug", "august", "sep",
    "september", "oct", "october", "nov", "november", "dec", "december",
}
_QUANTITY_PATTERN = re.compile(
    r"(?<![\w])(?:\d{1,3}(?:[ ,]\d{3})+|\d+)(?:[.,]\d+)?(?![\w])"
)


def _without_account_and_iso_date(text: str) -> str:
    without_date = re.sub(
        r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}[/.]\d{1,2}[/.]\d{2,4}\b",
        " ",
        text,
    )
    month_names = (
        "jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        "jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
    )
    without_date = re.sub(
        rf"\b(?:{month_names})\s+\d{{1,2}}(?:st|nd|rd|th)?"
        rf"(?:,?\s+(?:19|20)\d{{2}})?\b|"
        rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{month_names})"
        rf"(?:\s+(?:19|20)\d{{2}})?\b",
        " ",
        without_date,
        flags=re.IGNORECASE,
    )
    without_date = re.sub(r"\b(?:19|20)\d{2}\b", " ", without_date)
    return re.sub(
        r"\b(?:for|to)\s+(?:the\s+)?(?:customer|account|client)\s+[A-Za-z0-9_-]+\b",
        " ",
        without_date,
        flags=re.IGNORECASE,
    )


def _parse_number(value: str) -> Decimal | None:
    normalized = value.replace(" ", "")
    if "," in normalized and "." in normalized:
        if normalized.rfind(",") > normalized.rfind("."):
            normalized = normalized.replace(".", "").replace(",", ".")
        else:
            normalized = normalized.replace(",", "")
    elif "," in normalized:
        if re.fullmatch(r"\d{1,3}(?:,\d{3})+", normalized):
            normalized = normalized.replace(",", "")
        else:
            normalized = normalized.replace(",", ".")
    elif "." in normalized and re.fullmatch(r"\d{1,3}(?:\.\d{3})+", normalized):
        normalized = normalized.replace(".", "")
    try:
        quantity = Decimal(normalized)
    except InvalidOperation:
        return None
    return quantity if quantity.is_finite() and quantity > 0 else None


def _extract_quantity_and_unit(text: str) -> tuple[Decimal | None, str | None]:
    cleaned = _without_account_and_iso_date(text)
    for match in _QUANTITY_PATTERN.finditer(cleaned):
        following = cleaned[match.end():]
        if re.match(r"\s*[%$€£]", following):
            continue
        unit_match = re.match(r"\s*([A-Za-z]+)\b", following)
        following_word = unit_match.group(1).lower() if unit_match else None
        if following_word in _CURRENCY_WORDS:
            continue
        before = cleaned[:match.start()]
        has_purchase_verb = bool(
            re.search(
                r"\b(?:need|order|buy|require|want|get)\s*(?:about|around|approximately)?\s*$",
                before,
                re.IGNORECASE,
            )
        )
        if following_word not in _NATURAL_UNITS and not has_purchase_verb:
            continue
        quantity = _parse_number(match.group())
        if quantity is None:
            continue
        requested_unit = following_word if following_word in _NATURAL_UNITS else None
        return quantity, requested_unit
    return None, None


def _extract_product_query(text: str) -> str | None:
    cleaned = _without_account_and_iso_date(text)
    cleaned = _QUANTITY_PATTERN.sub(" ", cleaned)
    words = re.findall(r"[\w-]+", cleaned.lower())
    query_words = [
        word for word in words
        if word not in _REQUEST_STOP_WORDS
        and word not in _NATURAL_UNITS
        and word not in _CURRENCY_WORDS
        and word not in _WEEKDAYS
    ]
    return " ".join(query_words) or None


def _approver_tokens() -> dict[str, str]:
    tokens: dict[str, str] = {}
    for entry in TRUSTED_TRADE_APPROVER_TOKENS.split(","):
        approver, separator, token = entry.partition(":")
        if separator and approver.strip() and token.strip():
            tokens[approver.strip()] = token.strip()
    return tokens


def _agent_tokens() -> dict[str, str]:
    tokens: dict[str, str] = {}
    if not TRUSTED_TRADE_AGENT_TOKENS:
        return tokens
    for entry in TRUSTED_TRADE_AGENT_TOKENS.split(","):
        agent, separator, token = entry.partition(":")
        agent = agent.strip()
        token = token.strip()
        if not separator or not agent or not token or agent in tokens:
            raise HTTPException(status_code=503, detail="Agent token configuration is invalid")
        tokens[agent] = token
    return tokens


def _authenticated_agent(token: str, requested_agent_id: str | None = None) -> str:
    tokens = _agent_tokens()
    if not tokens:
        if APP_ENV.lower() in {"production", "prod"}:
            raise HTTPException(
                status_code=503,
                detail="Agent authentication is not configured",
            )
        if requested_agent_id and requested_agent_id != "demo-agent":
            raise HTTPException(status_code=401, detail="Invalid agent credentials")
        return requested_agent_id or "demo-agent"

    token_bytes = token.encode("utf-8")
    matches = [
        agent
        for agent, expected in tokens.items()
        if secrets.compare_digest(token_bytes, expected.encode("utf-8"))
    ]
    if len(matches) != 1:
        raise HTTPException(status_code=401, detail="Invalid agent credentials")
    authenticated_agent = matches[0]
    if requested_agent_id and requested_agent_id != authenticated_agent:
        raise HTTPException(status_code=403, detail="Agent identity does not match the token")
    return authenticated_agent


def _authenticated_approver(token: str) -> str:
    token_bytes = token.encode("utf-8")
    matches = [
        approver
        for approver, expected in _approver_tokens().items()
        if secrets.compare_digest(token_bytes, expected.encode("utf-8"))
    ]
    if len(matches) != 1 or matches[0] not in approval_service.allowed_approvers:
        raise HTTPException(status_code=401, detail="Invalid or unauthorized approver credentials")
    return matches[0]


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
    comparison_price = price.get("comparisonPrice")
    if not isinstance(comparison_price, dict):
        comparison_price = {}
    comparison_price_value = _decimal_or_none(comparison_price.get("value"))
    comparison_unit = price.get("comparisonUnit")
    if comparison_unit not in {"KG", "L"}:
        comparison_unit = None
    comparison_currency = comparison_price.get("currency") or currency
    price_ok = bool(
        price
        and not price.get("error", True)
        and unit_price is not None
        and unit_price >= 0
        and currency == "EUR"
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
        "comparison_price": (
            str(comparison_price_value)
            if comparison_price_value is not None and comparison_price_value >= 0
            else None
        ),
        "comparison_unit": comparison_unit,
        "comparison_currency": comparison_currency,
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


def _infer_request(
    text: str,
    customer_id: str,
    agent_id: str,
    requested_date: date | None,
) -> StructuredRequestInterpretation:
    query = _extract_product_query(text)
    quantity, requested_unit = _extract_quantity_and_unit(text)
    if requested_date is None:
        date_match = re.search(r"\b\d{4}-\d{2}-\d{2}\b", text)
        if date_match:
            try:
                requested_date = date.fromisoformat(date_match.group())
            except ValueError:
                requested_date = None

    missing_fields: list[str] = []
    questions: list[str] = []
    if not query:
        missing_fields.append("product")
        questions.append("Which product should I search for?")
    if quantity is None:
        missing_fields.append("quantity")
        questions.append("What quantity do you need?")
    missing_fields.append("sales_unit")
    if requested_unit:
        questions.append(
            f"You specified {requested_unit}; choose a catalog sales unit and confirm the equivalent quantity. No unit conversion is assumed."
        )
    else:
        questions.append(
            "Which catalog sales-unit code should be used? No unit conversion is assumed."
        )
    if requested_date is None:
        missing_fields.append("requested_date")
        questions.append("What delivery date is required? Provide an explicit date.")
    multiple_items = bool(re.search(r"\b(?:and|plus)\b", text, flags=re.IGNORECASE))
    if multiple_items:
        missing_fields.append("line_items")
        questions.append(
            "This demo interprets one product line at a time. Split the request into individual product requests."
        )

    return StructuredRequestInterpretation(
        agent_id=agent_id,
        customer_id=customer_id,
        requested_date=requested_date,
        line_items=[
            InterpretedRequestLine(
                query=query,
                quantity=quantity,
                requested_unit_text=requested_unit,
            )
        ],
        buyer_text=text,
        missing_fields=missing_fields,
        clarification_questions=questions,
        multiple_items_detected=multiple_items,
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def demo_ui() -> FileResponse:
    return FileResponse(Path(BASE_DIR, "app", "static", "index.html"))


@app.post("/api/requests/parse", response_model=RequestInterpretationResponse)
def parse_request(
    payload: RequestInterpretationRequest,
    agent_token: str = Header(default="", alias="X-Agent-Token"),
) -> dict[str, Any]:
    agent_id = _authenticated_agent(agent_token, payload.agent_id)
    request = _infer_request(payload.text, payload.customer_id, agent_id, payload.requested_date)
    query = request.line_items[0].query
    choices = catalog_service.search(query, limit=3) if query else []
    if query and not choices:
        request.missing_fields.append("product")
        request.clarification_questions.append(
            f"No catalog matches were found for '{query}'. What product did you mean?"
        )
    audit_service.record(
        actor=request.agent_id,
        action="request_interpreted",
        outcome="clarification_required" if request.missing_fields else "interpreted",
        entity_type="customer",
        entity_id=request.customer_id,
        metadata={
            "missing_fields": request.missing_fields,
            "candidate_count": len(choices),
        },
    )
    return {
        "request": request.model_dump(mode="json"),
        "candidate_products": [product.model_dump(mode="json") for product in choices],
        "clarification_required": bool(request.missing_fields),
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
def build_purchase_plan(
    payload: PurchasePlanRequest,
    agent_token: str = Header(default="", alias="X-Agent-Token"),
) -> dict[str, Any]:
    agent_id = _authenticated_agent(agent_token, payload.agent_id)
    request = BuyerRequest(
        agent_id=agent_id,
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
        assessments.sort(
            key=lambda item: (
                not item["eligible"],
                Decimal(item["unit_price"]) if item["eligible"] else Decimal("0"),
                -item["relevance_score"],
                item["product_id"],
            )
        )
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
            "comparison_price": selected["comparison_price"],
            "comparison_unit": selected["comparison_unit"],
            "comparison_currency": selected["comparison_currency"],
            "available_amount": selected["available_amount"],
            "availability_quantity_is_unknown": selected["availability_quantity_is_unknown"],
            "available_date": selected["available_date"],
            "closing_time": selected["closing_time"],
            "availability_checked_at": selected["availability_checked_at"],
            "availability_status": selected["availability_status"],
            "status": "PENDING_HUMAN_APPROVAL",
            "selection_reason": "lowest verified catalog price per matching sales unit among eligible candidates",
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
        policy_version=policy.policy_version,
        policy_reasons=policy.reasons,
        policy_facts=policy.facts,
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


@app.post("/api/approvals/request", response_model=ApprovalResponse)
def request_approval(
    payload: ApprovalRequest,
    agent_token: str = Header(default="", alias="X-Agent-Token"),
) -> dict[str, Any]:
    agent_id = _authenticated_agent(agent_token)
    try:
        approval = approval_service.create_request(payload.plan_id, requesting_agent=agent_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
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


@app.post("/api/approvals/decide", response_model=ApprovalResponse)
def decide_approval(
    payload: ApprovalDecisionRequest,
    approver_token: str = Header(default="", alias="X-Approver-Token"),
) -> dict[str, Any]:
    authenticated_approver = _authenticated_approver(approver_token)
    if payload.approver != authenticated_approver:
        raise HTTPException(status_code=403, detail="Approver identity does not match the token")
    try:
        approval = approval_service.decide(
            payload.approval_id,
            authenticated_approver,
            payload.approved,
        )
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


@app.get("/api/approvals", response_model=ApprovalListResponse)
def list_approvals(
    limit: int = Query(default=50, ge=1, le=200),
    approver_token: str = Header(default="", alias="X-Approver-Token"),
) -> dict[str, Any]:
    _authenticated_approver(approver_token)
    return {
        "approvals": [
            record.model_dump(mode="json")
            for record in approval_service.list_records(limit)
        ]
    }


@app.get("/api/approvals/{approval_id}", response_model=ApprovalReview)
def get_approval(
    approval_id: str,
    approver_token: str = Header(default="", alias="X-Approver-Token"),
) -> dict[str, Any]:
    _authenticated_approver(approver_token)
    review = approval_service.get_review(approval_id)
    if review is None:
        raise HTTPException(status_code=404, detail="approval request not found")
    return review.model_dump(mode="json")


@app.get("/api/audit")
def list_audit_events(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    return {"events": audit_service.list_events(limit)}
