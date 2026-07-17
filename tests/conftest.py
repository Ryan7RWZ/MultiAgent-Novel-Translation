"""pytest 公共配置：把 src 目录加入 sys.path。

pytest 启动时自动加载本文件；unittest 场景的同等注入见 tests/__init__.py。
src 布局下无需安装即可运行全部测试。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
