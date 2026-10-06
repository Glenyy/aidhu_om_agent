"""HTTP 接口层（S03-07）：合成样例下载、上传、预检、判一条与任务查询的最小集。"""

from .app import create_app, frontend_dist_path

__all__ = ["create_app", "frontend_dist_path"]
