"""读取项目的 JSON/JSONL 资料。"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


@dataclass
class WorkbenchData:
    root: Path
    manifest: dict[str, list[str]]
    catalogue: list[dict[str, Any]]
    glossary: list[dict[str, Any]]
    submissions: list[dict[str, Any]]
    changes: list[dict[str, Any]]
    icu_subset: list[dict[str, Any]]

    @property
    def target_locale(self) -> str:
        return "zh-Hans"


def read_jsonl_checked(path: Path, fields: list[str] | set[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    field_set = set(fields)
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"{path.name} 第 {line_number} 行不是有效的 JSON") from error
            if not isinstance(row, dict) or set(row) != field_set:
                raise ValueError(f"{path.name} 第 {line_number} 行与资料索引不符")
            rows.append(row)
    return rows


def load_data(data_dir: str | Path = "data") -> WorkbenchData:
    root = Path(data_dir)
    manifest_path = root / "index.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    collections: dict[str, list[dict[str, Any]]] = {}
    for name, fields in manifest.items():
        path = root / f"{name}.jsonl"
        if not path.exists():
            raise FileNotFoundError(f"缺少资料集合：{path}")
        collections[name] = read_jsonl_checked(path, fields)
    return WorkbenchData(root=root, manifest=manifest, **collections)  # type: ignore[arg-type]


def load_json(path: str | Path, default: Any) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def atomic_write_json(path: str | Path, value: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    temporary = p.with_suffix(p.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    temporary.replace(p)
