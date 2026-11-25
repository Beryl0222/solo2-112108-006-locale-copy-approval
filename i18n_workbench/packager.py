"""按发行版冻结契约选句、回退并生成/校验成品语言包。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .approvals import basis_hashes
from .repository import atomic_write_json
from .reuse import find_candidates_for_usage
from .semantics import match_glossary_usage
from .validation import error_findings, validate_candidate


PACK_FORMAT = "desktop-i18n-pack/1"


def effective_submissions(data: Any, state: Any | None = None) -> list[dict[str, Any]]:
    rows = [dict(row) for row in data.submissions]
    if state is not None:
        rows.extend(dict(row) for row in state.data.get("drafts", []))
    return rows


def effective_approvals(
    candidate: dict[str, Any],
    row: dict[str, Any] | None,
    *,
    locale: str,
    state: Any | None = None,
    catalog: str | None = None,
) -> dict[str, Any]:
    result = {
        "meaning": candidate.get("meaning", "pending"),
        "display": candidate.get("display", "pending"),
        "meaning_basis_current": None,
        "display_basis_current": None,
    }
    if row is not None:
        hashes = basis_hashes(row, locale, str(candidate.get("message", "")), catalog)
        result["meaning_basis_current"] = hashes["meaning"]
        result["display_basis_current"] = hashes["display"]

    if state is None:
        return result

    for kind in ("meaning", "display"):
        override = state.approval_override(str(candidate.get("id")), kind)
        if not override:
            continue
        status = str(override.get("status", "pending"))
        if status == "approved" and override.get("basis") and row is not None:
            current = result[f"{kind}_basis_current"]
            stored = override["basis"].get(kind)
            if stored and current and stored != current:
                status = "stale"
        result[kind] = status
        result[f"{kind}_reviewer"] = override.get("reviewer")
        result[f"{kind}_note"] = override.get("note")
    return result


def scenario_policy(row: dict[str, Any], glossary_rows: list[dict[str, Any]]) -> dict[str, Any]:
    matched = [
        term
        for term in glossary_rows
        if term.get("release") == row.get("release")
        and match_glossary_usage(str(row.get("usage") or ""), str(term.get("usage") or ""))
    ]
    permits: set[str] = set()
    for term in matched:
        permits.update(term.get("permit_fallback") or [])
    return {"glossary": matched, "permits": sorted(permits)}


def allowed_levels(row: dict[str, Any], config: dict[str, Any], glossary_rows: list[dict[str, Any]]) -> list[str]:
    release_config = (config.get("releases") or {}).get(str(row.get("release")), {})
    ladder = release_config.get("fallback_ladder") or config.get("default_fallback_ladder") or []
    policy = scenario_policy(row, glossary_rows)
    permits = set(policy["permits"])
    result: list[str] = []
    for level in ladder:
        if level in {"exact-approved", "usage-approved"}:
            result.append(level)
        elif not permits:
            # 没有术语/场景授权时，不自动做跨句复用；source 是否允许由
            # 发行版层级决定（梯子中没有 source 即不会出现）。
            if level == "source":
                result.append(level)
        elif level in permits:
            result.append(level)
    return result


def _smoke_contexts(row: dict[str, Any], message: str, locale: str) -> list[dict[str, Any]]:
    from .contracts import contract_for_row, trigger_params
    from .icu import render

    contract = contract_for_row(row)
    branches = []
    for req in contract.requirements:
        if req.branch not in branches:
            branches.append(req.branch)
    if not branches:
        branches = [None]
    contexts = []
    for branch in branches:
        params = trigger_params(contract, branch)
        contexts.append(
            {
                "source_branch": branch,
                "params": params,
                "rendered": render(message, params, locale=locale, strict=True),
            }
        )
    return contexts


def select_for_row(
    row: dict[str, Any],
    *,
    submissions: list[dict[str, Any]],
    rows_by_identity: dict[tuple[str, str], dict[str, Any]],
    locale: str,
    config: dict[str, Any],
    glossary_rows: list[dict[str, Any]],
    state: Any | None = None,
    catalog: str | None = None,
) -> dict[str, Any]:
    target = (str(row["release"]), str(row["key"]), locale)
    candidates = find_candidates_for_usage(
        row,
        submissions,
        rows_by_identity,
        locale=locale,
        glossary_rows=glossary_rows,
        decision_resolver=(state.get_reuse_decision if state is not None else None),
    )
    by_id = {str(candidate.get("id")): candidate for candidate in submissions}
    levels = allowed_levels(row, config, glossary_rows)
    rejected: list[dict[str, Any]] = []

    enriched = []
    for suggestion in candidates:
        candidate = by_id.get(str(suggestion["candidate_id"]))
        if candidate is None:
            continue
        # 核准针对志愿者投稿所引用的原始 release/key；目标用法是否兼容，
        # 由 suggestion 的渲染/语义校验和复用决定负责。
        candidate_row = rows_by_identity.get((str(candidate.get("release")), str(candidate.get("key"))))
        approvals = effective_approvals(candidate, candidate_row, locale=locale, state=state, catalog=catalog)
        item = {"suggestion": suggestion, "candidate": candidate, "approvals": approvals}
        enriched.append(item)
        if suggestion["errors"] or approvals["meaning"] != "approved" or approvals["display"] != "approved":
            rejected.append(
                {
                    "candidate_id": candidate.get("id"),
                    "relation": suggestion["relation"],
                    "meaning": approvals["meaning"],
                    "display": approvals["display"],
                    "errors": suggestion["errors"],
                }
            )

    chosen: dict[str, Any] | None = None
    chosen_level: str | None = None

    def take(item: dict[str, Any], level: str) -> None:
        nonlocal chosen, chosen_level
        suggestion = item["suggestion"]
        if not suggestion["technically_usable"] or suggestion["forbidden"]:
            return
        if item["approvals"]["meaning"] != "approved" or item["approvals"]["display"] != "approved":
            return
        try:
            smoke = _smoke_contexts(row, item["candidate"]["message"], locale)
        except Exception as error:  # noqa: BLE001
            return
        chosen = {
            "value": item["candidate"]["message"],
            "origin": level,
            "fallback_level": level,
            "candidate_id": item["candidate"].get("id"),
            "source_release": item["candidate"].get("release"),
            "source_key": item["candidate"].get("key"),
            "approvals": item["approvals"],
            "basis": basis_hashes(row, locale, item["candidate"]["message"], catalog),
            "smoke_contexts": smoke,
        }
        chosen_level = level

    for level in levels:
        if chosen is not None:
            break
        for item in enriched:
            suggestion = item["suggestion"]
            if level == "exact-approved" and suggestion["direct"]:
                take(item, level)
            elif level == "usage-approved" and suggestion.get("usage_approved"):
                take(item, level)
            elif level == "approved-same-context" and not suggestion["direct"] and suggestion["relation"] == "approved_same_usage":
                take(item, level)
            if chosen is not None:
                break

    if chosen is None and "source" in levels:
        source_message = str(row["source"])
        chosen = {
            "value": source_message,
            "origin": "source",
            "fallback_level": "source",
            "candidate_id": None,
            "basis": basis_hashes(row, locale, source_message, catalog),
            "smoke_contexts": _smoke_contexts(row, source_message, "en"),
        }
        chosen_level = "source"

    return {
        "target": {"release": target[0], "key": target[1], "locale": locale, "usage": row.get("usage"), "around": row.get("around")},
        "levels": levels,
        "chosen": chosen,
        "rejected": rejected,
        "candidates": candidates,
        "policy": scenario_policy(row, glossary_rows),
    }


def build_pack(
    wb: Any,
    *,
    release: str,
    locale: str = "zh-Hans",
    catalog: str | None = None,
    output_dir: str | Path | None = None,
) -> dict[str, Any]:
    catalog = catalog or wb.config.get("catalog", "desktop")
    configured_output = wb.config.get("output_dir", "dist")
    output_root = Path(output_dir) if output_dir is not None else wb.root / configured_output
    rows = [row for row in wb.data.catalogue if row.get("release") == release]
    rows_by_identity = {(str(row["release"]), str(row["key"])): row for row in wb.data.catalogue}
    submissions = effective_submissions(wb.data, wb.state)

    entries: dict[str, Any] = {}
    pack: dict[str, str] = {}
    unresolved: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for row in rows:
        selection = select_for_row(
            row,
            submissions=submissions,
            rows_by_identity=rows_by_identity,
            locale=locale,
            config=wb.config,
            glossary_rows=wb.data.glossary,
            state=wb.state,
            catalog=catalog,
        )
        key = str(row["key"])
        if selection["chosen"] is None:
            unresolved.append({**selection["target"], "levels": selection["levels"], "rejected": selection["rejected"]})
            continue
        chosen = selection["chosen"]
        pack[key] = chosen["value"]
        entries[key] = {
            **selection["target"],
            "catalog": catalog,
            "value": chosen["value"],
            "origin": chosen["origin"],
            "fallback_level": chosen["fallback_level"],
            "candidate_id": chosen.get("candidate_id"),
            "source_release": chosen.get("source_release"),
            "source_key": chosen.get("source_key"),
            "basis": chosen.get("basis"),
            "approvals": _jsonable_approvals(chosen.get("approvals")),
            "allowed_levels": selection["levels"],
            "render_samples": chosen.get("smoke_contexts"),
        }
        for item in selection["rejected"]:
            item = dict(item)
            item.update({"release": release, "key": key, "locale": locale})
            rejected.append(item)

    pack_dir = output_root / release / locale
    pack_dir.mkdir(parents=True, exist_ok=True)
    pack_path = pack_dir / f"{catalog}.json"
    manifest_path = pack_dir / f"{catalog}.manifest.json"
    pack_bytes = (json.dumps(pack, ensure_ascii=False, indent=2, sort_keys=False) + "\n").encode("utf-8")
    pack_path.write_bytes(pack_bytes)
    sha = hashlib.sha256(pack_bytes).hexdigest()

    release_config = (wb.config.get("releases") or {}).get(release, {})
    manifest = {
        "format": PACK_FORMAT,
        "catalog": catalog,
        "release": release,
        "locale": locale,
        "frozen": bool(release_config.get("frozen", False)),
        "contract_revision": release_config.get("contract_revision"),
        "pack_file": pack_path.name,
        "pack_sha256": sha,
        "entry_count": len(pack),
        "entries": entries,
        "unresolved": unresolved,
        "rejected": rejected,
    }
    atomic_write_json(manifest_path, manifest)
    return {"pack_path": str(pack_path), "manifest_path": str(manifest_path), "pack": pack, "manifest": manifest}


def _jsonable_approvals(approvals: dict[str, Any] | None) -> dict[str, Any]:
    if not approvals:
        return {}
    return {k: v for k, v in approvals.items() if not k.endswith("_basis_current")}


def verify_pack(
    manifest_path: str | Path,
    data: Any,
    *,
    state: Any | None = None,
) -> dict[str, Any]:
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    pack_path = manifest_path.parent / manifest["pack_file"]
    errors: list[str] = []
    warnings: list[str] = []

    raw = pack_path.read_bytes()
    actual_sha = hashlib.sha256(raw).hexdigest()
    if actual_sha != manifest.get("pack_sha256"):
        errors.append("语言包 SHA256 与清单不一致")
    pack = json.loads(raw.decode("utf-8"))
    if not isinstance(pack, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in pack.items()):
        errors.append("客户端语言包必须是键到字符串的 JSON 对象")

    rows_by_identity = {(str(row["release"]), str(row["key"])): row for row in data.catalogue}
    release = manifest["release"]
    locale = manifest["locale"]
    submissions = effective_submissions(data, state)
    submissions_by_id = {str(item.get("id")): item for item in submissions}

    if set(pack) != set(manifest.get("entries", {})):
        errors.append("语言包键集合与清单 entries 不一致")

    for key, entry in manifest.get("entries", {}).items():
        if pack.get(key) != entry.get("value", pack.get(key)) and "value" in entry:
            errors.append(f"{key}: 清单值与语言包不一致")
        row = rows_by_identity.get((release, key))
        if row is None:
            errors.append(f"{key}: 当前资料中找不到目录契约")
            continue
        message = pack.get(key, "")
        if entry.get("origin") != "source":
            findings = validate_candidate(row, message, locale=locale, glossary_rows=data.glossary)
            for finding in error_findings(findings):
                errors.append(f"{key} {finding.get('location')}: {finding['message']}")
            current_basis = basis_hashes(row, locale, message, manifest.get("catalog"))
            if entry.get("basis") != current_basis:
                errors.append(f"{key}: 核准依据哈希与当前目录/消息不一致，需重新核准")
            candidate_id = entry.get("candidate_id")
            if candidate_id and state is not None:
                candidate = submissions_by_id.get(str(candidate_id))
                if candidate is None:
                    errors.append(f"{key}: 找不到核准来源 {candidate_id}")
                else:
                    source_row = rows_by_identity.get(
                        (entry.get("source_release") or release, entry.get("source_key") or key)
                    )
                    approvals = effective_approvals(
                        candidate,
                        source_row,
                        locale=locale,
                        state=state,
                        catalog=manifest.get("catalog"),
                    )
                    for kind in ("meaning", "display"):
                        if approvals.get(kind) != "approved":
                            errors.append(f"{key}: {candidate_id} 的 {kind} 核准当前为 {approvals.get(kind)}")
        else:
            try:
                _smoke_contexts(row, message, locale)
            except Exception as error:  # noqa: BLE001
                errors.append(f"{key}: 源文回退无法渲染：{error}")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "manifest_path": str(manifest_path),
        "pack_path": str(pack_path),
    }
