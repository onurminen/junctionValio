from __future__ import annotations

from datetime import date
import threading
import time
from typing import Any
import math

import httpx

from app.config import VALIO_API_KEY, VALIO_BASE_URL


class ValioRateLimited(RuntimeError):
    def __init__(self, retry_after: int) -> None:
        super().__init__("Valio API rate limit reached")
        self.retry_after = retry_after


class ValioClient:
    _rate_limit_lock = threading.Lock()
    _blocked_until = 0.0

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

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        client_type = type(self)
        with client_type._rate_limit_lock:
            remaining = client_type._blocked_until - time.monotonic()
        if remaining > 0:
            raise ValioRateLimited(max(1, math.ceil(remaining)))

        with self._client() as client:
            response = client.request(method, path, **kwargs)
        if response.status_code == 429:
            try:
                server_retry = int(response.headers.get("Retry-After", "30"))
            except ValueError:
                server_retry = 30
            retry_after = max(30, server_retry)
            with client_type._rate_limit_lock:
                client_type._blocked_until = max(
                    client_type._blocked_until,
                    time.monotonic() + retry_after,
                )
            raise ValioRateLimited(retry_after)
        response.raise_for_status()
        return response.json()

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
        return self._request("GET", "/api/ping")

    def fetch_price(self, customer: str, requested_date: date, product_ids: list[str]) -> dict[str, Any]:
        if not self.base_url:
            raise RuntimeError("VALIO_BASE_URL is not configured")
        payload = {
            "customer": customer,
            "requestedDate": requested_date.isoformat(),
            "products": product_ids,
        }
        return self._request("POST", "/api/price", json=payload)

    def fetch_availability(self, customer: str, requested_date: date, product_ids: list[str]) -> dict[str, Any]:
        if not self.base_url:
            raise RuntimeError("VALIO_BASE_URL is not configured")
        payload = {
            "customer": customer,
            "requestedDate": requested_date.isoformat(),
            "products": product_ids,
        }
        return self._request("POST", "/api/availability", json=payload)
