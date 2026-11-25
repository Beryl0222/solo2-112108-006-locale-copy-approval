"""ICU MessageFormat 子集的解析、渲染与分支定位。

只实现业务资料 ``icu_subset.jsonl`` 中声明的能力：

* 命名变量 ``{name}``；
* ``plural``，支持 ``one``、``other`` 和 ``=0`` 这类精确分支以及 ``#``；
* ``select``，字符串选择值，缺省走 ``other``；
* 分支可以嵌套。

标准库之外没有依赖。解析器会保留源码位置所需的分支路径，方便门禁把错误
指到具体分支和实际渲染上下文。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable


class ICUError(ValueError):
    """消息不符合受支持的 ICU 子集。"""


@dataclass(frozen=True)
class Text:
    text: str


@dataclass(frozen=True)
class Hash:
    """复数分支中的 ``#``，渲染为当前复数参数的数值。"""


@dataclass(frozen=True)
class Variable:
    name: str


@dataclass(frozen=True)
class Plural:
    name: str
    branches: dict[str, tuple[Node, ...]]
    offset: int = 0
    kind: str = "plural"  # plural 或未来扩展；当前保留区分能力


@dataclass(frozen=True)
class Select:
    name: str
    branches: dict[str, tuple[Node, ...]]


Node = Text | Hash | Variable | Plural | Select


@dataclass(frozen=True)
class ParsedMessage:
    source: str
    nodes: tuple[Node, ...]
    locale: str | None = None

    def variables(self) -> set[str]:
        return message_variables(self.nodes)


@dataclass
class _Parser:
    text: str
    pos: int = 0

    def parse(self) -> tuple[Node, ...]:
        nodes = self._sequence(stop_at_brace=False)
        if self.pos != len(self.text):
            raise ICUError(f"第 {self.pos + 1} 个字符附近有多余的 '}}'")
        return tuple(nodes)

    def _peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def _sequence(self, *, stop_at_brace: bool, allow_hash: bool = False) -> list[Node]:
        nodes: list[Node] = []
        buffer: list[str] = []

        def flush() -> None:
            if buffer:
                nodes.append(Text("".join(buffer)))
                buffer.clear()

        while self.pos < len(self.text):
            ch = self.text[self.pos]
            if ch == "}":
                if stop_at_brace:
                    flush()
                    return nodes
                raise ICUError(f"第 {self.pos + 1} 个字符出现没有匹配 '{{' 的 '}}'")
            if ch == "#" and allow_hash:
                flush()
                nodes.append(Hash())
                self.pos += 1
                continue
            if ch == "'":
                # ICU 引号规则的常用部分：'' 表示一个撇号；
                # 成对单引号内的花括号按普通文本处理。
                if self.pos + 1 < len(self.text) and self.text[self.pos + 1] == "'":
                    buffer.append("'")
                    self.pos += 2
                    continue
                end = self.text.find("'", self.pos + 1)
                if end == -1:
                    buffer.append(ch)
                    self.pos += 1
                else:
                    buffer.append(self.text[self.pos + 1 : end])
                    self.pos = end + 1
                continue
            if ch == "{":
                flush()
                self.pos += 1
                nodes.append(self._argument())
                continue
            buffer.append(ch)
            self.pos += 1

        if stop_at_brace:
            raise ICUError("ICU 分支缺少结束的 '}'")
        flush()
        return nodes

    def _skip_ws(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos].isspace():
            self.pos += 1

    def _read_until(self, stops: str) -> str:
        start = self.pos
        while self.pos < len(self.text) and self.text[self.pos] not in stops:
            self.pos += 1
        return self.text[start : self.pos].strip()

    def _argument(self) -> Node:
        self._skip_ws()
        name = self._read_until(",}")
        if not name:
            raise ICUError(f"第 {self.pos + 1} 个字符附近缺少变量名")
        if self._peek() == "}":
            self.pos += 1
            return Variable(name)
        if self._peek() != ",":
            raise ICUError(f"变量 {name!r} 后应有 ',' 或 '}}'")
        self.pos += 1
        self._skip_ws()
        arg_type = self._read_until(", \t\r\n}")
        self._skip_ws()
        if self._peek() == ",":
            self.pos += 1
        self._skip_ws()

        if arg_type not in {"plural", "select"}:
            raise ICUError(f"不支持的 ICU 参数类型 {arg_type!r}（变量 {name!r}）")

        branches = self._branches()
        if "other" not in branches:
            raise ICUError(f"{arg_type} 参数 {name!r} 必须包含 other 分支")
        if arg_type == "plural":
            return Plural(name=name, branches=branches)
        return Select(name=name, branches=branches)

    def _branches(self) -> dict[str, tuple[Node, ...]]:
        branches: dict[str, tuple[Node, ...]] = {}
        while True:
            self._skip_ws()
            ch = self._peek()
            if ch == "}":
                self.pos += 1
                return branches
            if not ch:
                raise ICUError("ICU 分支列表没有结束的 '}'")

            key_start = self.pos
            while self.pos < len(self.text) and not self.text[self.pos].isspace() and self.text[self.pos] != "{":
                self.pos += 1
            key = self.text[key_start : self.pos].strip()
            if not key:
                raise ICUError("ICU 分支缺少分支名")
            self._skip_ws()
            if self._peek() != "{":
                raise ICUError(f"分支 {key!r} 后缺少 '{{'")
            self.pos += 1
            parser = _Parser(self.text, self.pos)
            branch_nodes = parser._sequence(stop_at_brace=True, allow_hash=True)
            self.pos = parser.pos
            if self._peek() != "}":
                raise ICUError(f"分支 {key!r} 缺少结束的 '}}'")
            self.pos += 1
            if key in branches:
                raise ICUError(f"分支 {key!r} 重复")
            branches[key] = tuple(branch_nodes)


def parse_message(source: str) -> ParsedMessage:
    """解析 ICU 子集消息；语法不支持时抛出 :class:`ICUError`。"""

    if not isinstance(source, str):
        raise ICUError("消息必须是字符串")
    parser = _Parser(source)
    return ParsedMessage(source=source, nodes=parser.parse())


def message_variables(nodes: Iterable[Node]) -> set[str]:
    """返回消息中出现的全部命名变量（含选择参数）。"""

    result: set[str] = set()

    def walk(items: Iterable[Node]) -> None:
        for node in items:
            if isinstance(node, Variable):
                result.add(node.name)
            elif isinstance(node, (Plural, Select)):
                result.add(node.name)
                for branch in node.branches.values():
                    walk(branch)

    walk(nodes)
    return result


def branch_variables(nodes: Iterable[Node]) -> set[str]:
    """返回某个具体分支正文中的变量。

    与 :func:`message_variables` 不同，调用方通常先选定一个分支，再把该分支
    的节点传入。复数选择参数如果只用于选分支，不会仅因 Plural 节点本身被
    计入；但分支中的 ``#`` 由 :func:`branch_uses_hash` 表示。
    """

    result: set[str] = set()

    def walk(items: Iterable[Node]) -> None:
        for node in items:
            if isinstance(node, Variable):
                result.add(node.name)
            elif isinstance(node, (Plural, Select)):
                # 嵌套参数在其自己的分支里处理；选择器名字本身不是正文变量。
                for branch in node.branches.values():
                    walk(branch)

    walk(nodes)
    return result


def branch_uses_hash(nodes: Iterable[Node]) -> bool:
    for node in nodes:
        if isinstance(node, Hash):
            return True
        if isinstance(node, (Plural, Select)):
            if any(branch_uses_hash(branch) for branch in node.branches.values()):
                return True
    return False


def direct_variables(nodes: Iterable[Node]) -> set[str]:
    """只取当前分支正文里的命名变量，不进入嵌套 ICU 的未选分支。"""

    result: set[str] = set()
    for node in nodes:
        if isinstance(node, Variable):
            result.add(node.name)
    return result


def direct_uses_hash(nodes: Iterable[Node]) -> bool:
    """当前分支正文是否直接含 ``#``，不把嵌套复数分支中的 # 计入。"""

    return any(isinstance(node, Hash) for node in nodes)


def plural_category(value: Any, locale: str | None) -> str:
    """资料所需的最小复数规则。

    完整 CLDR 不是这个项目的目标。英语实现 ``one/other``，中文一律
    ``other``；精确分支（``=0`` 等）在渲染器中优先。
    """

    try:
        number = Decimal(str(value))
    except Exception:  # noqa: BLE001 - 门禁需要把坏值落到报告中
        return "other"
    if locale and locale.lower().startswith("zh"):
        return "other"
    # en 及未知语言按资料中的 en profile 处理。
    return "one" if abs(number) == 1 else "other"


class MissingParameter(KeyError):
    def __init__(self, name: str):
        super().__init__(name)
        self.name = name


def _format_value(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def render_nodes(
    nodes: Iterable[Node],
    params: dict[str, Any] | None = None,
    *,
    locale: str = "zh-Hans",
    strict: bool = True,
    highlight_missing: bool = False,
    _plural_value: Any = None,
) -> str:
    """把 AST 渲染成门禁所检查的实际字符串。"""

    params = params or {}
    parts: list[str] = []

    def missing(name: str) -> str:
        if strict:
            raise MissingParameter(name)
        return f"⟦缺少 {name}⟧" if highlight_missing else ""

    for node in nodes:
        if isinstance(node, Text):
            # 未加引号的 # 在解析期已经成为 Hash；引号中的 # 是普通文本，
            # 因此这里不能再做一次全局替换。
            parts.append(node.text)
        elif isinstance(node, Hash):
            parts.append(_format_value(_plural_value) if _plural_value is not None else "#")
        elif isinstance(node, Variable):
            if node.name in params and params[node.name] is not None:
                parts.append(_format_value(params[node.name]))
            else:
                parts.append(missing(node.name))
        elif isinstance(node, Plural):
            value = params.get(node.name)
            if value is None:
                rendered_missing = missing(node.name)
                parts.append(rendered_missing)
                continue
            exact_key = f"={value}"
            if exact_key in node.branches:
                chosen = node.branches[exact_key]
            else:
                category = plural_category(value, locale)
                chosen = node.branches.get(category) or node.branches["other"]
            parts.append(
                render_nodes(
                    chosen,
                    params,
                    locale=locale,
                    strict=strict,
                    highlight_missing=highlight_missing,
                    _plural_value=value,
                )
            )
        elif isinstance(node, Select):
            value = params.get(node.name)
            key = None if value is None else str(value)
            chosen = node.branches.get(key) if key is not None else None
            if chosen is None:
                chosen = node.branches["other"]
            parts.append(
                render_nodes(
                    chosen,
                    params,
                    locale=locale,
                    strict=strict,
                    highlight_missing=highlight_missing,
                )
            )
    return "".join(parts)


def render(
    message: str | ParsedMessage,
    params: dict[str, Any] | None = None,
    *,
    locale: str = "zh-Hans",
    strict: bool = True,
    highlight_missing: bool = False,
) -> str:
    parsed = message if isinstance(message, ParsedMessage) else parse_message(message)
    return render_nodes(
        parsed.nodes,
        params,
        locale=locale,
        strict=strict,
        highlight_missing=highlight_missing,
    )


@dataclass(frozen=True)
class BranchView:
    path: str
    kind: str
    selector: str | None
    key: str
    nodes: tuple[Node, ...] = field(repr=False)

    @property
    def variables(self) -> set[str]:
        return branch_variables(self.nodes)

    @property
    def uses_hash(self) -> bool:
        return branch_uses_hash(self.nodes)

    @property
    def direct_variable_names(self) -> set[str]:
        return direct_variables(self.nodes)

    @property
    def direct_hash(self) -> bool:
        return direct_uses_hash(self.nodes)


def iter_branches(nodes: Iterable[Node], prefix: str = "") -> list[BranchView]:
    """列出所有 ICU 分支，路径形如 ``plural.other`` / ``select.saved``。"""

    result: list[BranchView] = []
    for node in nodes:
        if isinstance(node, (Plural, Select)):
            kind = "plural" if isinstance(node, Plural) else "select"
            base = f"{prefix}.{kind}" if prefix else kind
            for key, branch_nodes in node.branches.items():
                path = f"{base}[{key}]"
                result.append(BranchView(path, kind, node.name, key, branch_nodes))
                result.extend(iter_branches(branch_nodes, path))
    return result


def top_level_selector_kind(nodes: Iterable[Node]) -> str | None:
    for node in nodes:
        if isinstance(node, Plural):
            return "plural"
        if isinstance(node, Select):
            return "select"
    return None


def canonical_shape(message: str | ParsedMessage) -> str:
    """生成忽略参数名和空白的结构签名，用于发现“看起来相似”的消息。"""

    parsed = message if isinstance(message, ParsedMessage) else parse_message(message)

    def shape(nodes: Iterable[Node]) -> str:
        parts: list[str] = []
        for node in nodes:
            if isinstance(node, Text):
                parts.append("text")
            elif isinstance(node, Hash):
                parts.append("#")
            elif isinstance(node, Variable):
                parts.append("var")
            elif isinstance(node, Plural):
                keys = sorted(node.branches)
                branches = ",".join(keys)
                inner = ";".join(shape(node.branches[key]) for key in keys)
                parts.append(f"plural({branches})[{inner}]")
            elif isinstance(node, Select):
                keys = sorted(node.branches)
                branches = ",".join(keys)
                inner = ";".join(shape(node.branches[key]) for key in keys)
                parts.append(f"select({branches})[{inner}]")
        return "|".join(parts)

    return shape(parsed.nodes)
