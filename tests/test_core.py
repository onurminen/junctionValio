from __future__ import annotations

from decimal import Decimal
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain import BuyerRequest, PolicyDecision, RequestLine
from app.database import ApprovalRow, Database, PurchasePlanRow
from app.database import database
from app.main import app
from app.services.approval_service import ApprovalService
from app.services.audit_service import AuditService
from app.services.policy_engine import PolicyEngine

client = TestClient(app)


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
    assert payload["candidate_products"]


def test_catalog_search_returns_results() -> None:
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
    events = client.get("/api/audit?limit=1").json()["events"]
    assert events[0]["correlation_id"] == "corr-catalog-test"


def test_demo_ui_and_audit_store_are_available(tmp_path) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Trusted Trade" in response.text

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
    assert "price and availability have not been verified" in " ".join(evaluation.reasons)


def test_policy_engine_returns_auto_approval_and_pending_approval() -> None:
    request = BuyerRequest(
        agent_id="demo-agent",
        customer_id="customer-001",
        requested_date="2026-11-16",
        line_items=[RequestLine(query="chocolate", quantity=Decimal("2"), unit="KI")],
    )
    policy = PolicyEngine(
        approval_threshold=Decimal("250"),
        agent_customers={"demo-agent": {"customer-001"}},
        allowed_categories={"52200"},
    )
    candidate = [{"category": "52200"}]
    assert policy.evaluate(request, candidate, Decimal("100"), True, "EUR").decision == PolicyDecision.AUTO_APPROVED
    assert policy.evaluate(request, candidate, Decimal("250"), True, "EUR").decision == PolicyDecision.PENDING_APPROVAL


def test_policy_engine_rejects_unconfigured_categories_and_wrong_customer() -> None:
    request = BuyerRequest(
        agent_id="demo-agent",
        customer_id="other-customer",
        requested_date="2026-11-16",
        line_items=[RequestLine(query="chocolate", quantity=Decimal("2"), unit="KI")],
    )
    evaluation = PolicyEngine(
        agent_customers={"demo-agent": {"customer-001"}},
        allowed_categories=set(),
    ).evaluate(request, [{"category": "52200"}], Decimal("100"), True, "EUR")
    assert evaluation.decision == PolicyDecision.REJECTED
    assert any("not authorized" in reason for reason in evaluation.reasons)
    assert any("no product categories" in reason for reason in evaluation.reasons)


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
    requested = client.post("/api/approvals/request", json={"plan_id": plan_id})
    assert requested.status_code == 200
    approval_id = requested.json()["approval"]["approval_id"]
    monkeypatch.setattr(main, "TRUSTED_TRADE_APPROVER_TOKENS", "human-approver:test-secret")

    decision = {"approval_id": approval_id, "approver": "human-approver", "approved": True}
    assert client.post(
        "/api/approvals/decide",
        json=decision,
        headers={"X-Approver-Token": "wrong"},
    ).status_code == 401
    response = client.post(
        "/api/approvals/decide",
        json=decision,
        headers={"X-Approver-Token": "test-secret"},
    )
    assert response.status_code == 200
    assert response.json()["approval"]["status"] == "APPROVED"
    assert client.post(
        "/api/approvals/decide",
        json={**decision, "approved": False},
        headers={"X-Approver-Token": "test-secret"},
    ).status_code == 409


@pytest.mark.parametrize(
    ("available_amount", "available_date", "expected_verified"),
    [
        (0, None, False),
        (3, "2026-11-16", True),
    ],
)
def test_purchase_plan_verifies_requested_sales_units_and_stock(
    monkeypatch: pytest.MonkeyPatch,
    available_amount: int,
    available_date: str | None,
    expected_verified: bool,
) -> None:
    import app.main as main

    product_id = "460018"

    class MockValioClient:
        def fetch_price(self, customer: str, requested_date, product_ids: list[str]) -> dict:
            assert customer == "C-42"
            assert product_ids == [product_id]
            return {
                "currency": "EUR",
                "products": [{
                    "productId": product_id,
                    "error": False,
                    "catalogPrice": {"value": "35.94", "currency": "EUR"},
                }],
            }

        def fetch_availability(self, customer: str, requested_date, product_ids: list[str]) -> dict:
            return {
                "timeNow": "2026-11-14T14:00:00+0200",
                "products": [{
                    "productId": product_id,
                    "salesBlock": False,
                    "logisticModel": "Stock",
                    "scheduleLines": [{
                        "salesBlock": False,
                        "availableAmount": available_amount,
                        "availableDate": available_date,
                        "salesUnit": "KI",
                    }],
                }],
            }

    monkeypatch.setattr(main, "VALIO_API_KEY", "test-key")
    monkeypatch.setattr(main, "ValioClient", MockValioClient)
    response = client.post(
        "/api/purchase-plan/build",
        json={
            "agent_id": "demo-agent",
            "customer_id": "C-42",
            "requested_date": "2026-11-16",
            "line_items": [{"query": "chocolate", "quantity": "2", "unit": "KI"}],
        },
    )

    assert response.status_code == 200
    plan = response.json()["plan"]
    assert plan["total_value"] == "71.88"
    assert plan["verified"] is expected_verified
    assert plan["lines"][0]["status"] == ("VERIFIED" if expected_verified else "UNVERIFIED")
    assert "/api/orders/submit" not in app.openapi()["paths"]
