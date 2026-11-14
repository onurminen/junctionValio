from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any
from uuid import uuid4

from app.database import Database, database


class AuditService:
    def __init__(self, store: Database = database) -> None:
        self.store = store
        self._correlation_id: ContextVar[str | None] = ContextVar(
            f"audit_correlation_{id(self)}", default=None
        )

    @contextmanager
    def correlation(self, correlation_id: str):
        token: Token[str | None] = self._correlation_id.set(correlation_id)
        try:
            yield
        finally:
            self._correlation_id.reset(token)

    def record(
        self,
        *,
        actor: str,
        action: str,
        outcome: str,
        entity_type: str | None = None,
        entity_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> str:
        request_id = correlation_id or self._correlation_id.get() or str(uuid4())
        self.store.record_audit(
            correlation_id=request_id,
            actor=actor,
            action=action,
            outcome=outcome,
            entity_type=entity_type,
            entity_id=entity_id,
            metadata=metadata,
        )
        return request_id

    def list_events(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.store.list_audit(limit)
