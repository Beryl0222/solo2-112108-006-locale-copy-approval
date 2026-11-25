"""门禁：校验实际可达、实际会渲染的消息。"""

from __future__ import annotations

from typing import Any, Iterable

from .contracts import (
    MessageContract,
    contract_for_row,
    params_for_target_path,
    selected_branch_paths,
    trigger_params,
)
from .icu import ICUError, iter_branches, parse_message, render, render_nodes
from .semantics import (
    catalogue_frame,
    frame_conflicts,
    select_glossary,
    translation_frame,
)


def _alias_present(rendered: str, aliases: tuple[str, ...]) -> bool:
    import re

    for alias in aliases:
        if re.search(r"[A-Za-z0-9]", alias):
            if re.search(r"(?<![A-Za-z0-9])" + re.escape(alias) + r"(?![A-Za-z0-9])", rendered, re.IGNORECASE):
                return True
        elif alias in rendered:
            return True
    return False


def _base_location(row: dict[str, Any], locale: str, candidate_id: str | None, catalog: str | None) -> dict[str, Any]:
    return {
        "release": row.get("release"),
        "key": row.get("key"),
        "locale": locale,
        "catalog": catalog,
        "candidate_id": candidate_id,
        "usage": row.get("usage"),
        "around": row.get("around"),
    }


def validate_candidate(
    row: dict[str, Any],
    message: str,
    *,
    locale: str = "zh-Hans",
    glossary_rows: Iterable[dict[str, Any]] | None = None,
    candidate_id: str | None = None,
    catalog: str | None = None,
) -> list[dict[str, Any]]:
    """校验一条候选译文在该目录用法下的所有可达分支。"""

    base = _base_location(row, locale, candidate_id, catalog)
    findings: list[dict[str, Any]] = []

    def add(code: str, severity: str, detail: str, **extra: Any) -> None:
        finding = dict(base)
        finding.update({"code": code, "severity": severity, "message": detail})
        finding.update(extra)
        findings.append(finding)

    try:
        target = parse_message(message)
    except ICUError as error:
        add("icu_syntax", "error", f"ICU 语法无效：{error}", location="message")
        return findings

    try:
        contract = contract_for_row(row)
    except ICUError as error:
        add("source_icu_syntax", "error", f"源目录 ICU 无效：{error}", location="source")
        return findings

    # 引用当前契约中不存在的参数，通常是参数改名后仍沿用旧译稿。
    unknown = sorted(target.variables() - contract.all_param_names)
    if unknown:
        params = trigger_params(contract)
        add(
            "unknown_parameter",
            "error",
            "译文引用了当前消息契约不存在的参数：" + ", ".join(unknown),
            location="message",
            parameters=unknown,
            render_context=_render_context(contract, message, params, locale, target_paths=[]),
        )

    views = {view.path: view for view in iter_branches(target.nodes)}
    reported: set[tuple[str, str, str]] = set()

    for req in contract.requirements:
        trigger = req.path or req.branch
        params = trigger_params(contract, trigger)
        paths = selected_branch_paths(target.nodes, params, locale=locale)
        source_paths = selected_branch_paths(contract.parsed.nodes, params, locale="en")
        if req.path and req.path in source_paths:
            source_path = req.path
        else:
            source_path = next((path for path in source_paths if f"[{req.branch}]" in path), source_paths[0] if source_paths else "")
        source_depth = source_path.count("[")
        same_depth_paths = [path for path in paths if path.count("[") == source_depth]
        target_path = same_depth_paths[-1] if same_depth_paths else (paths[-1] if paths else "")
        view = views.get(target_path)

        # 报告落在目标分支上，因此样例参数也按“触发该目标分支”选择，
        # 例如中文 other 用 3 而不是沿用英文 one 的 1。
        context_params = dict(params)
        if view is not None and view.kind == "plural" and view.selector:
            if view.key == "one":
                context_params[view.selector] = 1
            elif view.key == "other":
                context_params[view.selector] = 3
            elif view.key.startswith("="):
                try:
                    context_params[view.selector] = int(view.key[1:])
                except ValueError:
                    pass
        elif view is not None and view.kind == "select" and view.selector and view.key != "other":
            context_params[view.selector] = view.key

        represented = False
        if view is not None:
            represented = (
                req.name in view.direct_variable_names
                or (view.selector == req.name and view.direct_hash)
                # =0 这类精确分支名本身已经表达复数值，允许用“暂无”等说法
                # 代替 #，但外层 Plural 仍引用该选择参数。
                or (view.selector == req.name and view.key.startswith("="))
            )
            branch_rendered = render_nodes(view.nodes, context_params, locale=locale, strict=False, highlight_missing=True)
        else:
            represented = req.name in target.variables()
            branch_rendered = render(message, context_params, locale=locale, strict=False, highlight_missing=True)

        context = _render_context(
            contract,
            message,
            context_params,
            locale,
            target_paths=paths,
            source_path=source_path,
            target_path=target_path,
        )
        context["rendered_branch"] = branch_rendered

        if req.role == "selector_only":
            # 选择参数不要求逐字出现；分支能被选中即满足。
            continue

        dedupe_key = ("missing_parameter", target_path or "root", req.name)
        if not represented and dedupe_key not in reported:
            reported.add(dedupe_key)
            add(
                "missing_parameter",
                "error",
                f"可达分支 {target_path or '正文'} 必须保留参数 {req.name}"
                + (f"（{req.note}）" if req.note else ""),
                location=target_path or "message",
                branch=target_path,
                parameter=req.name,
                requirement=req.to_dict(),
                render_context=context,
            )

        if represented and req.unit_aliases and not _alias_present(branch_rendered, req.unit_aliases):
            unit_key = ("missing_unit", target_path or "root", req.name)
            if unit_key not in reported:
                reported.add(unit_key)
                add(
                    "missing_unit",
                    "error",
                    f"参数 {req.name} 的数值缺少对应单位/语义标记（可接受：{'、'.join(req.unit_aliases[:4])}）",
                    location=target_path or "message",
                    branch=target_path,
                    parameter=req.name,
                    expected_units=list(req.unit_aliases),
                    requirement=req.to_dict(),
                    render_context=context,
                )

    # 目标语言可能新增 source 没有的可达分支（例如中文的 =0）。这些分支
    # 也必须满足对应 other/合并类别的参数和单位要求。
    source_views = iter_branches(contract.parsed.nodes)

    def requirement_depth(req: Any) -> int:
        if req.path:
            return req.path.count("[")
        source_view = next((view for view in source_views if view.key == req.branch), None)
        return source_view.path.count("[") if source_view else 0

    for view in views.values():
        target_params = params_for_target_path(target, view.path)
        selected_here = selected_branch_paths(target.nodes, target_params, locale=locale)
        if view.path not in selected_here:
            continue
        branch_rendered = render_nodes(view.nodes, target_params, locale=locale, strict=False, highlight_missing=True)
        for req in contract.requirements:
            if req.role == "selector_only" or requirement_depth(req) != view.path.count("["):
                continue
            applies = req.branch == view.key
            if view.kind == "plural" and view.key == "other" and req.branch in {"one", "other", "zero", "two", "few", "many"}:
                applies = True
            if view.kind == "plural" and view.key.startswith("=") and req.branch == "other":
                applies = True
            if not applies:
                continue
            represented = (
                req.name in view.direct_variable_names
                or (view.selector == req.name and view.direct_hash)
                or (view.selector == req.name and view.key.startswith("="))
            )
            context = _render_context(
                contract,
                message,
                target_params,
                locale,
                target_paths=selected_here,
                target_path=view.path,
            )
            context["rendered_branch"] = branch_rendered
            dedupe_key = ("missing_parameter", view.path, req.name)
            if not represented and dedupe_key not in reported:
                reported.add(dedupe_key)
                add(
                    "missing_parameter",
                    "error",
                    f"可达分支 {view.path} 必须保留参数 {req.name}"
                    + (f"（{req.note}）" if req.note else ""),
                    location=view.path,
                    branch=view.path,
                    parameter=req.name,
                    requirement=req.to_dict(),
                    render_context=context,
                )
            if represented and req.unit_aliases and not _alias_present(branch_rendered, req.unit_aliases):
                unit_key = ("missing_unit", view.path, req.name)
                if unit_key not in reported:
                    reported.add(unit_key)
                    add(
                        "missing_unit",
                        "error",
                        f"分支 {view.path} 的参数 {req.name} 缺少单位/语义标记（可接受：{'、'.join(req.unit_aliases[:4])}）",
                        location=view.path,
                        branch=view.path,
                        parameter=req.name,
                        expected_units=list(req.unit_aliases),
                        requirement=req.to_dict(),
                        render_context=context,
                    )

    # 语义框：动作和对象必须与具体界面用法一致。
    expected_frame = catalogue_frame(row)
    observed_frame = translation_frame(target)
    for conflict in frame_conflicts(expected_frame, observed_frame):
        params = trigger_params(contract)
        add(
            conflict["code"],
            "error",
            conflict["message"],
            location="message",
            expected_frame=expected_frame.to_dict(),
            observed_frame=observed_frame.to_dict(),
            render_context=_render_context(contract, message, params, locale, target_paths=selected_branch_paths(target.nodes, params, locale=locale)),
        )

    # 每个实际可达分支也不能换成相反动作或错误对象；分支可以省略动词，
    # 但只要出现了动作/对象词，就必须与该用法的语义框一致。
    for view in views.values():
        branch_params = params_for_target_path(target, view.path)
        selected_for_branch = selected_branch_paths(target.nodes, branch_params, locale=locale)
        if view.path not in selected_for_branch:
            continue
        rendered_branch = render_nodes(view.nodes, branch_params, locale=locale, strict=False, highlight_missing=True)
        branch_frame = translation_frame(rendered_branch)
        branch_context = _render_context(
            contract,
            message,
            branch_params,
            locale,
            target_paths=selected_for_branch,
            target_path=view.path,
        )
        branch_context["rendered_branch"] = rendered_branch
        if expected_frame.action and branch_frame.action and branch_frame.action != expected_frame.action:
            add(
                "branch_wrong_action",
                "error",
                f"可达分支 {view.path} 的动作是 {branch_frame.action}，与界面要求的 {expected_frame.action} 不一致",
                location=view.path,
                branch=view.path,
                expected_frame=expected_frame.to_dict(),
                observed_frame=branch_frame.to_dict(),
                render_context=branch_context,
            )
        strong_objects = {"filter", "recent_records", "hotkey"}
        if (
            expected_frame.object in strong_objects
            and branch_frame.object
            and branch_frame.object != expected_frame.object
        ):
            add(
                "branch_wrong_object",
                "error",
                f"可达分支 {view.path} 的动作对象是 {branch_frame.object}，与界面要求的 {expected_frame.object} 不一致",
                location=view.path,
                branch=view.path,
                expected_frame=expected_frame.to_dict(),
                observed_frame=branch_frame.to_dict(),
                render_context=branch_context,
            )

    # 术语表按 release + usage 场景选择；只给建议级结果，已核准则可覆盖措辞。
    if glossary_rows is not None:
        for term in select_glossary(
            glossary_rows,
            release=str(row.get("release")),
            usage=str(row.get("usage") or ""),
            source=str(row.get("source") or ""),
        ):
            preferred = str(term.get("preferred") or "")
            plain = observed_frame.raw_text
            if preferred and preferred not in plain:
                add(
                    "glossary_preferred_term_absent",
                    "warning",
                    f"该场景的{term.get('source')}建议译为“{preferred}”；如已核准同义说法可保留",
                    location="glossary",
                    glossary_usage=term.get("usage"),
                    preferred=preferred,
                    permit_fallback=term.get("permit_fallback"),
                )

    return findings


def _render_context(
    contract: MessageContract,
    target_message: str,
    params: dict[str, Any],
    locale: str,
    *,
    target_paths: list[str],
    source_path: str = "",
    target_path: str = "",
) -> dict[str, Any]:
    source_rendered = render(contract.parsed, params, locale="en", strict=False, highlight_missing=True)
    target_rendered = render(target_message, params, locale=locale, strict=False, highlight_missing=True)
    return {
        "locale": locale,
        "params": params,
        "source_branch": source_path,
        "target_branch": target_path,
        "selected_target_paths": target_paths,
        "source_rendered": source_rendered,
        "rendered": target_rendered,
        "hint": "用这些参数可在对应界面状态下复现译者实际看到的文案",
    }


def error_findings(findings: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [finding for finding in findings if finding.get("severity") == "error"]
