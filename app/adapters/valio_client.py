from __future__ import annotations

from datetime import date
from typing import Any

import httpx

from app.config import VALIO_API_KEY, VALIO_BASE_URL


class ValioClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or VALIO_BASE_URL).rstrip("/")
        self.api_key = api_key or VALIO_API_KEY
        self.timeout = timeout
        self._transport = transport

    def _client(self) -> httpx.Client:
        if self._transport is not None:
            return httpx.Client(
                base_url=self.base_url,
                headers={"X-API-Key": self.api_key} if self.api_key else None,
                timeout=self.timeout,
                transport=self._transport,
            )
        return httpx.Client(
            base_url=self.base_url,
            headers={"X-API-Key": self.api_key} if self.api_key else None,
            timeout=self.timeout,
        )

    def ping(self) -> dict[str, Any]:
        if not self.base_url:
            raise RuntimeError("VALIO_BASE_URL is not configured")
        with self._client() as client:
            response = client.get("/api/ping")
            response.raise_for_status()
            return response.json()

    def fetch_price(self, customer: str, requested_date: date, product_ids: list[str]) -> dict[str, Any]:
        if not self.base_url:
            raise RuntimeError("VALIO_BASE_URL is not configured")
        payload = {
            "customer": customer,
            "requestedDate": requested_date.isoformat(),
            "products": product_ids,
        }
        with self._client() as client:
            response = client.post("/api/price", json=payload)
            response.raise_for_status()
            return response.json()

    def fetch_availability(self, customer: str, requested_date: date, product_ids: list[str]) -> dict[str, Any]:
        if not self.base_url:
            raise RuntimeError("VALIO_BASE_URL is not configured")
        payload = {
            "customer": customer,
            "requestedDate": requested_date.isoformat(),
            "products": product_ids,
        }
        with self._client() as client:
            response = client.post("/api/availability", json=payload)
            response.raise_for_status()
            return response.json()

    def submit_order(self, order_payload: dict[str, Any]) -> dict[str, Any]:
        if not self.base_url:
            raise RuntimeError("VALIO_BASE_URL is not configured")
        with self._client() as client:
            response = client.post("/api/order", json=order_payload)
            response.raise_for_status()
            return response.json()
