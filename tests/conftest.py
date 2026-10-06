"""测试公共配置。

把 tests/ 目录加入 sys.path，使 `tests/fixtures/excel_samples.py` 可以通过
`from fixtures.excel_samples import ...` 导入；样例是测试资产，不进入 Python 包。
"""

from __future__ import annotations

import sys
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parent
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))
