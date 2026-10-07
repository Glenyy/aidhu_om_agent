# 架构入口

当前实现状态：**S01—S05 已实现并经用户验收（阶段已接受）**——配置与日志、只读输入解析（S02）、两阶段判别与模型适配（S03）、最小 API 与前端判别页（S03-07）、存储与 repositories／worker／批次与恢复（S04，落本机 SQLite）。**S05（导出与命令行闭环）已实现、自动化审阅通过，并经用户界面验证接受**——四表 Excel 与两阶段 JSONL 导出、导出任务与成对发布、导出/下载接口与界面导出面、`run`/`resume`/`export` 命令。**S06（API 与前端完整闭环）已实现、自动化审阅通过，待用户手动审阅**——记录查询接口（列表分页筛选 + 详情，被拒尝试带 `raw_output`）、上传防护与错误合同收口、`GET /api/jobs/{job_id}` 改读库、`last_error` 语义分工与 `/runs` 筛选分页、记录列表与记录详情独立页、首页删除单条判别并改**只读模式指示**、本地开发与静态服务核对；`POST /api/judge` **已删除**（连同首页单条判别），诊断产物改由 worker 失败路径写入（`judge-<job_id>-<record_key>.json`）。业务数据以 SQLite 为准；S03 时代的进程内存态上传/判别链路**已由 S04 落库、S06 移除**。**在线人工改标签与复核回读、`evaluate` 指标计算尚未实现**（属 S07）。

| 路径 | 职责 |
| --- | --- |
| src/frontend | Vue + TypeScript，上传、进度、只读结果与下载 |
| src/aidhu_om_agent/api | FastAPI 网页接口 |
| src/aidhu_om_agent/services | 网页与 CLI 共用的业务操作 |
| src/aidhu_om_agent/repositories | 批次、记录、阶段和任务数据存取 |
| src/aidhu_om_agent/agent | V3 → R1 固定两阶段流程 |
| src/aidhu_om_agent/worker.py | 独立串行队列消费 |
| src/aidhu_om_agent/storage | SQLite 连接、事务和迁移 |
| tests | 后端、前端、集成和浏览器验证 |

拟定架构见 [02 详细设计](../plan/02-架构与详细设计.md)；目录依据见 [07 目录与选型](../plan/07-项目目录与前后端选型.md)。

接口、数据表及恢复分别见 [08 API](../plan/08-API接口与数据合同.md)、[09 数据库](../plan/09-数据库与持久化设计.md)、[10 状态恢复](../plan/10-任务状态与恢复设计.md)。实现之后再补充实际运行入口与版本。
