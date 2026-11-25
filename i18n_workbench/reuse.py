"""复用索引：定位同句多用、拆合 lineage，并按具体用法判断能否复用。"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable

from .icu import ICUError, canonical_shape
from .validation import error_findings, validate_candidate


DecisionResolver = Callable[[tuple[str, str, str], str], dict[str, Any] | None]


def source_occurrences(catalogue: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按原文和 ICU 结构列出所有复用位置。"""

    exact_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    shape_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in catalogue:
        identity = {"release": row["release"], "key": row["key"], "usage": row.get("usage"), "around": row.get("around")}
        exact_groups[row["source"]].append(identity)
        try:
            shape = canonical_shape(row["source"])
        except ICUError:
            shape = "invalid"
        shape_groups[shape].append(identity)

    result: list[dict[str, Any]] = []
    for source, locations in exact_groups.items():
        if len(locations) > 1:
            result.append({"type": "identical_source", "source": source, "count": len(locations), "locations": locations})
    for shape, locations in shape_groups.items():
        if shape != "invalid" and len(locations) > 1:
            sources = sorted({loc.get("key", "") for loc in locations})
            result.append({"type": "similar_icu_shape", "shape": shape, "count": len(locations), "keys": sources, "locations": locations})
    return result


def _issue_codes(findings: list[dict[str, Any]]) -> set[str]:
    return {f["code"] for f in error_findings(findings)}


def find_candidates_for_usage(
    target_row: dict[str, Any],
    submissions: list[dict[str, Any]],
    rows_by_identity: dict[tuple[str, str], dict[str, Any]],
    *,
    locale: str = "zh-Hans",
    glossary_rows: list[dict[str, Any]] | None = None,
    decision_resolver: DecisionResolver | None = None,
) -> list[dict[str, Any]]:
    """返回某一个具体 ``release/key/locale`` 可复核的候选译句。"""

    target = (str(target_row["release"]), str(target_row["key"]), locale)
    suggestions: list[dict[str, Any]] = []

    try:
        target_shape = canonical_shape(target_row["source"])
    except ICUError:
        target_shape = "invalid"

    for candidate in submissions:
        if candidate.get("locale") != locale:
            continue
        candidate_row = rows_by_identity.get((str(candidate.get("release")), str(candidate.get("key"))))
        if candidate_row is None:
            continue

        direct = (
            candidate.get("release") == target_row.get("release")
            and candidate.get("key") == target_row.get("key")
        )
        same_source = candidate_row.get("source") == target_row.get("source")
        try:
            candidate_shape = canonical_shape(candidate_row["source"])
        except ICUError:
            candidate_shape = "invalid"
        same_shape = candidate_shape == target_shape

        # 不相关的候选不跑完整报告；同原文/同结构/直接键一定纳入。
        if not direct and not same_source and not same_shape:
            continue

        findings = validate_candidate(
            target_row,
            str(candidate["message"]),
            locale=locale,
            glossary_rows=glossary_rows,
            candidate_id=str(candidate.get("id")),
        )
        errors = error_findings(findings)
        codes = _issue_codes(findings)
        context_ok = not (codes & {"wrong_action", "wrong_object", "missing_object", "object_term_missing"})
        contract_ok = not (codes & {"missing_parameter", "unknown_parameter", "missing_unit", "icu_syntax", "source_icu_syntax"})
        technically_usable = not errors

        relation = "semantic_match"
        if direct:
            relation = "direct"
        elif same_source and context_ok and contract_ok:
            relation = "approved_same_usage"
        elif same_source and not context_ok:
            relation = "same_source_context_conflict"
        elif same_shape and not contract_ok:
            relation = "similar_sentence_contract_conflict"
        elif not contract_ok:
            relation = "contract_conflict"

        decision = decision_resolver(target, str(candidate["id"])) if decision_resolver else None
        decision_forbidden = bool(decision and decision.get("decision") == "forbidden")
        # “禁止复用”的全局决定只阻止把该句搬到别的用法；它原来的直接
        # release/key 仍按自身核准状态发布。针对具体用法的禁止则对该用法
        # （包括直接键）生效。
        blocked = decision_forbidden and (
            not direct or decision.get("scope", "usage") == "usage"
        )
        usage_approved = bool(decision and decision.get("decision") == "approved")

        score = 0
        if direct:
            score += 100
        if same_source:
            score += 30
        if same_shape:
            score += 10
        if context_ok:
            score += 25
        if contract_ok:
            score += 25
        if blocked:
            score -= 1000

        suggestions.append(
            {
                "target_release": target[0],
                "target_key": target[1],
                "locale": locale,
                "candidate_id": candidate.get("id"),
                "candidate_release": candidate.get("release"),
                "candidate_key": candidate.get("key"),
                "message": candidate.get("message"),
                "direct": direct,
                "same_source": same_source,
                "same_icu_shape": same_shape,
                "relation": relation,
                "context_compatible": context_ok,
                "contract_compatible": contract_ok,
                "technically_usable": technically_usable,
                "forbidden": blocked,
                "global_reuse_forbidden": bool(
                    decision_forbidden and decision and decision.get("scope") == "translation"
                ),
                "review_decision": decision,
                "usage_approved": usage_approved,
                "score": score,
                "errors": errors,
                "warnings": [f for f in findings if f.get("severity") == "warning"],
            }
        )

    suggestions.sort(key=lambda item: item["score"], reverse=True)
    return suggestions


def build_reuse_report(
    catalogue: list[dict[str, Any]],
    submissions: list[dict[str, Any]],
    *,
    locale: str = "zh-Hans",
    glossary_rows: list[dict[str, Any]] | None = None,
    decision_resolver: DecisionResolver | None = None,
) -> dict[str, Any]:
    rows_by_identity = {(str(row["release"]), str(row["key"])): row for row in catalogue}
    usages: list[dict[str, Any]] = []
    for row in catalogue:
        if row.get("locale") and row.get("locale") != "en":
            # 当前目录是英文源；若以后加入其他源语言行，仍可显式调用。
            pass
        candidates = find_candidates_for_usage(
            row,
            submissions,
            rows_by_identity,
            locale=locale,
            glossary_rows=glossary_rows,
            decision_resolver=decision_resolver,
        )
        usages.append(
            {
                "release": row["release"],
                "key": row["key"],
                "locale": locale,
                "usage": row.get("usage"),
                "around": row.get("around"),
                "candidates": candidates,
            }
        )
    return {"source_occurrences": source_occurrences(catalogue), "usages": usages}
