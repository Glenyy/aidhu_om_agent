---
paths:
  - "src/aidhu_om_agent/repositories/**"
  - "src/aidhu_om_agent/storage/**"
  - "src/aidhu_om_agent/worker.py"
---

# 存储与恢复约定

数据表和短事务以 [持久化设计](../../plan/09-数据库与持久化设计.md) 为依据，状态与预算以 [恢复设计](../../plan/10-任务状态与恢复设计.md) 为依据。

模型请求与文件生成不占数据库写事务。检查点、尝试次数和输入快照可追踪；恢复不清空预算。导出完整后登记文件，导出失败不改变分类结果。
