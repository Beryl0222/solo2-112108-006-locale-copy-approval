"""志愿者可提交的草稿、复用决定和分步核准状态。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import uuid

from .repository import atomic_write_json, load_json


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class WorkbenchState:
    path: Path
    data: dict[str, Any]

    @classmethod
    def open(cls, path: str | Path = "state/workbench-state.json") -> "WorkbenchState":
        p = Path(path)
        data = load_json(
            p,
            {
                "drafts": [],
                "reuse_decisions": [],
                "approvals": [],
            },
        )
        data.setdefault("drafts", [])
        data.setdefault("reuse_decisions", [])
        data.setdefault("approvals", [])
        return cls(path=p, data=data)

    def save(self) -> None:
        atomic_write_json(self.path, self.data)

    def add_draft(
        self,
        *,
        release: str,
        key: str,
        message: str,
        locale: str = "zh-Hans",
        source_hint: str | None = None,
        reviewer: str | None = None,
    ) -> dict[str, Any]:
        draft = {
            "id": "draft-" + uuid.uuid4().hex[:8],
            "release": release,
            "key": key,
            "locale": locale,
            "source_hint": source_hint,
            "message": message,
            "meaning": "pending",
            "display": "pending",
            "created_by": reviewer,
            "created_at": _now(),
        }
        self.data["drafts"].append(draft)
        self.save()
        return draft

    def reuse_decision(
        self,
        *,
        target_release: str,
        target_key: str,
        locale: str,
        candidate_id: str,
        decision: str,
        scope: str = "usage",
        reviewer: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        if decision not in {"approved", "forbidden", "needs_review"}:
            raise ValueError("decision 必须是 approved、forbidden 或 needs_review")
        if scope not in {"usage", "translation"}:
            raise ValueError("scope 必须是 usage 或 translation")
        record = {
            "target_release": target_release if scope == "usage" else "*",
            "target_key": target_key if scope == "usage" else "*",
            "locale": locale,
            "candidate_id": candidate_id,
            "decision": decision,
            "scope": scope,
            "reviewer": reviewer,
            "note": note,
            "updated_at": _now(),
        }
        # 同一目标/候选/范围只保留最新决定。
        self.data["reuse_decisions"] = [
            old
            for old in self.data["reuse_decisions"]
            if not (
                old.get("target_release") == record["target_release"]
                and old.get("target_key") == record["target_key"]
                and old.get("locale") == locale
                and old.get("candidate_id") == candidate_id
                and old.get("scope") == scope
            )
        ]
        self.data["reuse_decisions"].append(record)
        self.save()
        return record

    def get_reuse_decision(self, target: tuple[str, str, str], candidate_id: str) -> dict[str, Any] | None:
        release, key, locale = target
        usage_match: dict[str, Any] | None = None
        global_match: dict[str, Any] | None = None
        for row in self.data["reuse_decisions"]:
            if row.get("candidate_id") != candidate_id or row.get("locale") != locale:
                continue
            if (
                row.get("scope") == "usage"
                and row.get("target_release") == release
                and row.get("target_key") == key
            ):
                usage_match = row
            if row.get("scope") == "translation":
                global_match = row
        # 具体用法的决定优先于全局禁止/核准。
        return usage_match or global_match

    def set_approval(
        self,
        *,
        candidate_id: str,
        kind: str,
        status: str,
        basis: dict[str, str] | None = None,
        reviewer: str | None = None,
        note: str | None = None,
        target: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if kind not in {"meaning", "display"}:
            raise ValueError("kind 必须是 meaning 或 display")
        if status not in {"approved", "pending", "rejected"}:
            raise ValueError("status 必须是 approved、pending 或 rejected")
        record = {
            "candidate_id": candidate_id,
            "kind": kind,
            "status": status,
            "basis": basis or {},
            "reviewer": reviewer,
            "note": note,
            "target": target or {},
            "updated_at": _now(),
        }
        self.data["approvals"] = [
            old
            for old in self.data["approvals"]
            if not (old.get("candidate_id") == candidate_id and old.get("kind") == kind)
        ]
        self.data["approvals"].append(record)
        self.save()
        return record

    def approval_override(self, candidate_id: str, kind: str) -> dict[str, Any] | None:
        for row in self.data["approvals"]:
            if row.get("candidate_id") == candidate_id and row.get("kind") == kind:
                return row
        return None
