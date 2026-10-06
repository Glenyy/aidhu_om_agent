"""``python -m aidhu_om_agent`` 的模块入口。

命令行实现在 `cli.py`（S01 为工程验证入口，S02-05 增加只读 ``inspect``）。
本模块只做转发，避免出现两份参数定义。
"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
