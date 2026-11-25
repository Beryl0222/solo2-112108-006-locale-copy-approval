#!/usr/bin/env python3
"""项目根目录下的便捷入口。

既可以直接运行::

    python workbench.py check

也可以在测试中 ``import workbench`` 后使用这里再导出的常用 API。
"""

from pathlib import Path
import sys

# 允许以 `python /path/to/workbench.py` 直接运行；隔离模式下也显式加入根目录。
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from i18n_workbench import Workbench, WorkbenchData, load_data  # noqa: E402
from i18n_workbench.cli import main  # noqa: E402
from i18n_workbench.validation import validate_candidate  # noqa: E402

__all__ = ["Workbench", "WorkbenchData", "load_data", "validate_candidate", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
