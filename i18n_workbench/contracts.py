"""消息契约、分支必传参数和渲染样例。"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from .icu import (
    BranchView,
    ParsedMessage,
    direct_variables,
    iter_branches,
    parse_message,
    render,
)


UNIT_ALIASES: dict[str, tuple[str, ...]] = {
    "secs": ("秒", "秒钟", "s", "sec", "secs", "second", "seconds"),
    "seconds": ("秒", "秒钟", "s", "sec", "secs", "second", "seconds"),
    "wait": ("秒", "秒钟", "s", "sec", "secs", "second", "seconds"),
    "size": ("mb", "kb", "gb", "兆", "MB", "KB", "GB"),
    "hotkey": ("ctrl", "alt", "shift", "cmd", "⌘", "快捷键", "键位"),
}

SAMPLE_VALUES: dict[str, Any] = {
    "secs": 42,
    "seconds": 42,
    "wait": 42,
    "size": 12,
    "name": "月度筛选",
    "hotkey": "Ctrl+F",
    "query": "月报",
    "filename": "report.txt",
}


@dataclass(frozen=True)
class ParamRequirement:
    name: str
    branch: str
    role: str  # visible 或 selector_only
    unit_aliases: tuple[str, ...] = ()
    note: str = ""
    path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "branch": self.branch,
            "path": self.path,
            "role": self.role,
            "unit_aliases": list(self.unit_aliases),
            "note": self.note,
        }


@dataclass
class MessageContract:
    row: dict[str, Any]
    parsed: ParsedMessage
    requirements: list[ParamRequirement] = field(default_factory=list)

    @property
    def all_param_names(self) -> set[str]:
        return self.parsed.variables()

    def requirement(self, branch: str, name: str) -> ParamRequirement | None:
        for req in self.requirements:
            if req.branch == branch and req.name == name:
                return req
        return None


def _sections_from_prose(text_value: str) -> dict[str, str]:
    """把 ``one: ... other: ...`` 这类 required 说明按分支切开。"""

    matches = list(re.finditer(r"(?<![A-Za-z])(one|other|zero|two|few|many|saved|=\d+|[A-Za-z_][\w-]*)\s*[:：]", text_value))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        branch = match.group(1)
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text_value)
        sections[branch] = text_value[start:end].strip()
    if not sections and text_value.strip():
        sections["other"] = text_value.strip()
    return sections


def _unit_aliases_for(name: str, source: str, note: str) -> tuple[str, ...]:
    aliases = list(UNIT_ALIASES.get(name, ()))
    combined = f"{source}\n{note}"
    # 只读取紧跟在命名变量后的单位，避免把同一句中 secs 的 “s”
    # 错安到 count/files 上。
    adjacent = re.search(r"\{" + re.escape(name) + r"\}\s*([A-Za-z%℃°][A-Za-z0-9%℃°]*)", source)
    if adjacent:
        token = adjacent.group(1)
        if token.upper() in {"MB", "KB", "GB", "TB"} or token.lower() in {"s", "sec", "secs"}:
            aliases.append(token)
    if "秒" in combined:
        aliases.extend(["秒", "s"])
    if "MB" in combined.upper() and name == "size":
        aliases.append("MB")
    # 去重但保留顺序。
    seen: set[str] = set()
    result: list[str] = []
    for alias in aliases:
        key = alias.lower()
        if key not in seen:
            seen.add(key)
            result.append(alias)
    return tuple(result)


def contract_for_row(row: dict[str, Any]) -> MessageContract:
    parsed = parse_message(row["source"])
    required = row.get("required") or {}
    requirements: list[ParamRequirement] = []

    if isinstance(required, dict):
        for branch, names in required.items():
            for name in names:
                requirements.append(
                    ParamRequirement(
                        name=str(name),
                        branch=str(branch),
                        role="visible",
                        unit_aliases=_unit_aliases_for(str(name), str(row.get("source") or ""), ""),
                    )
                )
    elif isinstance(required, str) and required.strip():
        sections = _sections_from_prose(required)
        for branch, note in sections.items():
            for name in sorted(parsed.variables()):
                if not re.search(rf"(?<![A-Za-z_]){re.escape(name)}(?![A-Za-z0-9_])", note):
                    continue
                after = note.split(name, 1)[1][:24]
                selector_only = any(
                    phrase in after
                    for phrase in ("作为分支选择参数", "只用来选分支", "只用于选择", "仅用于选择", "用来选择分支")
                )
                requirements.append(
                    ParamRequirement(
                        name=name,
                        branch=branch,
                        role="selector_only" if selector_only else "visible",
                        unit_aliases=_unit_aliases_for(name, str(row.get("source") or ""), note),
                        note=note,
                    )
                )
    # ICU 结构本身也是契约的一部分：required 没逐字写到的当前分支变量，
    # 仍需在对应可达分支保留。
    source = str(row.get("source") or "")
    views = iter_branches(parsed.nodes)
    for view in views:
        for name in sorted(view.direct_variable_names):
            if any(req.name == name and (req.path == view.path or (not req.path and req.branch == view.key)) for req in requirements):
                continue
            requirements.append(
                ParamRequirement(
                    name=name,
                    branch=view.key,
                    path=view.path,
                    role="visible",
                    unit_aliases=_unit_aliases_for(name, source, ""),
                )
            )
    if not views:
        for name in sorted(direct_variables(parsed.nodes)):
            if any(req.name == name for req in requirements):
                continue
            requirements.append(
                ParamRequirement(
                    name=name,
                    branch="other",
                    role="visible",
                    unit_aliases=_unit_aliases_for(name, source, ""),
                )
            )

    return MessageContract(row=row, parsed=parsed, requirements=requirements)


def _source_branch_text(parsed: ParsedMessage, branch_key: str) -> str:
    for view in iter_branches(parsed.nodes):
        if view.key == branch_key:
            return render(parsed.source, _all_sample_values(parsed), strict=False)
    return parsed.source


def _all_sample_values(parsed: ParsedMessage, branch_key: str | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for name in parsed.variables():
        params[name] = sample_value(name, branch_key)
    return params


def sample_value(name: str, branch_key: str | None = None) -> Any:
    lower = name.lower()
    if lower in {"count", "files", "items", "n", "num", "number", "total"}:
        if branch_key and (branch_key == "one" or branch_key.endswith(".one")):
            return 1
        if branch_key and branch_key.startswith("="):
            try:
                return int(branch_key[1:])
            except ValueError:
                return 0
        return 3
    if lower == "state":
        return branch_key if branch_key not in {None, "other"} else "unmatched-state"
    if lower in SAMPLE_VALUES:
        return SAMPLE_VALUES[lower]
    return "示例值"


def trigger_params(contract: MessageContract, source_branch: str | None = None) -> dict[str, Any]:
    """构造触发指定源分支的样例参数，支持嵌套分支路径。"""

    params: dict[str, Any] = {}
    branch_key = source_branch
    view: BranchView | None = None
    if source_branch:
        for candidate in iter_branches(contract.parsed.nodes):
            if candidate.path == source_branch:
                view = candidate
                branch_key = candidate.key
                break
        if view is None:
            for candidate in iter_branches(contract.parsed.nodes):
                if candidate.key == source_branch or candidate.path.endswith(f"[{source_branch}]"):
                    view = candidate
                    branch_key = candidate.key
                    break
    for name in contract.all_param_names:
        params[name] = sample_value(name, branch_key)
    if view is not None:
        _apply_branch_path(contract.parsed.nodes, view.path, params)
    return params


def params_for_target_path(parsed: ParsedMessage, path: str) -> dict[str, Any]:
    """构造让 *目标译文* 指定分支实际可达的样例参数。"""

    params = {name: sample_value(name) for name in parsed.variables()}
    _apply_branch_path(parsed.nodes, path, params)
    return params


def _apply_branch_path(nodes: tuple[Any, ...] | list[Any], path: str, params: dict[str, Any]) -> None:
    from .icu import Plural, Select

    segments = re.findall(r"(plural|select)\[([^\]]+)\]", path)

    def walk(items: tuple[Any, ...] | list[Any], index: int) -> bool:
        if index >= len(segments):
            return True
        kind, key = segments[index]
        for node in items:
            if kind == "plural" and isinstance(node, Plural) and key in node.branches:
                if key == "one":
                    params[node.name] = 1
                elif key == "other":
                    params[node.name] = 3
                elif key.startswith("="):
                    try:
                        params[node.name] = int(key[1:])
                    except ValueError:
                        params[node.name] = 0
                else:
                    params[node.name] = 3
                return walk(node.branches[key], index + 1)
            if kind == "select" and isinstance(node, Select) and key in node.branches:
                params[node.name] = "__unmatched__" if key == "other" else key
                return walk(node.branches[key], index + 1)
            if isinstance(node, (Plural, Select)):
                for branch_nodes in node.branches.values():
                    if walk(branch_nodes, index):
                        return True
        return False

    walk(tuple(nodes), 0)


def selected_branch_paths(
    nodes: list[Any] | tuple[Any, ...],
    params: dict[str, Any],
    *,
    locale: str,
    prefix: str = "",
) -> list[str]:
    """返回给定参数下实际选中的 ICU 分支路径。"""

    # 延迟导入避免循环。
    from .icu import Plural, Select, plural_category

    paths: list[str] = []
    for node in nodes:
        if isinstance(node, Plural):
            value = params.get(node.name, 3)
            exact = f"={value}"
            key = exact if exact in node.branches else (
                plural_category(value, locale) if plural_category(value, locale) in node.branches else "other"
            )
            path = f"{prefix}.plural[{key}]" if prefix else f"plural[{key}]"
            paths.append(path)
            paths.extend(
                selected_branch_paths(node.branches[key], params, locale=locale, prefix=path)
            )
        elif isinstance(node, Select):
            value = params.get(node.name)
            key = str(value) if value is not None and str(value) in node.branches else "other"
            path = f"{prefix}.select[{key}]" if prefix else f"select[{key}]"
            paths.append(path)
            paths.extend(
                selected_branch_paths(node.branches[key], params, locale=locale, prefix=path)
            )
    return paths


def semantic_unit_key(aliases: tuple[str, ...]) -> str:
    lowered = {alias.lower() for alias in aliases}
    if "秒" in aliases or {"secs", "seconds"} & lowered:
        return "time_seconds"
    if "mb" in lowered or "kb" in lowered or "gb" in lowered:
        return "data_size"
    if "ctrl" in lowered or "hotkey" in lowered:
        return "hotkey"
    return "generic"
