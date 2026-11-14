from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.config import TRUSTED_TRADE_APPROVAL_TTL_HOURS, TRUSTED_TRADE_APPROVERS
from app.database import ApprovalRow, Database, database
from app.domain import ApprovalRecord, ApprovalReview, ApprovalStatus


class ApprovalExpiredError(ValueError):
    def __init__(self, transitioned: bool = False) -> None:
        super().__init__("approval request has expired")
        self.transitioned = transitioned


class ApprovalService:
    def __init__(
        self,
        store: Database = database,
        allowed_approvers: set[str] | None = None,
        approval_ttl: timedelta | None = None,
    ) -> None:
        self.store = store
        if approval_ttl is None:
            try:
                ttl_hours = int(TRUSTED_TRADE_APPROVAL_TTL_HOURS)
            except ValueError as exc:
                raise ValueError("approval TTL must be an integer number of hours") from exc
            if not 1 <= ttl_hours <= 8760:
                raise ValueError("approval TTL must be between 1 and 8760 hours")
            approval_ttl = timedelta(hours=ttl_hours)
        if not timedelta(hours=1) <= approval_ttl <= timedelta(hours=8760):
            raise ValueError("approval TTL must be between 1 and 8760 hours")
        self.approval_ttl = approval_ttl
        self.allowed_approvers = (
            allowed_approvers
            if allowed_approvers is not None
            else {
                approver.strip()
                for approver in TRUSTED_TRADE_APPROVERS.split(",")
                if approver.strip()
            }
        )

    def _expires_at(self, created_at: str) -> datetime:
        created = datetime.fromisoformat(created_at)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return created + self.approval_ttl

    def _is_expired(self, created_at: str, now: datetime | None = None) -> bool:
        return (now or datetime.now(timezone.utc)) >= self._expires_at(created_at)

    def expire_overdue(self, approval_id: str | None = None) -> list[str]:
        now = datetime.now(timezone.utc)
        expired: list[str] = []
        with self.store.session() as session, session.begin():
            statement = select(ApprovalRow).where(
                ApprovalRow.status == ApprovalStatus.PENDING.value
            )
            if approval_id is not None:
                statement = statement.where(ApprovalRow.approval_id == approval_id)
            rows = session.scalars(statement).all()
            for row in rows:
                if not self._is_expired(row.created_at, now):
                    continue
                result = session.execute(
                    update(ApprovalRow)
                    .where(
                        ApprovalRow.approval_id == row.approval_id,
                        ApprovalRow.status == ApprovalStatus.PENDING.value,
                    )
                    .values(status=ApprovalStatus.EXPIRED.value)
                )
                if result.rowcount == 1:
                    expired.append(row.approval_id)
        return expired

    def create_request(
        self,
        plan_id: str,
        requesting_agent: str | None = None,
    ) -> ApprovalRecord:
        plan = self.store.get_plan(plan_id)
        if plan is None:
            raise KeyError("purchase plan not found")
        if requesting_agent is not None and plan["agent_id"] != requesting_agent:
            raise PermissionError("agent is not authorized for this purchase plan")
        if plan["decision"] != "PENDING_APPROVAL":
            raise ValueError("only plans requiring approval can have an approval request")
        with self.store.session() as session:
            existing = session.scalar(
                select(ApprovalRow).where(ApprovalRow.plan_id == plan_id)
            )
        if existing is not None:
            raise ValueError("an approval request already exists for this plan")

        snapshot = plan["snapshot"]
        snapshot_json = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
        snapshot_hash = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
        created_at = datetime.now(timezone.utc).isoformat()
        approval = ApprovalRecord(
            approval_id=f"apr-{uuid4().hex}",
            plan_id=plan_id,
            snapshot_hash=snapshot_hash,
            status=ApprovalStatus.PENDING,
            created_at=created_at,
            expires_at=self._expires_at(created_at).isoformat(),
        )
        try:
            with self.store.session() as session, session.begin():
                session.add(
                    ApprovalRow(
                        approval_id=approval.approval_id,
                        plan_id=approval.plan_id,
                        snapshot_hash=approval.snapshot_hash,
                        snapshot_json=snapshot_json,
                        status=approval.status.value,
                        created_at=approval.created_at,
                    )
                )
        except IntegrityError as exc:
            raise ValueError("an approval request already exists for this plan") from exc
        return approval

    def decide(self, approval_id: str, approver: str, approved: bool) -> ApprovalRecord:
        expired = self.expire_overdue(approval_id)
        if expired:
            raise ApprovalExpiredError(transitioned=True)
        with self.store.session() as session:
            row = session.get(ApprovalRow, approval_id)
            if row is None:
                raise KeyError("approval request not found")
            plan_id = row.plan_id
            snapshot_hash = row.snapshot_hash
            snapshot_json = row.snapshot_json
            status = row.status

        if status == ApprovalStatus.EXPIRED.value:
            raise ApprovalExpiredError()
        plan_info = self.store.get_plan(plan_id)
        if plan_info is None:
            raise KeyError("purchase plan not found")
        current_snapshot = json.dumps(
            plan_info["snapshot"], sort_keys=True, separators=(",", ":")
        )
        current_hash = hashlib.sha256(current_snapshot.encode("utf-8")).hexdigest()
        stored_snapshot = json.dumps(
            json.loads(snapshot_json), sort_keys=True, separators=(",", ":")
        )
        if current_hash != snapshot_hash or stored_snapshot != current_snapshot:
            raise ValueError("approval snapshot mismatch: the order snapshot has changed")
        if status != ApprovalStatus.PENDING.value:
            raise ValueError("duplicate or contradictory approval decision")
        if approver == plan_info["agent_id"]:
            raise ValueError("the purchasing agent cannot approve its own plan")
        if approver not in self.allowed_approvers:
            raise ValueError("approver identity is not authorized")

        decision = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
        decided_at = datetime.now(timezone.utc).isoformat()
        expired_during_decision = False
        decision_rowcount = 0
        with self.store.session() as session, session.begin():
            current_row = session.get(ApprovalRow, approval_id)
            if current_row is None:
                raise KeyError("approval request not found")
            if current_row.status != ApprovalStatus.PENDING.value:
                if current_row.status == ApprovalStatus.EXPIRED.value:
                    raise ApprovalExpiredError()
                raise ValueError("duplicate or contradictory approval decision")
            if self._is_expired(current_row.created_at):
                result = session.execute(
                    update(ApprovalRow)
                    .where(
                        ApprovalRow.approval_id == approval_id,
                        ApprovalRow.status == ApprovalStatus.PENDING.value,
                    )
                    .values(status=ApprovalStatus.EXPIRED.value)
                )
                expired_during_decision = result.rowcount == 1
            else:
                result = session.execute(
                    update(ApprovalRow)
                    .where(
                        ApprovalRow.approval_id == approval_id,
                        ApprovalRow.status == ApprovalStatus.PENDING.value,
                    )
                    .values(
                        status=decision.value,
                        approver=approver,
                        decided_at=decided_at,
                        comment="approved" if approved else "rejected",
                    )
                )
                decision_rowcount = result.rowcount
        if expired_during_decision:
            raise ApprovalExpiredError(transitioned=True)
        if decision_rowcount != 1:
            raise ValueError("duplicate or contradictory approval decision")

        return ApprovalRecord(
            approval_id=approval_id,
            plan_id=plan_info["snapshot"]["plan_id"],
            snapshot_hash=snapshot_hash,
            status=decision,
            approver=approver,
            created_at=row.created_at,
            expires_at=self._expires_at(row.created_at).isoformat(),
            decided_at=decided_at,
            comment="approved" if approved else "rejected",
        )

    def list_records(self, limit: int = 50) -> list[ApprovalRecord]:
        with self.store.session() as session:
            rows = session.scalars(
                select(ApprovalRow).order_by(ApprovalRow.created_at.desc()).limit(limit)
            ).all()
            return [
                ApprovalRecord(
                    approval_id=row.approval_id,
                    plan_id=row.plan_id,
                    snapshot_hash=row.snapshot_hash,
                    status=ApprovalStatus(row.status),
                    approver=row.approver,
                    created_at=row.created_at,
                    expires_at=self._expires_at(row.created_at).isoformat(),
                    decided_at=row.decided_at,
                    comment=row.comment,
                )
                for row in rows
            ]

    def get_review(self, approval_id: str) -> ApprovalReview | None:
        with self.store.session() as session:
            row = session.get(ApprovalRow, approval_id)
            if row is None:
                return None
            return ApprovalReview(
                approval=ApprovalRecord(
                    approval_id=row.approval_id,
                    plan_id=row.plan_id,
                    snapshot_hash=row.snapshot_hash,
                    status=ApprovalStatus(row.status),
                    approver=row.approver,
                    created_at=row.created_at,
                    expires_at=self._expires_at(row.created_at).isoformat(),
                    decided_at=row.decided_at,
                    comment=row.comment,
                ),
                snapshot=json.loads(row.snapshot_json),
            )
