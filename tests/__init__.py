"""tests 包：把 src 目录加入 sys.path。

保证未执行 ``pip install -e .`` 时，``python -m unittest``（发现模式会
导入 tests 包）也能直接 import ``mant.*``；pytest 场景由 conftest.py
提供同等效果。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
