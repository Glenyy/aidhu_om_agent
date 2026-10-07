"""接口层共用的最小依赖（S04-02）。

按 [plan/09 §7](../../../plan/09-数据库与持久化设计.md)，**每个操作各用一条连接**，
用完即关；不在应用状态里放共享连接（FastAPI 默认在线程池里跑同步代码，共享连接
会跨线程使用）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import Request

from ..storage import Database


@contextmanager
def database_connection(request: Request) -> Iterator[sqlite3.Connection]:
    """为一次请求打开一条连接；离开上下文即关闭。"""
    database: Database = request.app.state.db
    connection = database.connect()
    try:
        yield connection
    finally:
        connection.close()


__all__ = ["database_connection"]
