"""按“动作 + 对象 + 使用场景”理解界面消息。

仅靠英文原句或中文相似度会把筛选器上的 Clear 和最近记录旁的 Clear 混为一
谈。这里从界面路径、相邻提示、键名和术语表中提取一个可比较的语义框
（frame）。它不试图做通用 NLP，只覆盖桌面资料中出现的受控词表，并允许
项目状态以后登记已核准的同义说法。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Iterable

from .icu import ParsedMessage, Text, parse_message


_ACTION_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("clear", ("clear", "清空", "清除")),
    ("reset", ("reset", "重置", "还原", "恢复默认", "恢复预设")),
    ("remove", ("remove", "delete", "移除", "删除")),
    ("save", ("save", "保存", "储存")),
    ("cancel", ("cancel", "取消")),
]

# 顺序即优先级：快捷键等更具体的场景要先于宽泛的 search/filter。
_OBJECT_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("hotkey", ("hotkey", "keyboard shortcut", "shortcut", "键位", "快捷键")),
    (
        "filter",
        (
            "filter",
            "filters",
            "filtered",
            "filter by",
            "筛选",
            "过滤",
            "all categories",
            "categor",
            "month",
            "saved filter",
            "no query",
        ),
    ),
    (
        "recent_records",
        (
            "recent searches",
            "recent",
            "history",
            "privacy",
            "最近记录",
            "最近",
            "历史",
            "搜索记录",
            "浏览记录",
            "隐私",
            "记录",
        ),
    ),
    ("sync_queue", ("sync", "queue", "同步", "队列")),
    ("sort", ("sort", "排序")),
]

_OBJECT_ZH_TOKENS: dict[str, tuple[str, ...]] = {
    "filter": ("筛选", "过滤", "过滤器", "筛选器"),
    "recent_records": ("记录", "历史", "日志"),
    "hotkey": ("快捷键", "键位", "按键"),
    "sync_queue": ("同步", "队列"),
    "sort": ("排序",),
}


@dataclass(frozen=True)
class Frame:
    action: str | None = None
    object: str | None = None
    source_verb: str | None = None
    signals: tuple[str, ...] = ()
    raw_text: str = ""
    usage_object: str | None = None
    around_object: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "object": self.object,
            "source_verb": self.source_verb,
            "signals": list(self.signals),
            "usage_object": self.usage_object,
            "around_object": self.around_object,
        }


def _contains_word(haystack: str, word: str) -> bool:
    # 中文短语直接子串；英文术语允许常见复数。
    if re.search(r"[A-Za-z]", word):
        pattern = r"(?<![A-Za-z])" + re.escape(word) + r"(?:s|es)?(?![A-Za-z])"
        return re.search(pattern, haystack, re.IGNORECASE) is not None
    return word in haystack


def detect_action(text: str) -> tuple[str | None, str | None]:
    lowered = text.lower()
    for action, words in _ACTION_PATTERNS:
        for word in words:
            if _contains_word(lowered, word.lower()):
                return action, word
    return None, None


def detect_object(text: str) -> tuple[str | None, tuple[str, ...]]:
    lowered = text.lower()
    signals: list[str] = []
    for obj, words in _OBJECT_PATTERNS:
        hits = [word for word in words if word.lower() in lowered or _contains_word(lowered, word.lower())]
        if hits:
            signals.extend(hits)
            return obj, tuple(signals)
    return None, tuple(signals)


def catalogue_frame(row: dict[str, Any]) -> Frame:
    """从目录行及界面语境提取预期语义框。"""

    context_blob = " ".join(
        str(row.get(name) or "")
        for name in ("usage", "around", "key", "release")
    ).lower()
    # 显式界面路径/键名比相邻提示更权威：当目录变动只改 usage 时，不能让
    # 旧 around 中的词覆盖新的用法归类。around 仍保留为辅助信号。
    usage_blob = " ".join(str(row.get(name) or "") for name in ("usage", "key")).lower()
    around_blob = str(row.get("around") or "").lower()
    source = str(row.get("source") or "")

    # 动作以源句首词/显式动词为准，避免相邻提示里的 “Reset search”
    # 覆盖源句 Clear 的动作。
    source_action, source_verb = detect_action(source)
    if source_action is None:
        key_action, key_verb = detect_action(str(row.get("key") or "").replace(".", " "))
        source_action, source_verb = key_action, key_verb
    usage_object, usage_signals = detect_object(usage_blob)
    around_object, around_signals = detect_object(around_blob)
    obj = usage_object or around_object
    signals = tuple([*usage_signals, *around_signals])
    return Frame(
        action=source_action,
        object=obj,
        source_verb=source_verb,
        signals=signals,
        raw_text=context_blob,
        usage_object=usage_object,
        around_object=around_object,
    )


def message_plain_text(message: str | ParsedMessage) -> str:
    """收集 ICU 各分支中的可翻译文字，供受控词表检查。"""

    parsed = message if isinstance(message, ParsedMessage) else parse_message(message)
    chunks: list[str] = []

    def walk(nodes: Iterable[Any]) -> None:
        for node in nodes:
            if isinstance(node, Text):
                chunks.append(node.text)
            elif hasattr(node, "branches"):
                for branch in node.branches.values():  # type: ignore[attr-defined]
                    walk(branch)

    walk(parsed.nodes)
    return " ".join(chunks)


def translation_frame(message: str | ParsedMessage) -> Frame:
    text = message_plain_text(message)
    action, verb = detect_action(text)
    obj, signals = detect_object(text)
    return Frame(action=action, object=obj, source_verb=verb, signals=signals, raw_text=text)


def frame_conflicts(expected: Frame, observed: Frame) -> list[dict[str, str]]:
    """返回语义框不兼容原因；空列表表示可按该用法复用。"""

    problems: list[dict[str, str]] = []
    if expected.action and observed.action and expected.action != observed.action:
        problems.append(
            {
                "code": "wrong_action",
                "message": f"动作应为 {expected.action}，译文表现为 {observed.action}",
            }
        )

    strong_objects = {"filter", "recent_records", "hotkey"}
    if expected.object in strong_objects:
        if observed.object and observed.object != expected.object:
            problems.append(
                {
                    "code": "wrong_object",
                    "message": f"动作对象应为 {expected.object}，译文指向 {observed.object}",
                }
            )
        elif not observed.object and expected.object != "hotkey":
            # hotkey 由必传参数检查给出更准确的反馈。
            tokens = "/".join(_OBJECT_ZH_TOKENS.get(expected.object, ()))
            problems.append(
                {
                    "code": "missing_object",
                    "message": f"译文没有显示动作对象；该界面用法要求出现 {tokens} 等明确对象",
                }
            )

    # 这些场景必须在译文正文中显式出现对象词。hotkey 由必传参数和样例
    # 渲染负责；sync_queue 可以用量词表达，不要求出现“同步/队列”原词。
    lexical_objects = {"filter", "recent_records"}
    if expected.object in lexical_objects and expected.object in _OBJECT_ZH_TOKENS:
        required_tokens = _OBJECT_ZH_TOKENS[expected.object]
        if not any(token in observed.raw_text for token in required_tokens):
            # wrong_object 已添加时不重复添加。
            if not any(p["code"] == "wrong_object" for p in problems):
                problems.append(
                    {
                        "code": "object_term_missing",
                        "message": "译文中缺少该用法对应的对象词：" + "/".join(required_tokens),
                    }
                )
    return problems


def match_glossary_usage(catalogue_usage: str, glossary_usage: str) -> bool:
    """术语 usage 可以是完整路径，也可以是其中一个路径组件。"""

    catalogue_usage = catalogue_usage or ""
    glossary_usage = glossary_usage or ""
    if not glossary_usage:
        return False
    if glossary_usage == catalogue_usage:
        return True
    parts = [part.strip() for part in re.split(r"[/／>]+", catalogue_usage) if part.strip()]
    if glossary_usage in parts:
        return True
    return glossary_usage in catalogue_usage or catalogue_usage in glossary_usage


def select_glossary(
    glossary_rows: Iterable[dict[str, Any]],
    *,
    release: str,
    usage: str,
    source: str,
) -> list[dict[str, Any]]:
    """选择该发行版、适用场景和源词对应的术语，绝不做全局一词一译。"""

    source_lower = source.lower()
    source_words = set(re.findall(r"[a-z]+", source_lower))
    matches: list[tuple[int, dict[str, Any]]] = []
    for row in glossary_rows:
        if row.get("release") != release:
            continue
        if not match_glossary_usage(usage, str(row.get("usage") or "")):
            continue
        term = str(row.get("source") or "")
        term_matches = _contains_word(source_lower, term.lower()) or term.lower() in source_words
        if not term_matches:
            continue
        # 越长的 usage/term 越具体，优先返回；调用方仍可看到全部匹配。
        score = len(str(row.get("usage") or "")) * 10 + len(term)
        matches.append((score, row))
    matches.sort(key=lambda item: item[0], reverse=True)
    return [row for _, row in matches]
