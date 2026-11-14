from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from app.domain import ApprovalRecord, ApprovalStatus


class ApprovalService:
    def __init__(self) -> None:
        self.records: dict[str, ApprovalRecord] = {}

    def create_request(self, plan_id: str, snapshot: dict) -> ApprovalRecord:
        snapshot_json = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
        snapshot_hash = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
        approval = ApprovalRecord(
            approval_id=f"apr-{len(self.records) + 1:04d}",
            plan_id=plan_id,
            snapshot_hash=snapshot_hash,
            status=ApprovalStatus.PENDING,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self.records[approval.approval_id] = approval
        return approval

    def decide(self, approval_id: str, approver: str, approved: bool, snapshot: dict) -> ApprovalRecord:
        approval = self.records[approval_id]
        snapshot_json = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
        current_hash = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
        if current_hash != approval.snapshot_hash:
            raise ValueError("approval snapshot mismatch: the order snapshot has changed")
        if approval.status in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}:
            raise ValueError("duplicate or contradictory approval decision")

        approval.status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
        approval.approver = approver
        approval.comment = "approved" if approved else "rejected"
        return approval
