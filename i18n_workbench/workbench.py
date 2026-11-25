"""对外门面：加载资料/状态，组织门禁、复用、变动影响和出包。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .approvals import analyze_change, apply_change_to_row, basis_hashes
from .packager import build_pack, effective_approvals, effective_submissions, select_for_row, verify_pack
from .repository import load_data, load_json
from .reuse import build_reuse_report
from .state import WorkbenchState
from .validation import validate_candidate


class Workbench:
    def __init__(
        self,
        root: str | Path = ".",
        *,
        data_dir: str | Path | None = None,
        config_path: str | Path | None = None,
        state: WorkbenchState | None = None,
    ) -> None:
        self.root = Path(root)
        self.data_dir = Path(data_dir) if data_dir is not None else self.root / "data"
        self.config_path = Path(config_path) if config_path is not None else self.root / "workbench.config.json"
        self.data = load_data(self.data_dir)
        self.config = load_json(self.config_path, {})
        state_path = self.root / self.config.get("state_file", "state/workbench-state.json")
        self.state = state or WorkbenchState.open(state_path)
        self.rows_by_identity = {(str(r["release"]), str(r["key"])): r for r in self.data.catalogue}

    def submissions(self) -> list[dict[str, Any]]:
        return effective_submissions(self.data, self.state)

    def gate(
        self,
        *,
        release: str | None = None,
        locale: str | None = None,
        include_approvals: bool = True,
    ) -> dict[str, Any]:
        locale = locale or self.config.get("locale", "zh-Hans")
        findings: list[dict[str, Any]] = []
        checked = 0
        for candidate in self.submissions():
            if release and candidate.get("release") != release:
                continue
            if candidate.get("locale") != locale:
                continue
            row = self.rows_by_identity.get((str(candidate.get("release")), str(candidate.get("key"))))
            if row is None:
                findings.append(
                    {
                        "severity": "error",
                        "code": "orphan_submission",
                        "release": candidate.get("release"),
                        "key": candidate.get("key"),
                        "locale": candidate.get("locale"),
                        "candidate_id": candidate.get("id"),
                        "message": "译稿引用的 release/key 不在目录中",
                    }
                )
                continue
            checked += 1
            candidate_findings = validate_candidate(
                row,
                str(candidate["message"]),
                locale=locale,
                glossary_rows=self.data.glossary,
                candidate_id=str(candidate.get("id")),
                catalog=self.config.get("catalog"),
            )
            findings.extend(candidate_findings)

            approvals = effective_approvals(
                candidate,
                row,
                locale=locale,
                state=self.state,
                catalog=self.config.get("catalog"),
            )
            if include_approvals:
                for kind, label in (("meaning", "句意"), ("display", "目录可用性")):
                    status = approvals[kind]
                    if status != "approved":
                        findings.append(
                            {
                                "severity": "error",
                                "code": f"{kind}_not_approved",
                                "release": row["release"],
                                "key": row["key"],
                                "locale": locale,
                                "candidate_id": candidate.get("id"),
                                "usage": row.get("usage"),
                                "around": row.get("around"),
                                "location": "approval",
                                "approval_kind": kind,
                                "approval_status": status,
                                "message": f"{label}核准状态为 {status}，不能作为已核准译文发布",
                            }
                        )

        errors = [f for f in findings if f.get("severity") == "error"]
        warnings = [f for f in findings if f.get("severity") == "warning"]
        return {
            "ok": not errors,
            "checked_candidates": checked,
            "error_count": len(errors),
            "warning_count": len(warnings),
            "errors": errors,
            "warnings": warnings,
            "findings": findings,
        }

    def reuse_report(self, *, locale: str | None = None) -> dict[str, Any]:
        locale = locale or self.config.get("locale", "zh-Hans")
        report = build_reuse_report(
            self.data.catalogue,
            self.submissions(),
            locale=locale,
            glossary_rows=self.data.glossary,
            decision_resolver=self.state.get_reuse_decision,
        )
        report["lineage"] = self.lineage()
        return report

    def change_impact(self) -> list[dict[str, Any]]:
        return [analyze_change(change, self.rows_by_identity) for change in self.data.changes]

    def lineage(self) -> list[dict[str, Any]]:
        links: list[dict[str, Any]] = []
        for impact in self.change_impact():
            links.extend(impact.get("lineage", []))
        return links

    def preview_change_selection(self, change_id: str, *, locale: str | None = None) -> dict[str, Any]:
        """在不改动冻结目录的前提下，预览应用某条 changes 后的选句结果。"""

        locale = locale or self.config.get("locale", "zh-Hans")
        change = next((item for item in self.data.changes if item["id"] == change_id), None)
        if change is None:
            raise KeyError(f"找不到目录变动 {change_id}")
        row = self.rows_by_identity[(str(change["release"]), str(change["key"]))]
        after_row = apply_change_to_row(row, change)
        selection = select_for_row(
            after_row,
            submissions=self.submissions(),
            rows_by_identity=self.rows_by_identity,
            locale=locale,
            config=self.config,
            glossary_rows=self.data.glossary,
            state=self.state,
            catalog=self.config.get("catalog"),
        )
        selection["impact"] = analyze_change(change, self.rows_by_identity)
        return selection

    def submit(
        self,
        *,
        release: str,
        key: str,
        message: str,
        locale: str | None = None,
        source_hint: str | None = None,
        reviewer: str | None = None,
    ) -> dict[str, Any]:
        if (release, key) not in self.rows_by_identity:
            raise KeyError(f"目录中不存在 {release}/{key}")
        return self.state.add_draft(
            release=release,
            key=key,
            message=message,
            locale=locale or self.config.get("locale", "zh-Hans"),
            source_hint=source_hint,
            reviewer=reviewer,
        )

    def approve(
        self,
        candidate_id: str,
        kind: str,
        status: str = "approved",
        *,
        reviewer: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        candidate = next((item for item in self.submissions() if str(item.get("id")) == candidate_id), None)
        if candidate is None:
            raise KeyError(f"找不到译稿 {candidate_id}")
        row = self.rows_by_identity[(str(candidate["release"]), str(candidate["key"]))]
        locale = str(candidate.get("locale") or self.config.get("locale", "zh-Hans"))
        hashes = basis_hashes(row, locale, str(candidate["message"]), self.config.get("catalog"))
        return self.state.set_approval(
            candidate_id=candidate_id,
            kind=kind,
            status=status,
            basis=hashes,
            reviewer=reviewer,
            note=note,
            target={"release": row["release"], "key": row["key"], "locale": locale},
        )

    def decide_reuse(
        self,
        *,
        target_release: str,
        target_key: str,
        candidate_id: str,
        decision: str,
        locale: str | None = None,
        scope: str = "usage",
        reviewer: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        return self.state.reuse_decision(
            target_release=target_release,
            target_key=target_key,
            locale=locale or self.config.get("locale", "zh-Hans"),
            candidate_id=candidate_id,
            decision=decision,
            scope=scope,
            reviewer=reviewer,
            note=note,
        )

    def forbid_reuse(
        self,
        *,
        target_release: str,
        target_key: str,
        candidate_id: str,
        locale: str | None = None,
        scope: str = "usage",
        reviewer: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """禁止某条候选在指定用法或所有其他用法中复用。"""

        return self.decide_reuse(
            target_release=target_release,
            target_key=target_key,
            candidate_id=candidate_id,
            decision="forbidden",
            locale=locale,
            scope=scope,
            reviewer=reviewer,
            note=note,
        )

    def approve_meaning(self, candidate_id: str, **kwargs: Any) -> dict[str, Any]:
        return self.approve(candidate_id, "meaning", **kwargs)

    def approve_display(self, candidate_id: str, **kwargs: Any) -> dict[str, Any]:
        return self.approve(candidate_id, "display", **kwargs)

    def build_pack(
        self,
        *,
        release: str,
        locale: str | None = None,
        catalog: str | None = None,
        output_dir: str | Path | None = None,
    ) -> dict[str, Any]:
        return build_pack(
            self,
            release=release,
            locale=locale or self.config.get("locale", "zh-Hans"),
            catalog=catalog,
            output_dir=output_dir,
        )

    def verify_pack(self, manifest_path: str | Path) -> dict[str, Any]:
        return verify_pack(manifest_path, self.data, state=self.state)
