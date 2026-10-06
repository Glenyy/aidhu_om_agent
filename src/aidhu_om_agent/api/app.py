"""S03-07：FastAPI 应用与静态页面入口。

本阶段只挂载 S03-07 的最小接口（合成样例下载、上传、预检、判一条、任务查询）与
构建后的前端静态文件；批次、恢复、导出与批次结果下载接口属 S04／S06。

临时性说明：上传登记、预检快照与判别任务都保存在**进程内存**中，进程重启即
失效，本阶段不承诺中断恢复。
"""

from __future__ import annotations

import uuid
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from ..config import AppConfig, load_config
from ..services.judging import JudgeJobRegistry
from ..services.uploads import UploadStore
from .responses import fail
from .routes import jobs, samples, uploads

_PACKAGE = "aidhu-om-agent"


def _package_version() -> str:
    try:
        return version(_PACKAGE)
    except PackageNotFoundError:
        return "0.0.0+unknown"


def create_app(config: AppConfig | None = None) -> FastAPI:
    """构造应用；``config`` 省略时按 `load_config()` 读取本机配置。"""
    app_config = config if config is not None else load_config()

    app = FastAPI(
        title="AIDHU 回答判别 Agent API",
        version=_package_version(),
        description="S03-07 最小接口：上传、预检、判一条（模拟/真实）与任务查询。",
    )
    app.state.config = app_config
    app.state.uploads = UploadStore(app_config.paths.uploads)
    app.state.jobs = JudgeJobRegistry(app_config)

    @app.middleware("http")
    async def attach_request_id(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.request_id = uuid.uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception):  # type: ignore[no-untyped-def]
        # 不把堆栈或凭据返回给前端；详细信息写日志。
        return fail(request, 500, "INTERNAL_ERROR", "服务内部错误")

    app.include_router(samples.router, prefix="/api")
    app.include_router(uploads.router, prefix="/api")
    app.include_router(jobs.router, prefix="/api")

    dist = app_config.paths.frontend_dist
    if dist.is_dir():
        # 必须最后挂载：根路径兜底，交给前端路由。
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="frontend")

    return app


def frontend_dist_path(config: AppConfig | None = None) -> Path:
    """构建产物目录；`serve` 用它提示用户先执行 ``pnpm build``。"""
    return (config if config is not None else load_config()).paths.frontend_dist


__all__ = ["create_app", "frontend_dist_path"]
