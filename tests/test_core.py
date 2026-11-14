from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.domain import BuyerRequest, PolicyDecision, RequestLine
from app.main import app
from app.services.approval_service import ApprovalService
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
    response = client.get("/api/catalog/search", params={"query": "chocolate", "limit": 3})
    assert response.status_code == 200
    payload = response.json()
    assert payload["results"]
    assert payload["results"][0]["product_id"]


def test_policy_engine_rejects_unverified_order() -> None:
    request = BuyerRequest(
        agent_id="demo-agent",
        customer_id="customer-001",
        requested_date="2026-11-16",
        line_items=[RequestLine(query="chocolate", quantity=Decimal("10"), unit="pcs")],
    )
    evaluation = PolicyEngine().evaluate(request, [{"category": "17301"}], Decimal("150"), False)
    assert evaluation.decision == PolicyDecision.REJECTED
    assert "price and availability have not been verified" in " ".join(evaluation.reasons)


def test_approval_service_detects_snapshot_mismatch() -> None:
    service = ApprovalService()
    snapshot = {"plan_id": "plan-1", "customer_id": "customer-001", "lines": [{"query": "chocolate"}]}
    approval = service.create_request("plan-1", snapshot)
    with pytest.raises(ValueError, match="snapshot mismatch"):
        service.decide(approval.approval_id, "human-approver", True, {"plan_id": "plan-2", "customer_id": "customer-001"})
