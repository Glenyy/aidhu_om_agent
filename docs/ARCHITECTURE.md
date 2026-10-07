# 架构入口

当前实现状态：**S01—S07 已实现并经用户验收（阶段已接受；S07 实现 S07-01—S07-03，S07-04 未实现）**——配置与日志、只读输入解析（S02）、两阶段判别与模型适配（S03）、最小 API 与前端判别页（S03-07）、存储与 repositories／worker／批次与恢复（S04，落本机 SQLite）。**S05（导出与命令行闭环）已实现、自动化审阅通过，并经用户界面验证接受**——四表 Excel 与两阶段 JSONL 导出、导出任务与成对发布、导出/下载接口与界面导出面、`run`/`resume`/`export` 命令。**S06（API 与前端完整闭环）已实现、自动化审阅通过，并经用户界面验证接受（2026-10-07）**——记录查询接口（列表分页筛选 + 详情，被拒尝试带 `raw_output`）、上传防护与错误合同收口、`GET /api/jobs/{job_id}` 改读库、`last_error` 语义分工与 `/runs` 筛选分页、记录列表与记录详情独立页、首页删除单条判别并改**只读模式指示**、本地开发与静态服务核对；`POST /api/judge` **已删除**（连同首页单条判别），诊断产物改由 worker 失败路径写入（`judge-<job_id>-<record_key>.json`）。业务数据以 SQLite 为准；S03 时代的进程内存态上传/判别链路**已由 S04 落库、S06 移除**。**S07（人工基准与规则校准）的 S07-01—S07-03 已实现、自动化审阅通过，并经用户 2026-10-07 界面验证接受（阶段已接受）**——人工核定数据合同与校验（`evaluation/gold.py`）、固定分层划分（`evaluation/split.py`）、指标计算与 `evaluate` 及评估落库（`evaluation/metrics.py`、`services/evaluation.py`、`repositories/evaluations.py`、`storage/migrations/003_evaluations.sql` 升 `SCHEMA_VERSION 2 → 3`）、三个**只读**评估接口与**只读评估页** `/runs/{run_id}/evaluation`；**S07-04（校准并冻结规则版本）未实现**——其真实模型调用预算不在批准范围内，**保留集全程一次不跑、本轮不产出任何准确率结论**（演示数据为合成数据、预测取自模拟模式）。**在线人工改标签与复核回读仍属 S07 之后**。

| 路径 | 职责 |
| --- | --- |
| src/frontend | Vue + TypeScript，上传、进度、只读结果与下载 |
| src/aidhu_om_agent/api | FastAPI 网页接口 |
| src/aidhu_om_agent/services | 网页与 CLI 共用的业务操作 |
| src/aidhu_om_agent/repositories | 批次、记录、阶段和任务数据存取 |
| src/aidhu_om_agent/evaluation | 人工核定数据、固定分层划分与质量指标计算（S07） |
| src/aidhu_om_agent/agent | V3 → R1 固定两阶段流程 |
| src/aidhu_om_agent/worker.py | 独立串行队列消费 |
| src/aidhu_om_agent/storage | SQLite 连接、事务和迁移 |
| tests | 后端、前端、集成和浏览器验证 |

拟定架构见 [02 详细设计](../plan/02-架构与详细设计.md)；目录依据见 [07 目录与选型](../plan/07-项目目录与前后端选型.md)。

接口、数据表及恢复分别见 [08 API](../plan/08-API接口与数据合同.md)、[09 数据库](../plan/09-数据库与持久化设计.md)、[10 状态恢复](../plan/10-任务状态与恢复设计.md)。实现之后再补充实际运行入口与版本。
