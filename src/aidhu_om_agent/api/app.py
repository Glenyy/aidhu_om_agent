"""FastAPI 应用与静态页面入口。

已挂载的接口：合成样例下载、上传、预检、判一条、任务查询（S03-07），批次创建
（S04-02）、恢复（S04-05）、批次列表与详情（S04-07），手动导出与导出历史（S05-04）、
已登记文件下载（S05-04）。

前端的 history 路由由 `_mount_frontend` 的回落处理，`/api/...` 不走回落。

所有错误响应（含参数校验失败与未捕获异常）统一使用 [plan/08 §1] 的 error 信封，
不混用 FastAPI 默认的 ``{"detail": ...}``。

持久化（S04-02 起）：上传登记与预检快照写入 SQLite，**进程重启后仍然有效**；
``/api/judge`` 的判别任务仍是内存态（临时接口，S06 由批次记录体系取代）。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse

from ..config import AppConfig, load_config
from ..services.judging import JudgeJobRegistry
from ..services.uploads import UploadStore
from ..storage import Database
from ..version import package_version
from .responses import fail
from .routes import artifacts, jobs, runs, samples, uploads


def create_app(config: AppConfig | None = None) -> FastAPI:
    """构造应用；``config`` 省略时按 `load_config()` 读取本机配置。

    建应用时**先迁移再服务**：`Database.initialize()` 幂等，多进程同时启动也安全；
    迁移失败即启动失败，不带着未知结构对外服务。
    """
    app_config = config if config is not None else load_config()

    database = Database(app_config.paths.database)
    database.initialize()

    app = FastAPI(
        title="AIDHU 回答判别 Agent API",
        version=package_version(),
        description="上传、预检、判一条（模拟/真实）、批次创建与任务查询。",
    )
    app.state.config = app_config
    app.state.db = database
    app.state.uploads = UploadStore(database, app_config.paths.uploads)
    app.state.jobs = JudgeJobRegistry(app_config)

    @app.middleware("http")
    async def attach_request_id(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.request_id = uuid.uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):  # type: ignore[no-untyped-def]
        # FastAPI 默认返回 ``{"detail": [...]}``，与 [plan/08 §1]「错误 JSON 使用
        # error 与 request_id」不一致：同一个 422 会因为「谁先发现参数不合法」而
        # 变成两种形状（分页越界由 Schema 拦下、非法状态由服务层拦下）。这里统一成
        # 项目错误信封，只回报字段位置与提示，**不回显收到的值**——请求体里可能有
        # 用户数据。
        problems = [
            {
                "field": ".".join(str(part) for part in problem.get("loc", ())),
                "message": str(problem.get("msg", "")),
            }
            for problem in exc.errors()
        ]
        return fail(request, 422, "PARAM_VALIDATION", "请求参数不合法", problems)

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception):  # type: ignore[no-untyped-def]
        # 不把堆栈或凭据返回给前端；详细信息写日志。
        return fail(request, 500, "INTERNAL_ERROR", "服务内部错误")

    app.include_router(samples.router, prefix="/api")
    app.include_router(uploads.router, prefix="/api")
    app.include_router(jobs.router, prefix="/api")
    app.include_router(runs.router, prefix="/api")
    app.include_router(artifacts.router, prefix="/api")

    dist = app_config.paths.frontend_dist
    if dist.is_dir():
        _mount_frontend(app, dist)

    return app


def _mount_frontend(app: FastAPI, dist: Path) -> None:
    """托管前端构建产物，并把未知路径回落到 `index.html`。

    前端路由用 history 模式，且 S04-07 起有 ``/runs/{run_id}`` 这样的深链接。
    ``StaticFiles(html=True)`` 只对**目录**回落，刷新 ``/runs/abc`` 会得到 404
    JSON；S04-07 的界面要求「重启服务后刷新，批次仍在」，所以这里显式回落。

    回落只针对 GET：静态资源命中真实文件时按文件返回，其余一律给 `index.html`，
    由前端路由决定显示什么。

    **``/api/...`` 必须排除在外**：已注册的接口照常匹配，但拼错的接口路径若回落到
    `index.html`，调用方会收到 200 + HTML，把「接口不存在」伪装成成功。
    """
    root = dist.resolve()

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(request: Request, full_path: str) -> Any:
        if full_path == "api" or full_path.startswith("api/"):
            return fail(request, 404, "NOT_FOUND", "接口不存在")
        if full_path:
            candidate = (root / full_path).resolve()
            # 防目录穿越：只有确实落在构建产物目录里的文件才按文件返回。
            if candidate.is_file() and candidate.is_relative_to(root):
                return FileResponse(candidate)
        index = root / "index.html"
        if not index.is_file():
            # 服务能起来但没得可看：这是部署问题，不是「页面不存在」。
            return fail(
                request, 500, "INTERNAL_ERROR", "前端构建产物缺少 index.html；请先执行 pnpm build"
            )
        return FileResponse(index)


def frontend_dist_path(config: AppConfig | None = None) -> Path:
    """构建产物目录；`serve` 用它提示用户先执行 ``pnpm build``。"""
    return (config if config is not None else load_config()).paths.frontend_dist


__all__ = ["create_app", "frontend_dist_path"]
