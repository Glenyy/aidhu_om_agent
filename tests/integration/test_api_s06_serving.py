"""S06-06：本地开发与静态服务两种运行方式下的**回落与错误信封**。

S06 阶段文档 §0.3 第 13 项说静态服务「均已实现，本阶段只核对」，所以这里不做新的
功能设计，只把核对到的事实固化成用例。核对时用**真实 uvicorn** 的构建产物模式发现
了一个只有挂上构建产物才会出现的缺陷，本文件把它钉住：

- 只登记 GET 的回落会让 ``POST /api/judge``（已删除的接口）被判成 **405**，而且响应体
  是 Starlette 默认的 ``{"detail": ...}``，不是 [plan/08 §1] 的 ``error`` + ``request_id``
  信封；同一个已删除接口在「有构建产物」与「没有构建产物」两种部署下还会给出不同状态码。
  收口方式：回落登记**全部方法**（``/api/...`` 一律 404 信封）、静态文件仍只对 GET/HEAD
  按文件返回，并给框架级 HTTP 异常补信封处理器。

全部用例走内存 SQLite、不启动进程、**零真实模型调用**。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from test_api_s03 import make_config
from test_api_s04_reads import build_frontend

from aidhu_om_agent.api.app import create_app


def without_dist(tmp_path: Path) -> TestClient:
    """没有构建产物的部署（等价于开发时只跑后端 + vite 代理）。"""
    return TestClient(create_app(make_config(tmp_path)))


def assert_envelope(response, code: str) -> None:  # noqa: ANN001 - TestClient 的响应
    """响应是项目错误信封：有 ``error.code`` 与 ``request_id``，且不是 HTML。"""
    assert response.headers["content-type"].startswith("application/json")
    payload = response.json()
    assert payload["error"]["code"] == code
    assert payload["request_id"]
    assert "data" not in payload
    assert "marker" not in response.text


# ------------------------------- 深链接刷新（S04-07 的回落，S06 新增记录页同样依赖）


def test_record_detail_deep_link_is_served_by_the_spa_fallback(tmp_path: Path) -> None:
    """刷新 ``/runs/{run}/records/{key}`` 必须仍能打开页面（S06-06 的核对点）。"""
    _, client = build_frontend(tmp_path)

    deep = client.get("/runs/" + "a" * 32 + "/records/" + "b" * 32)

    assert deep.status_code == 200 and "marker" in deep.text


def test_record_detail_deep_link_is_served_without_dist_too(tmp_path: Path) -> None:
    """没有构建产物时不挂回落：这是开发模式，页面由 vite 提供，接口照常 404 信封。"""
    client = without_dist(tmp_path)

    assert client.get("/api/runs").status_code == 200


# ------------------------------------------- 已删除的 /api/judge 与未知接口的动词


def test_deleted_judge_endpoint_is_404_with_the_envelope_in_built_mode(tmp_path: Path) -> None:
    """挂着构建产物时，``POST /api/judge`` 也是 404 信封（曾经是 405 + ``detail``）。"""
    _, client = build_frontend(tmp_path)

    response = client.post("/api/judge", json={"validation_id": "x", "record_key": "y"})

    assert response.status_code == 404
    assert_envelope(response, "NOT_FOUND")


def test_deleted_judge_endpoint_is_404_without_dist_too(tmp_path: Path) -> None:
    """没挂构建产物时也是 404：同一个已删除接口不随部署方式改变状态码。"""
    response = without_dist(tmp_path).post("/api/judge", json={})

    assert response.status_code == 404
    assert_envelope(response, "NOT_FOUND")


def test_unknown_api_path_answers_every_verb_with_the_envelope(tmp_path: Path) -> None:
    """拼错的接口路径不允许任何动词翻到 HTML 或漏出 ``{"detail": ...}``。"""
    _, client = build_frontend(tmp_path)

    for call in (client.get, client.post, client.put, client.delete):
        response = call("/api/nope")
        assert response.status_code == 404, call.__name__
        assert_envelope(response, "NOT_FOUND")


def test_api_prefix_itself_is_not_served_as_a_page(tmp_path: Path) -> None:
    _, client = build_frontend(tmp_path)

    response = client.get("/api")

    assert response.status_code == 404
    assert_envelope(response, "NOT_FOUND")


def test_registered_post_route_is_not_shadowed_by_the_fallback(tmp_path: Path) -> None:
    """回落登记了 POST，但已注册的 ``POST /api/runs`` 必须仍然优先（顺序不能错）。"""
    _, client = build_frontend(tmp_path)

    response = client.post("/api/runs", json={})

    # 参数不合法仍然是接口自己的 422 信封，不是回落的 404。
    assert response.status_code == 422
    assert_envelope(response, "PARAM_VALIDATION")


# ------------------------------------------------- 框架级 HTTP 异常也走同一信封


def test_framework_method_mismatch_uses_the_envelope(tmp_path: Path) -> None:
    """``PUT /api/runs``：路径存在、方法不对 → 405，但形状必须是项目信封。

    没挂构建产物时才会出现 405（挂着时回落自己接住、给 404），所以这里用
    `without_dist` 构造。
    """
    response = without_dist(tmp_path).put("/api/runs")

    assert response.status_code == 405
    assert_envelope(response, "METHOD_NOT_ALLOWED")


def test_page_and_assets_are_read_only_even_with_the_fallback(tmp_path: Path) -> None:
    """页面与静态资源只有读语义：写请求给 405 信封，不发 HTML、也不发文件。"""
    _, client = build_frontend(tmp_path)

    assert client.get("/assets/app.js").status_code == 200
    for call in (client.post, client.put, client.delete):
        response = call("/assets/app.js")
        assert response.status_code == 405, call.__name__
        assert_envelope(response, "METHOD_NOT_ALLOWED")
