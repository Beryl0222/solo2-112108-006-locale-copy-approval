"""命令行入口：检查、复用复核、核准、出包和验包。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from .workbench import Workbench


def _json_print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _finding_text(finding: dict[str, Any]) -> list[str]:
    loc = f"{finding.get('release')}/{finding.get('key')} [{finding.get('locale')}]"
    lines = [f"[{finding['severity'].upper()} {finding['code']}] {loc} {finding.get('candidate_id') or ''}"]
    if finding.get("usage"):
        lines.append(f"  界面路径: {finding['usage']}")
    if finding.get("around"):
        lines.append(f"  相邻提示: {finding['around']}")
    if finding.get("location"):
        lines.append(f"  失败位置: {finding['location']}")
    lines.append(f"  问题: {finding['message']}")
    context = finding.get("render_context")
    if context:
        lines.append(f"  触发参数: {json.dumps(context.get('params'), ensure_ascii=False)}")
        if context.get("source_rendered") is not None:
            lines.append(f"  源文渲染: {context['source_rendered']}")
        lines.append(f"  实际渲染: {context.get('rendered')}")
    return lines


def _cmd_check(wb: Workbench, args: argparse.Namespace) -> int:
    report = wb.gate(release=args.release, locale=args.locale)
    if args.json:
        _json_print(report)
        return 0 if report["ok"] else 1
    for finding in report["findings"]:
        print("\n".join(_finding_text(finding)))
        print()
    print(f"检查译稿 {report['checked_candidates']} 条；错误 {report['error_count']}，警告 {report['warning_count']}")
    return 0 if report["ok"] else 1


def _cmd_reuse(wb: Workbench, args: argparse.Namespace) -> int:
    report = wb.reuse_report(locale=args.locale)
    if args.release or args.key:
        report["usages"] = [
            usage
            for usage in report["usages"]
            if (not args.release or usage["release"] == args.release)
            and (not args.key or usage["key"] == args.key)
        ]
    if args.json:
        _json_print(report)
        return 0
    for occurrence in report["source_occurrences"]:
        print(f"复用簇 {occurrence['type']} ×{occurrence['count']}: {occurrence.get('source') or occurrence.get('shape')}")
        for loc in occurrence["locations"]:
            print(f"  - {loc['release']}/{loc['key']}｜{loc.get('usage')}")
    for link in report.get("lineage", []):
        if args.release and link.get("release") != args.release:
            continue
        print(
            f"拆合句 {link['change_id']}: {link.get('related_key')} -> {link.get('current_key')} "
            f"建议分支 {link.get('suggested_branch') or '需人工对齐'} @ {link.get('location')}"
        )
    print()
    for usage in report["usages"]:
        print(f"用法 {usage['release']}/{usage['key']}｜{usage.get('usage')}｜{usage.get('around')}")
        for cand in usage["candidates"]:
            mark = "可用" if cand["technically_usable"] and not cand["forbidden"] else "不可自动复用"
            decision = cand.get("review_decision")
            decision_text = f"｜决定={decision['decision']}" if decision else ""
            print(
                f"  {cand['candidate_id']} [{cand['relation']}/{mark}{decision_text}] "
                f"{cand['candidate_release']}/{cand['candidate_key']}: {cand['message']}"
            )
            for error in cand["errors"][:3]:
                print(f"      ↳ {error['code']}: {error['message']}")
        print()
    return 0


def _cmd_impact(wb: Workbench, args: argparse.Namespace) -> int:
    impact = wb.change_impact()
    lineage = wb.lineage()
    if args.json:
        _json_print({"changes": impact, "lineage": lineage})
        return 0
    for item in impact:
        print(
            f"{item['change_id']} {item['release']}/{item['key']} {item['previous']}->{item['revision']} "
            f"句意继承={'是' if item['meaning_inheritable'] else '否'} "
            f"目录核准继承={'是' if item['display_inheritable'] else '否'}"
        )
        for reason in item["reasons"]:
            print(f"  - {reason}")
        for link in item.get("lineage", []):
            print(f"  - lineage: {link['direction']} related={link['related_key']} at {link['location']}")
    return 0


def _cmd_preview_change(wb: Workbench, args: argparse.Namespace) -> int:
    result = wb.preview_change_selection(args.change_id, locale=args.locale)
    if args.json:
        _json_print(result)
        return 0
    impact = result["impact"]
    print(
        f"{impact['change_id']} 句意继承={'是' if impact['meaning_inheritable'] else '否'} "
        f"目录核准继承={'是' if impact['display_inheritable'] else '否'}"
    )
    for reason in impact["reasons"]:
        print(f"  - {reason}")
    chosen = result.get("chosen")
    if chosen:
        print(f"预览选句 [{chosen['origin']}/{chosen.get('candidate_id') or 'source'}]: {chosen['value']}")
    else:
        print("预览选句: 无可用层级，键将不会写入语言包")
    return 0


def _cmd_glossary(wb: Workbench, args: argparse.Namespace) -> int:
    from .semantics import select_glossary

    rows = select_glossary(
        wb.data.glossary,
        release=args.release,
        usage=args.usage,
        source=args.source,
    )
    if args.json:
        _json_print(rows)
    else:
        for row in rows:
            print(f"{row['release']}｜{row['usage']}｜{row['source']} → {row['preferred']}｜fallback={','.join(row['permit_fallback'])}")
    return 0 if rows or args.allow_empty else 1


def _cmd_submit(wb: Workbench, args: argparse.Namespace) -> int:
    draft = wb.submit(
        release=args.release,
        key=args.key,
        locale=args.locale,
        message=args.message,
        source_hint=args.source_hint,
        reviewer=args.reviewer,
    )
    _json_print(draft)
    return 0


def _cmd_approve(wb: Workbench, args: argparse.Namespace) -> int:
    record = wb.approve(args.candidate_id, args.kind, args.status, reviewer=args.reviewer, note=args.note)
    _json_print(record)
    return 0


def _cmd_decide_reuse(wb: Workbench, args: argparse.Namespace) -> int:
    record = wb.decide_reuse(
        target_release=args.target_release,
        target_key=args.target_key,
        candidate_id=args.candidate_id,
        decision=args.decision,
        locale=args.locale,
        scope=args.scope,
        reviewer=args.reviewer,
        note=args.note,
    )
    _json_print(record)
    return 0


def _cmd_build(wb: Workbench, args: argparse.Namespace) -> int:
    result = wb.build_pack(release=args.release, locale=args.locale, catalog=args.catalog, output_dir=args.output)
    verification = wb.verify_pack(result["manifest_path"])
    if args.json:
        _json_print({k: v for k, v in result.items() if k != "pack"} | {"verification": verification, "pack": result["pack"]})
    else:
        print(f"语言包: {result['pack_path']}")
        print(f"清单:   {result['manifest_path']}")
        print(f"条目: {result['manifest']['entry_count']}；未解析: {len(result['manifest']['unresolved'])}；跳过候选: {len(result['manifest']['rejected'])}")
        print("校验: " + ("通过" if verification["ok"] else "失败"))
        for error in verification["errors"]:
            print(f"  ERROR {error}")
    return 0 if verification["ok"] else 1


def _cmd_build_all(wb: Workbench, args: argparse.Namespace) -> int:
    releases = sorted({str(row["release"]) for row in wb.data.catalogue})
    outputs = []
    failed = False
    for release in releases:
        result = wb.build_pack(release=release, locale=args.locale, catalog=args.catalog, output_dir=args.output)
        verification = wb.verify_pack(result["manifest_path"])
        failed = failed or not verification["ok"]
        outputs.append(
            {
                "release": release,
                "pack_path": result["pack_path"],
                "manifest_path": result["manifest_path"],
                "verification": verification,
            }
        )
    if args.json:
        _json_print({"builds": outputs})
    else:
        for item in outputs:
            status = "通过" if item["verification"]["ok"] else "失败"
            print(f"{item['release']}: {item['pack_path']}（校验{status}）")
    return 1 if failed else 0


def _cmd_verify(wb: Workbench, args: argparse.Namespace) -> int:
    result = wb.verify_pack(args.manifest)
    if args.json:
        _json_print(result)
    else:
        print("校验: " + ("通过" if result["ok"] else "失败"))
        for error in result["errors"]:
            print(f"  ERROR {error}")
    return 0 if result["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="开源桌面译句复用、门禁和语言包工具")
    parser.add_argument("--root", default=".", help="项目根目录（含 data/ 和 workbench.config.json）")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="门禁检查所有译稿的实际渲染结果")
    check.add_argument("--release")
    check.add_argument("--locale")
    check.add_argument("--json", action="store_true")
    check.set_defaults(func=_cmd_check)

    reuse = sub.add_parser("reuse", help="列出同句多用、可复用候选和具体用法决定")
    reuse.add_argument("--release")
    reuse.add_argument("--key")
    reuse.add_argument("--locale")
    reuse.add_argument("--json", action="store_true")
    reuse.set_defaults(func=_cmd_reuse)

    impact = sub.add_parser("impact", help="分析目录变动后核准继承和拆合句定位")
    impact.add_argument("--json", action="store_true")
    impact.set_defaults(func=_cmd_impact)

    preview_change = sub.add_parser("preview-change", help="预览应用某条目录变动后的核准继承和选句")
    preview_change.add_argument("change_id")
    preview_change.add_argument("--locale")
    preview_change.add_argument("--json", action="store_true")
    preview_change.set_defaults(func=_cmd_preview_change)

    glossary = sub.add_parser("glossary", help="按发行版、场景和英文词查询适用术语")
    glossary.add_argument("--release", required=True)
    glossary.add_argument("--usage", required=True)
    glossary.add_argument("--source", required=True)
    glossary.add_argument("--allow-empty", action="store_true")
    glossary.add_argument("--json", action="store_true")
    glossary.set_defaults(func=_cmd_glossary)

    submit = sub.add_parser("submit", help="提交新的志愿译句草稿")
    submit.add_argument("--release", required=True)
    submit.add_argument("--key", required=True)
    submit.add_argument("--message", required=True)
    submit.add_argument("--locale")
    submit.add_argument("--source-hint")
    submit.add_argument("--reviewer")
    submit.set_defaults(func=_cmd_submit)

    approve = sub.add_parser("approve", help="分别核准 meaning（句意）或 display（目录可用性）")
    approve.add_argument("candidate_id")
    approve.add_argument("kind", choices=["meaning", "display"])
    approve.add_argument("--status", choices=["approved", "pending", "rejected"], default="approved")
    approve.add_argument("--reviewer")
    approve.add_argument("--note")
    approve.set_defaults(func=_cmd_approve)

    forbid = sub.add_parser("decide-reuse", help="按具体用法批准/禁止/复核某条候选的复用")
    forbid.add_argument("--target-release", required=True)
    forbid.add_argument("--target-key", required=True)
    forbid.add_argument("--candidate-id", required=True)
    forbid.add_argument("--decision", choices=["approved", "forbidden", "needs_review"], required=True)
    forbid.add_argument("--scope", choices=["usage", "translation"], default="usage")
    forbid.add_argument("--locale")
    forbid.add_argument("--reviewer")
    forbid.add_argument("--note")
    forbid.set_defaults(func=_cmd_decide_reuse)

    build = sub.add_parser("build", help="按发行版契约选句、回退并生成语言包")
    build.add_argument("--release", required=True)
    build.add_argument("--locale")
    build.add_argument("--catalog")
    build.add_argument("--output")
    build.add_argument("--json", action="store_true")
    build.set_defaults(func=_cmd_build)

    build_all = sub.add_parser("build-all", help="为资料中的全部发行版重新生成语言包")
    build_all.add_argument("--locale")
    build_all.add_argument("--catalog")
    build_all.add_argument("--output")
    build_all.add_argument("--json", action="store_true")
    build_all.set_defaults(func=_cmd_build_all)

    verify = sub.add_parser("verify", help="依据清单和当前资料核对成品语言包")
    verify.add_argument("manifest")
    verify.add_argument("--json", action="store_true")
    verify.set_defaults(func=_cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        wb = Workbench(args.root)
        return args.func(wb, args)
    except (FileNotFoundError, KeyError, ValueError) as error:
        print(f"错误: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
