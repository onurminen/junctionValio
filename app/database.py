from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from typing import Any, Iterator

from sqlalchemy import ForeignKey, Integer, JSON, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import DATABASE_URL


class Base(DeclarativeBase):
    pass


class PurchasePlanRow(Base):
    __tablename__ = "purchase_plans"

    plan_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(120), nullable=False)
    customer_id: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)


class ApprovalRow(Base):
    __tablename__ = "approvals"

    approval_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("purchase_plans.plan_id"), nullable=False, unique=True
    )
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    approver: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    decided_at: Mapped[str | None] = mapped_column(String(40))
    comment: Mapped[str | None] = mapped_column(Text)


class AuditEventRow(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurred_at: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    correlation_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(120), nullable=False)
    action: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(80))
    entity_id: Mapped[str | None] = mapped_column(String(120))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class Database:
    def __init__(self, url: str = DATABASE_URL) -> None:
        options: dict[str, Any] = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            options["connect_args"] = {"check_same_thread": False}
            if ":memory:" in url:
                options["poolclass"] = StaticPool
        self.engine = create_engine(url, **options)
        self.session_factory = sessionmaker(self.engine, expire_on_commit=False)
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        with self.session_factory() as session:
            yield session

    def save_plan(
        self,
        plan_id: str,
        snapshot: dict[str, Any],
        decision: str,
        agent_id: str,
        customer_id: str,
    ) -> None:
        with self.session() as session, session.begin():
            session.add(PurchasePlanRow(
                plan_id=plan_id,
                snapshot_json=json.dumps(snapshot, sort_keys=True, separators=(",", ":")),
                decision=decision,
                agent_id=agent_id,
                customer_id=customer_id,
                created_at=datetime.now(timezone.utc).isoformat(),
            ))

    def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        with self.session() as session:
            row = session.get(PurchasePlanRow, plan_id)
            if row is None:
                return None
            return {
                "snapshot": json.loads(row.snapshot_json),
                "decision": row.decision,
                "agent_id": row.agent_id,
                "customer_id": row.customer_id,
            }

    def record_audit(
        self,
        *,
        correlation_id: str,
        actor: str,
        action: str,
        outcome: str,
        entity_type: str | None = None,
        entity_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self.session() as session, session.begin():
            session.add(AuditEventRow(
                occurred_at=datetime.now(timezone.utc).isoformat(),
                correlation_id=correlation_id,
                actor=actor,
                action=action,
                outcome=outcome,
                entity_type=entity_type,
                entity_id=entity_id,
                metadata_json=metadata or {},
            ))

    def list_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.session() as session:
            rows = session.scalars(
                select(AuditEventRow).order_by(AuditEventRow.id.desc()).limit(limit)
            ).all()
            return [{
                "occurred_at": row.occurred_at,
                "correlation_id": row.correlation_id,
                "actor": row.actor,
                "action": row.action,
                "outcome": row.outcome,
                "entity_type": row.entity_type,
                "entity_id": row.entity_id,
                "metadata": row.metadata_json,
            } for row in rows]


database = Database()
