from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.adapters.valio_client import ValioClient, ValioRateLimited
from app.domain import BuyerRequest, PolicyDecision, RequestLine
from app.database import ApprovalRow, Database, PurchasePlanRow
from app.database import database
from app.main import app
from app.services.approval_service import ApprovalService
from app.services.audit_service import AuditService
from app.services.policy_engine import PolicyEngine

client = TestClient(app)


@pytest.fixture(autouse=True)
def allow_seeded_demo_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.main as main

    monkeypatch.setattr(main, "APP_ENV", "development")
    monkeypatch.setattr(main, "TRUSTED_TRADE_AGENT_TOKENS", "")


def test_request_parser_finds_a_candidate_product() -> None:
    response = client.post(
        "/api/requests/parse",
        json={
            "text": "I need 12 bars of chocolate for customer C-42 by 2026-11-16",
            "customer_id": "C-42",
            "agent_id": "demo-agent",
            "requested_date": "2026-11-16",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["request"]["customer_id"] == "C-42"
    assert Decimal(payload["request"]["line_items"][0]["quantity"]) >= Decimal("12")
    assert payload["request"]["line_items"][0]["query"] == "chocolate"
    assert payload["request"]["line_items"][0]["unit"] is None
    assert payload["candidate_products"]
    assert payload["clarification_required"] is True


def test_agent_token_binds_request_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as main

    monkeypatch.setattr(main, "APP_ENV", "production")
    monkeypatch.setattr(main, "TRUSTED_TRADE_AGENT_TOKENS", "trusted-agent:agent-secret")
    payload = {
        "text": "Find chocolate",
        "customer_id": "customer-001",
        "agent_id": "trusted-agent",
    }

    assert client.post("/api/requests/parse", json=payload).status_code == 401
    assert client.post(
        "/api/requests/parse",
        json=payload,
        headers={"X-Agent-Token": "wrong-secret"},
    ).status_code == 401
    assert client.post(
        "/api/requests/parse",
        json={**payload, "agent_id": "demo-agent"},
        headers={"X-Agent-Token": "agent-secret"},
    ).status_code == 403
    assert client.post(
        "/api/requests/parse",
        json=payload,
        headers={"X-Agent-Token": "agent-secret"},
    ).status_code == 200


def test_production_agent_routes_fail_closed_without_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as main

    monkeypatch.setattr(main, "APP_ENV", "production")
    monkeypatch.setattr(main, "TRUSTED_TRADE_AGENT_TOKENS", "")
    response = client.post(
        "/api/requests/parse",
        json={"text": "Find chocolate"},
    )
    assert response.status_code == 503


def test_invalid_agent_token_blocks_plan_before_supplier_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as main

    monkeypatch.setattr(main, "APP_ENV", "production")
    monkeypatch.setattr(main, "TRUSTED_TRADE_AGENT_TOKENS", "trusted-agent:agent-secret")

    class UnexpectedValioClient:
        def __init__(self) -> None:
            raise AssertionError("supplier client must not be constructed for an invalid agent")

    monkeypatch.setattr(main, "ValioClient", UnexpectedValioClient)
    response = client.post(
        "/api/purchase-plan/build",
        json={
            "agent_id": "trusted-agent",
            "customer_id": "customer-001",
            "requested_date": "2026-11-16",
            "line_items": [{"query": "chocolate", "quantity": "1", "unit": "KI"}],
        },
        headers={"X-Agent-Token": "wrong-secret"},
    )
    assert response.status_code == 401


def test_development_agent_identity_is_limited_to_demo_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as main

    monkeypatch.setattr(main, "APP_ENV", "development")
    monkeypatch.setattr(main, "TRUSTED_TRADE_AGENT_TOKENS", "")
    response = client.post(
        "/api/requests/parse",
        json={"text": "Find chocolate", "agent_id": "untrusted-agent"},
    )
    assert response.status_code == 401


def test_request_interpretation_does_not_invent_missing_values() -> None:
    import app.main as main

    interpreted = main._infer_request(
        "I need lactose-free milk by Monday",
        "customer-001",
        "demo-agent",
        None,
    )
    line = interpreted.line_items[0]
    assert line.query == "lactose-free milk"
    assert line.quantity is None
    assert line.unit is None
    assert interpreted.requested_date is None
    assert set(interpreted.missing_fields) == {
        "quantity",
        "sales_unit",
        "requested_date",
    }


def test_request_interpretation_handles_thousands_and_keeps_units_unconverted() -> None:
    import app.main as main

    interpreted = main._infer_request(
        "I need 1,000 litres of lactose-free milk by Monday",
        "customer-001",
        "demo-agent",
        None,
    )
    line = interpreted.line_items[0]
    assert line.query == "lactose-free milk"
    assert line.quantity == Decimal("1000")
    assert line.requested_unit_text == "litres"
    assert line.unit is None
    assert "sales_unit" in interpreted.missing_fields
    assert any("No unit conversion is assumed" in question for question in interpreted.clarification_questions)


def test_request_interpretation_does_not_read_delivery_date_as_quantity() -> None:
    import app.main as main

    interpreted = main._infer_request(
        "Please deliver milk by 2026-11-16",
        "customer-001",
        "demo-agent",
        None,
    )
    assert interpreted.line_items[0].query == "milk"
    assert interpreted.line_items[0].quantity is None
    assert interpreted.requested_date == date(2026, 11, 16)


def test_request_interpretation_flags_multiple_items_for_manual_split() -> None:
    import app.main as main

    interpreted = main._infer_request(
        "I need milk and cheese",
        "customer-001",
        "demo-agent",
        date(2026, 11, 16),
    )
    assert interpreted.multiple_items_detected is True
    assert "line_items" in interpreted.missing_fields
    assert any("Split the request" in question for question in interpreted.clarification_questions)


def test_request_interpretation_does_not_mistake_product_percentage_for_quantity() -> None:
    import app.main as main

    interpreted = main._infer_request(
        "Find 2% milk for Monday",
        "customer-001",
        "demo-agent",
        None,
    )
    assert interpreted.line_items[0].query == "milk"
    assert interpreted.line_items[0].quantity is None


def test_request_interpretation_does_not_treat_a_budget_as_quantity() -> None:
    import app.main as main

    interpreted = main._infer_request(
        "I need milk under 10 euros by Monday",
        "customer-001",
        "demo-agent",
        None,
    )
    assert interpreted.line_items[0].query == "milk"
    assert interpreted.line_items[0].quantity is None


def test_catalog_search_returns_results(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.main as main

    monkeypatch.setattr(main, "TRUSTED_TRADE_APPROVER_TOKENS", "human-approver:audit-secret")
    response = client.get(
        "/api/catalog/search",
        params={"query": "chocolate", "limit": 3},
        headers={"X-Correlation-ID": "corr-catalog-test"},
    )
    assert response.status_code == 200
    assert response.headers["X-Correlation-ID"] == "corr-catalog-test"
    payload = response.json()
    assert payload["results"]
    assert payload["results"][0]["product_id"]
    events = client.get(
        "/api/audit?limit=1",
        headers={"X-Approver-Token": "audit-secret"},
    ).json()["events"]
    assert events[0]["correlation_id"] == "corr-catalog-test"


def test_catalog_search_filters_explicit_category() -> None:
    from app.services.catalog_service import CatalogService

    service = CatalogService()
    unfiltered = service.search("chocolate", limit=20)
    filtered = service.search("chocolate", limit=20, category="52200")

    assert len({product.category for product in unfiltered}) > 1
    assert filtered
    assert all(product.category == "52200" for product in filtered)


@pytest.mark.parametrize(
    "params",
    [
        {"query": "chocolate", "limit": 0},
        {"query": "chocolate", "limit": 51},
        {"query": ""},
        {"query": "x" * 201},
    ],
)
def test_catalog_search_rejects_unbounded_inputs(params: dict[str, object]) -> None:
    response = client.get("/api/catalog/search", params=params)
    assert response.status_code == 422


def test_catalog_search_requires_explicit_free_from_allergen_data() -> None:
    from app.services.catalog_service import CatalogService

    service = CatalogService()
    unsafe_product = service.get_by_id("460000")
    assert unsafe_product is not None
    assert unsafe_product.allergen_classifications["AE"] == ["CONTAINS"]

    results = service.search(
        "chocolate",
        limit=20,
        excluded_allergen_ids=["AE"],
    )
    assert results
    assert all(
        product.allergen_classifications.get("AE") == ["FREE_FROM"]
        for product in results
    )
    assert "460018" in {product.product_id for product in results}

    unfiltered_unsafe_results = service.search(
        unsafe_product.name,
        limit=20,
    )
    assert "460000" in {product.product_id for product in unfiltered_unsafe_results}
    unsafe_results = service.search(
        unsafe_product.name,
        limit=20,
        excluded_allergen_ids=["AE"],
    )
    assert "460000" not in {product.product_id for product in unsafe_results}


@pytest.mark.parametrize(
    ("classifications", "eligible", "expected_status"),
    [
        ({"AE": ["FREE_FROM"]}, True, "FREE_FROM"),
        ({"AE": ["CONTAINS"]}, False, "CONTAINS"),
        ({"AE": ["MAY_CONTAIN"]}, False, "MAY_CONTAIN"),
        ({"AE": ["UNDECLARED"]}, False, "UNDECLARED"),
        ({}, False, "UNKNOWN"),
        ({"AE": ["FREE_FROM", "CONTAINS"]}, False, "CONTAINS"),
    ],
)
def test_candidate_allergen_constraints_fail_closed(
    classifications: dict[str, list[str]],
    eligible: bool,
    expected_status: str,
) -> None:
    import app.main as main

    line = RequestLine(
        query="product",
        quantity=Decimal("1"),
        unit="KI",
        excluded_allergen_ids=["ae"],
    )
    candidate = {
        "product_id": "test-product",
        "name": "Test product",
        "category": "52200",
        "sales_unit": "KI",
        "allowed_lot_size": "1",
        "allergen_classifications": classifications,
    }
    assessment = main._assess_candidate(
        line=line,
        candidate=candidate,
        price={
            "error": False,
            "catalogPrice": {"value": "1", "currency": "EUR"},
        },
        availability={
            "salesBlock": False,
            "scheduleLines": [{
                "salesBlock": False,
                "availableAmount": 3,
                "availableDate": "2026-11-16",
                "salesUnit": "KI",
            }],
        },
        availability_checked_at=None,
        requested_date=date(2026, 11, 16),
    )

    assert assessment["eligible"] is eligible
    assert assessment["excluded_allergen_statuses"] == {"AE": expected_status}


def test_audit_api_requires_approver_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as main

    monkeypatch.setattr(main, "TRUSTED_TRADE_APPROVER_TOKENS", "human-approver:audit-secret")
    assert client.get("/api/audit").status_code == 401
    response = client.get(
        "/api/audit",
        headers={"X-Approver-Token": "audit-secret"},
    )
    assert response.status_code == 200


def test_demo_ui_and_audit_store_are_available(tmp_path) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Trusted Trade" in response.text
    assert "nothing is sent to the supplier" in response.text
    assert "Human approval queue" in response.text
    assert 'id="approver-token"' in response.text
    assert 'id="category"' in response.text
    assert 'id="excluded-allergen-codes"' in response.text
    assert 'id="audit-filter"' in response.text

    store = Database(f"sqlite:///{tmp_path / 'audit.db'}")
    audit = AuditService(store)
    audit.record(
        actor="demo-agent",
        action="purchase_plan_evaluated",
        outcome="REJECTED",
        entity_type="purchase_plan",
        entity_id="plan-test",
        metadata={"verified": False},
    )
    reopened = AuditService(Database(f"sqlite:///{tmp_path / 'audit.db'}"))
    events = reopened.list_events()
    assert events[0]["action"] == "purchase_plan_evaluated"
    assert events[0]["metadata"] == {"verified": False}


def test_policy_engine_rejects_unverified_order() -> None:
    request = BuyerRequest(
        agent_id="demo-agent",
        customer_id="customer-001",
        requested_date="2026-11-16",
        line_items=[RequestLine(query="chocolate", quantity=Decimal("10"), unit="pcs")],
    )
    evaluation = PolicyEngine(
        agent_customers={"demo-agent": {"customer-001"}},
        allowed_categories={"17301"},
    ).evaluate(request, [{"category": "17301"}], Decimal("150"), False, "EUR")
    assert evaluation.decision == PolicyDecision.REJECTED
    assert "not verified" in " ".join(evaluation.reasons)


def test_policy_engine_returns_auto_approval_and_pending_approval() -> None:
    request = BuyerRequest(
        agent_id="demo-agent",
        customer_id="customer-001",
        requested_date="2026-11-16",
        line_items=[RequestLine(query="chocolate", quantity=Decimal("2"), unit="KI")],
    )
    policy = PolicyEngine(
        agent_customers={"demo-agent": {"customer-001"}},
        allowed_categories={"52200"},
    )
    candidate = [{"category": "52200"}]
    assert policy.evaluate(request, candidate, Decimal("100"), True, "EUR").decision == PolicyDecision.PENDING_APPROVAL
    autonomous = PolicyEngine(
        agent_customers={"demo-agent": {"customer-001"}},
        allowed_categories={"52200"},
        require_human_approval=False,
    )
    assert autonomous.evaluate(request, candidate, Decimal("100"), True, "EUR").decision == PolicyDecision.AUTO_APPROVED


def test_policy_engine_rejects_unconfigured_categories_and_wrong_customer() -> None:
    request = BuyerRequest(
        agent_id="demo-agent",
        customer_id="other-customer",
        requested_date="2026-11-16",
        line_items=[RequestLine(query="chocolate", quantity=Decimal("2"), unit="KI")],
    )
    evaluation = PolicyEngine(
        agent_customers={"demo-agent": {"customer-001"}},
        allowed_categories={"17301"},
    ).evaluate(request, [{"category": "52200"}], Decimal("100"), True, "EUR")
    assert evaluation.decision == PolicyDecision.REJECTED
    assert any("not authorized" in reason for reason in evaluation.reasons)


def test_policy_engine_rejects_unverified_free_form_hard_constraints() -> None:
    request = BuyerRequest(
        agent_id="demo-agent",
        customer_id="customer-001",
        requested_date="2026-11-16",
        line_items=[RequestLine(query="milk", quantity=Decimal("1"), unit="KI")],
        hard_constraints=["gluten-free"],
    )
    evaluation = PolicyEngine(
        agent_customers={"demo-agent": {"customer-001"}},
        allowed_categories={"52200"},
        require_human_approval=False,
    ).evaluate(request, [{"category": "52200"}], Decimal("10"), True, "EUR")

    assert evaluation.decision == PolicyDecision.REJECTED
    assert evaluation.facts["unverified_hard_constraint_count"] == 1
    assert any("supported structured fields" in reason for reason in evaluation.reasons)


def test_purchase_plan_rejects_blank_hard_constraint_before_supplier_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as main

    class UnexpectedValioClient:
        def __init__(self) -> None:
            raise AssertionError("invalid constraints must be rejected before supplier calls")

    monkeypatch.setattr(main, "ValioClient", UnexpectedValioClient)
    response = client.post(
        "/api/purchase-plan/build",
        json={
            "agent_id": "demo-agent",
            "customer_id": "customer-001",
            "requested_date": "2026-11-16",
            "line_items": [{"query": "milk", "quantity": "1", "unit": "KI"}],
            "hard_constraints": [" "],
        },
    )

    assert response.status_code == 422


def test_approval_service_detects_snapshot_mismatch() -> None:
    store = Database("sqlite:///:memory:")
    snapshot = {
        "plan_id": "plan-1",
        "agent_id": "demo-agent",
        "customer_id": "customer-001",
        "lines": [{"query": "chocolate"}],
    }
    store.save_plan("plan-1", snapshot, "PENDING_APPROVAL", "demo-agent", "customer-001")
    service = ApprovalService(store, allowed_approvers={"human-approver"})
    approval = service.create_request("plan-1")
    review = service.get_review(approval.approval_id)
    assert review is not None
    assert review.snapshot == snapshot
    with store.session() as session, session.begin():
        row = session.get(PurchasePlanRow, "plan-1")
        row.snapshot_json = json.dumps({**snapshot, "customer_id": "changed"})
    with pytest.raises(ValueError, match="snapshot mismatch"):
        service.decide(approval.approval_id, "human-approver", True)


def test_approval_is_persisted_and_agent_cannot_approve_own_plan() -> None:
    store = Database("sqlite:///:memory:")
    snapshot = {
        "plan_id": "plan-2",
        "agent_id": "demo-agent",
        "customer_id": "customer-001",
        "lines": [{"query": "chocolate"}],
    }
    store.save_plan("plan-2", snapshot, "PENDING_APPROVAL", "demo-agent", "customer-001")
    service = ApprovalService(store, allowed_approvers={"human-approver"})
    approval = service.create_request("plan-2")
    assert len(service.list_records()) == 1
    with pytest.raises(ValueError, match="cannot approve its own"):
        service.decide(approval.approval_id, "demo-agent", True)
    decided = service.decide(approval.approval_id, "human-approver", True)
    assert decided.status.value == "APPROVED"
    with store.session() as session:
        persisted = session.get(ApprovalRow, approval.approval_id)
        assert persisted.status == "APPROVED"
    with pytest.raises(ValueError, match="duplicate or contradictory"):
        service.decide(approval.approval_id, "human-approver", False)


def test_approval_api_requires_approver_token_and_persists_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as main

    monkeypatch.setattr(main, "APP_ENV", "production")
    monkeypatch.setattr(main, "TRUSTED_TRADE_AGENT_TOKENS", "demo-agent:agent-secret")
    plan_id = f"plan-{uuid4().hex}"
    snapshot = {
        "plan_id": plan_id,
        "agent_id": "demo-agent",
        "customer_id": "customer-001",
        "lines": [],
    }
    database.save_plan(
        plan_id,
        snapshot,
        "PENDING_APPROVAL",
        "demo-agent",
        "customer-001",
    )
    requested = client.post(
        "/api/approvals/request",
        json={"plan_id": plan_id},
        headers={"X-Agent-Token": "agent-secret"},
    )
    assert requested.status_code == 200
    approval_id = requested.json()["approval"]["approval_id"]
    monkeypatch.setattr(main, "TRUSTED_TRADE_APPROVER_TOKENS", "human-approver:test-secret")

    assert client.get("/api/approvals").status_code == 401
    headers = {"X-Approver-Token": "test-secret"}
    listed = client.get("/api/approvals", headers=headers)
    assert listed.status_code == 200
    assert any(item["approval_id"] == approval_id for item in listed.json()["approvals"])
    review = client.get(f"/api/approvals/{approval_id}", headers=headers)
    assert review.status_code == 200
    assert review.json()["snapshot"] == snapshot
    assert client.get(
        f"/api/approvals/{approval_id}",
        headers={"X-Approver-Token": "wrong"},
    ).status_code == 401

    decision = {"approval_id": approval_id, "approver": "human-approver", "approved": True}
    assert client.post(
        "/api/approvals/decide",
        json=decision,
        headers={"X-Approver-Token": "wrong"},
    ).status_code == 401
    response = client.post(
        "/api/approvals/decide",
        json=decision,
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["approval"]["status"] == "APPROVED"
    assert client.post(
        "/api/approvals/decide",
        json={**decision, "approver": "another-approver"},
        headers=headers,
    ).status_code == 403
    assert client.post(
        "/api/approvals/decide",
        json={**decision, "approved": False},
        headers=headers,
    ).status_code == 409


def test_approval_request_is_limited_to_plan_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as main

    monkeypatch.setattr(main, "APP_ENV", "production")
    monkeypatch.setattr(main, "TRUSTED_TRADE_AGENT_TOKENS", "trusted-agent:agent-secret")
    plan_id = f"plan-{uuid4().hex}"
    database.save_plan(
        plan_id,
        {
            "plan_id": plan_id,
            "agent_id": "another-agent",
            "customer_id": "customer-001",
            "lines": [],
        },
        "PENDING_APPROVAL",
        "another-agent",
        "customer-001",
    )
    response = client.post(
        "/api/approvals/request",
        json={"plan_id": plan_id},
        headers={"X-Agent-Token": "agent-secret"},
    )
    assert response.status_code == 403


def test_catalog_normalizes_fractional_technical_lot_size() -> None:
    from app.services.catalog_service import CatalogService

    product = CatalogService().get_by_id("460018")
    assert product is not None
    assert product.allowed_lot_size == Decimal("1")


def test_purchase_plan_uses_first_eligible_shortlist_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as main

    class MockValioClient:
        def fetch_price(self, customer: str, requested_date, product_ids: list[str]) -> dict:
            assert customer == "customer-001"
            return {
                "currency": "EUR",
                "products": [
                    {
                        "productId": product_id,
                        "error": False,
                        "catalogPrice": {"value": "10", "currency": "EUR"},
                    }
                    for product_id in product_ids
                ],
            }

        def fetch_availability(self, customer: str, requested_date, product_ids: list[str]) -> dict:
            return {
                "timeNow": "2026-11-14T14:00:00+0200",
                "products": [
                    {
                        "productId": product_id,
                        "closingTime": "2026-11-15T17:00:00+0200",
                        "salesBlock": False,
                        "logisticModel": "Stock",
                        "scheduleLines": [{
                            "salesBlock": False,
                            "availableAmount": 3 if product_id == "460105" else 0,
                            "availableDate": "2026-11-16" if product_id == "460105" else None,
                            "salesUnit": "KI",
                        }],
                    }
                    for product_id in product_ids
                ],
            }

    monkeypatch.setattr(main, "VALIO_API_KEY", "test-key")
    monkeypatch.setattr(main, "ValioClient", MockValioClient)
    monkeypatch.setattr(main, "APP_ENV", "production")
    monkeypatch.setattr(main, "TRUSTED_TRADE_AGENT_TOKENS", "demo-agent:agent-secret")
    response = client.post(
        "/api/purchase-plan/build",
        json={
            "agent_id": "demo-agent",
            "customer_id": "customer-001",
            "requested_date": "2026-11-16",
            "line_items": [{
                "query": "chocolate",
                "quantity": "2",
                "unit": "KI",
                "category": " 52200 ",
                "excluded_allergen_ids": ["ae"],
            }],
        },
        headers={"X-Agent-Token": "agent-secret"},
    )

    assert response.status_code == 200
    body = response.json()
    plan = body["plan"]
    assert plan["lines"][0]["product_id"] == "460105"
    assert plan["total_value"] == "20"
    assert plan["verified"] is True
    assert body["policy"]["decision"] == "PENDING_APPROVAL"
    assert plan["policy_version"] == body["policy"]["policy_version"]
    assert plan["policy_reasons"] == body["policy"]["reasons"]
    assert plan["policy_facts"]["human_approval_required"] is True
    assert plan["lines"][0]["excluded_allergen_ids"] == ["AE"]
    assert plan["lines"][0]["excluded_allergen_statuses"] == {"AE": "FREE_FROM"}
    assert all(item["category"] == "52200" for item in plan["lines"][0]["alternatives"])
    first_alternative = next(
        item for item in plan["lines"][0]["alternatives"]
        if item["product_id"] == "460018"
    )
    assert first_alternative["availability_status"] == "INSUFFICIENT_STOCK"
    assert "/api/orders/submit" not in app.openapi()["paths"]


def test_purchase_plan_prefers_lowest_eligible_price_per_sales_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as main

    prices = {"460018": "9.00", "460105": "5.00", "460220": "0.10", "460448": "7.00", "460452": "0.20"}

    class MockValioClient:
        def fetch_price(self, customer: str, requested_date, product_ids: list[str]) -> dict:
            return {
                "products": [
                    {
                        "productId": product_id,
                        "error": False,
                        "catalogPrice": {"value": prices[product_id], "currency": "EUR"},
                        "comparisonPrice": {"value": prices[product_id], "currency": "EUR"},
                        "comparisonUnit": "KG",
                    }
                    for product_id in product_ids
                ],
            }

        def fetch_availability(self, customer: str, requested_date, product_ids: list[str]) -> dict:
            return {
                "timeNow": "2026-11-14T14:00:00+0200",
                "products": [
                    {
                        "productId": product_id,
                        "closingTime": "2026-11-15T17:00:00+0200",
                        "salesBlock": False,
                        "scheduleLines": [{
                            "salesBlock": False,
                            "availableAmount": 3,
                            "availableDate": "2026-11-16",
                            "salesUnit": main.catalog_service.get_by_id(product_id).sales_unit,
                        }],
                    }
                    for product_id in product_ids
                ],
            }

    monkeypatch.setattr(main, "VALIO_API_KEY", "test-key")
    monkeypatch.setattr(main, "ValioClient", MockValioClient)
    response = client.post(
        "/api/purchase-plan/build",
        json={
            "agent_id": "demo-agent",
            "customer_id": "customer-001",
            "requested_date": "2026-11-16",
            "line_items": [{"query": "chocolate", "quantity": "2", "unit": "KI"}],
        },
    )

    assert response.status_code == 200
    line = response.json()["plan"]["lines"][0]
    assert line["product_id"] == "460105"
    assert line["unit_price"] == "5.00"
    assert line["selection_reason"].startswith("lowest verified catalog price")
    assert line["comparison_price"] == "5.00"
    assert line["comparison_unit"] == "KG"


def test_terminal_unknown_availability_allows_unbounded_quantity_for_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as main

    class MockValioClient:
        def fetch_price(self, customer: str, requested_date, product_ids: list[str]) -> dict:
            return {
                "currency": "EUR",
                "products": [{
                    "productId": "460452",
                    "error": False,
                    "catalogPrice": {"value": "1.5", "currency": "EUR"},
                }],
            }

        def fetch_availability(self, customer: str, requested_date, product_ids: list[str]) -> dict:
            return {
                "timeNow": "2026-11-14T14:00:00+0200",
                "products": [{
                    "productId": "460452",
                    "closingTime": "2026-11-15T17:00:00+0200",
                    "salesBlock": False,
                    "logisticModel": "Terminal",
                    "scheduleLines": [{
                        "salesBlock": False,
                        "availableAmount": 999999,
                        "availableDate": "2026-11-16",
                        "salesUnit": "CS",
                    }],
                }],
            }

    monkeypatch.setattr(main, "VALIO_API_KEY", "test-key")
    monkeypatch.setattr(main, "ValioClient", MockValioClient)
    response = client.post(
        "/api/purchase-plan/build",
        json={
            "agent_id": "demo-agent",
            "customer_id": "customer-001",
            "requested_date": "2026-11-16",
            "line_items": [{
                "query": "chocolate",
                "selected_product_id": "460452",
                "quantity": "100000",
                "unit": "CS",
            }],
        },
    )

    assert response.status_code == 200
    plan = response.json()["plan"]
    line = plan["lines"][0]
    assert plan["policy_decision"] == "PENDING_APPROVAL"
    assert plan["verified"] is False
    assert line["availability_status"] == "TERMINAL_AVAILABILITY_UNKNOWN"
    assert line["available_amount"] is None
    assert line["availability_quantity_is_unknown"] is True
    assert line["quantity"] == "100000"


def test_valio_429_sets_shared_cooldown_without_retrying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ValioClient, "_blocked_until", 0.0)
    request_count = 0

    def rate_limited(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(429, headers={"Retry-After": "5"}, json={"detail": "rate limited"})

    client_with_429 = ValioClient(
        base_url="https://mock.example",
        api_key="test-key",
        transport=httpx.MockTransport(rate_limited),
    )
    with pytest.raises(ValioRateLimited) as first:
        client_with_429.ping()
    assert first.value.retry_after == 30

    with pytest.raises(ValioRateLimited) as second:
        ValioClient(
            base_url="https://mock.example",
            api_key="test-key",
            transport=httpx.MockTransport(rate_limited),
        ).ping()
    assert second.value.retry_after > 0
    assert request_count == 1


def test_purchase_plan_returns_retry_after_on_valio_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as main

    class RateLimitedClient:
        def fetch_price(self, customer: str, requested_date, product_ids: list[str]) -> dict:
            raise ValioRateLimited(30)

    monkeypatch.setattr(main, "VALIO_API_KEY", "test-key")
    monkeypatch.setattr(main, "ValioClient", RateLimitedClient)
    response = client.post(
        "/api/purchase-plan/build",
        json={
            "agent_id": "demo-agent",
            "customer_id": "customer-001",
            "requested_date": "2026-11-16",
            "line_items": [{"query": "chocolate", "quantity": "1", "unit": "KI"}],
        },
    )
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "30"


def test_closing_time_is_a_cutoff_for_the_available_date() -> None:
    from app.main import _assess_candidate

    assessment = _assess_candidate(
        line=RequestLine(query="chocolate", quantity=Decimal("1"), unit="KI"),
        candidate={
            "product_id": "p1",
            "name": "Product",
            "sales_unit": "KI",
            "allowed_lot_size": "1",
        },
        price={
            "error": False,
            "catalogPrice": {"value": "2.50", "currency": "EUR"},
        },
        availability={
            "salesBlock": False,
            "closingTime": "2026-11-15T17:00:00+0200",
            "logisticModel": "Stock",
            "scheduleLines": [{
                "salesBlock": False,
                "availableAmount": 4,
                "availableDate": "2026-11-16",
                "salesUnit": "KI",
            }],
        },
        availability_checked_at="2026-11-15T17:01:00+0200",
        requested_date=date(2026, 11, 16),
    )
    assert assessment["availability_status"] == "CUTOFF_PASSED"
    assert assessment["eligible"] is False


def test_empty_schedule_is_unavailable_not_sales_blocked() -> None:
    from app.main import _assess_candidate

    assessment = _assess_candidate(
        line=RequestLine(query="product", quantity=Decimal("1"), unit="KI"),
        candidate={
            "product_id": "p1",
            "name": "Product",
            "sales_unit": "KI",
            "allowed_lot_size": "1",
        },
        price={
            "error": False,
            "catalogPrice": {"value": "2.50", "currency": "EUR"},
        },
        availability={
            "salesBlock": False,
            "logisticModel": "Stock",
            "scheduleLines": [],
        },
        availability_checked_at="2026-11-14T14:00:00+0200",
        requested_date=date(2026, 11, 16),
    )
    assert assessment["availability_status"] == "UNAVAILABLE"
    assert assessment["eligible"] is False
