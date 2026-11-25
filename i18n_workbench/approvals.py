"""核准状态、依据指纹和变动后的继承判断。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .contracts import contract_for_row, semantic_unit_key
from .icu import canonical_shape, iter_branches, parse_message
from .semantics import catalogue_frame


def _stable_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def meaning_basis(row: dict[str, Any]) -> dict[str, Any]:
    """句意依据：动作/对象、ICU 语义结构和参数语义角色。

    这里不使用参数的字面名字，因此已知的纯改名在语义角色和单位不变时，
    句意核准仍可能继承；目标目录可用性则使用更严格的显示依据。
    """

    contract = contract_for_row(row)
    param_semantics = sorted(
        (req.branch, req.role, semantic_unit_key(req.unit_aliases))
        for req in contract.requirements
    )
    return {
        "release": row.get("release"),
        "key": row.get("key"),
        "frame": catalogue_frame(row).to_dict(),
        "shape": canonical_shape(row["source"]),
        "parameter_semantics": param_semantics,
        # 拆句/合句简报无法由 shape 完全表达；存在 lineage 标记时必须破坏继承。
        "lineage": row.get("_lineage_after"),
    }


def display_basis(
    row: dict[str, Any],
    locale: str,
    message: str | None = None,
    catalog: str | None = None,
) -> dict[str, Any]:
    """目录可用性依据：精确参数名、required 契约、目标语言、catalog 和实际译文。"""

    return {
        "catalog": catalog,
        "release": row.get("release"),
        "key": row.get("key"),
        "locale": locale,
        "source": row.get("source"),
        "required": row.get("required"),
        "usage": row.get("usage"),
        "around": row.get("around"),
        "message": message,
        "revision": row.get("_revision"),
        "lineage": row.get("_lineage_after"),
    }


def basis_hashes(
    row: dict[str, Any],
    locale: str,
    message: str | None = None,
    catalog: str | None = None,
) -> dict[str, str]:
    return {
        "meaning": _stable_hash(meaning_basis(row)),
        "display": _stable_hash(display_basis(row, locale, message, catalog)),
    }


def apply_change_to_row(row: dict[str, Any], change: dict[str, Any]) -> dict[str, Any]:
    """根据 changes 记录构造变动后的假设目录行。"""

    updated = dict(row)
    field_name = change.get("field")
    before = change.get("before")
    after = change.get("after")

    if field_name == "placeholder":
        for key in ("source", "required"):
            value = updated.get(key)
            if isinstance(value, str) and before is not None and after is not None:
                updated[key] = value.replace(str(before), str(after))
            elif isinstance(value, dict):
                new_required: dict[str, Any] = {}
                for branch, names in value.items():
                    if isinstance(names, list):
                        new_required[branch] = [str(after) if name == before else name for name in names]
                    else:
                        new_required[branch] = after if names == before else names
                updated[key] = new_required
    elif field_name in {"usage", "around", "source", "required", "key"}:
        updated[field_name] = after
    elif field_name == "sentence":
        # 拆句/合句无法从一句简报安全生成新 ICU；保留 after 作为 lineage
        # 标记，并强制句意依据改变，要求译者按新分支复核。
        updated["_lineage_after"] = after
        updated["_lineage_change_id"] = change.get("id")
    else:
        updated[field_name] = after

    updated["_revision"] = change.get("revision")
    return updated


def _align_lineage_branches(after_row: dict[str, Any], links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把简报中的旧键粗略对齐到新 ICU 分支，并列出全部待复核分支。"""

    try:
        parsed = parse_message(str(after_row.get("source") or ""))
        branches = [view.path for view in iter_branches(parsed.nodes)]
    except Exception:  # noqa: BLE001
        branches = []

    for link in links:
        related = str(link.get("related_key") or "").lower()
        suffix = related.split(".")[-1]
        link["current_branches"] = branches
        if suffix in {"active", "saved", "selected", "on"}:
            link["suggested_branch"] = "select[saved]" if "select[saved]" in branches else (branches[0] if branches else None)
            link["alignment_confidence"] = "inferred-from-key-and-select-value"
        elif suffix in {"empty", "none", "blank", "inactive"}:
            link["suggested_branch"] = "select[other]" if "select[other]" in branches else (branches[-1] if branches else None)
            link["alignment_confidence"] = "inferred-fallback-branch"
        else:
            link["suggested_branch"] = None
            link["alignment_confidence"] = "needs-human-alignment"
    return links


def analyze_change(change: dict[str, Any], rows_by_identity: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    """判断一条目录变动是否允许旧核准分别继承句意和目录可用性。"""

    identity = (str(change.get("release")), str(change.get("key")))
    row = rows_by_identity.get(identity)
    result: dict[str, Any] = {
        "change_id": change.get("id"),
        "release": identity[0],
        "key": identity[1],
        "field": change.get("field"),
        "previous": change.get("previous"),
        "revision": change.get("revision"),
        "found": row is not None,
        "meaning_inheritable": False,
        "display_inheritable": False,
        "reasons": [],
        "lineage": [],
    }
    if row is None:
        result["reasons"].append("当前目录中找不到该 release/key，可能是已删除或尚未合入的键")
        return result

    after_row = apply_change_to_row(row, change)
    old_meaning = _stable_hash(meaning_basis(row))
    new_meaning = _stable_hash(meaning_basis(after_row))
    old_display = _stable_hash(display_basis(row, "zh-Hans"))
    new_display = _stable_hash(display_basis(after_row, "zh-Hans"))

    result["meaning_inheritable"] = old_meaning == new_meaning and change.get("field") != "sentence"
    # 显示依据包含精确参数名/usage/required，因此任何已建模变动都需要重新做
    # 目录可用性核准。纯改名时句意仍可继承。
    result["display_inheritable"] = False
    result["old_meaning_basis"] = old_meaning
    result["new_meaning_basis"] = new_meaning
    result["old_display_basis"] = old_display
    result["new_display_basis"] = new_display

    if change.get("field") == "placeholder":
        result["reasons"].append("参数改名后，旧消息中的命名占位符无法满足新目录契约；需更新译文并重新核准目录可用性")
        if result["meaning_inheritable"]:
            result["reasons"].append("参数语义角色和单位未变，句意核准可继承")
    elif change.get("field") == "usage":
        old_frame = catalogue_frame(row).to_dict()
        new_frame = catalogue_frame(after_row).to_dict()
        result["old_frame"] = old_frame
        result["new_frame"] = new_frame
        if old_frame != new_frame:
            result["reasons"].append("界面动作对象/使用场景改变，旧句意核准不能继承")
        else:
            result["reasons"].append("usage 文本改变但语义框未变；仍需重新确认实际界面位置")
    elif change.get("field") == "sentence":
        lineage = _align_lineage_branches(after_row, extract_lineage(change))
        result["lineage"] = lineage
        result["reasons"].append("拆句/合句改变了消息边界，旧核准必须按新分支和具体界面位置复核")
    else:
        result["reasons"].append("目录契约发生变化，需要重新核准")

    return result


def extract_lineage(change: dict[str, Any]) -> list[dict[str, Any]]:
    """从 changes 简报中定位拆句/合句涉及的键和分支。"""

    import re

    text = f"{change.get('before') or ''} {change.get('after') or ''}"
    keys = sorted(set(re.findall(r"[A-Za-z_][\w-]*(?:\.[A-Za-z0-9_-]+)+", text)))
    direction = "merged" if any(word in text for word in ("合并", "一个", "两条", "merge")) else "split" if any(word in text for word in ("拆", "split")) else "changed"
    links: list[dict[str, Any]] = []
    for key in keys:
        links.append(
            {
                "change_id": change.get("id"),
                "release": change.get("release"),
                "revision": change.get("revision"),
                "direction": direction,
                "related_key": key,
                "current_key": change.get("key"),
                "location": f"{change.get('release')}/{change.get('key')}@{change.get('revision')}",
                "status": "needs_branch_alignment",
            }
        )
    if not links:
        links.append(
            {
                "change_id": change.get("id"),
                "release": change.get("release"),
                "revision": change.get("revision"),
                "direction": direction,
                "related_key": None,
                "current_key": change.get("key"),
                "location": f"{change.get('release')}/{change.get('key')}@{change.get('revision')}",
                "status": "needs_branch_alignment",
            }
        )
    return links
