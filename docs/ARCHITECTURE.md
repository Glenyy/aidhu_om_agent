# 架构入口

当前实现状态：目录与占位文件已创建；API、worker、模型调用、前端页面和存储尚未实现。

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
