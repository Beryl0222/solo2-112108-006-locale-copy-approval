#!/usr/bin/env python3
"""``python main.py ...`` 入口，转发到 :mod:`i18n_workbench.cli`。"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from i18n_workbench.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
