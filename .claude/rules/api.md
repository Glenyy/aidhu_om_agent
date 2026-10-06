---
paths:
  - "src/aidhu_om_agent/api/**"
  - "src/aidhu_om_agent/services/**"
  - "tests/integration/**"
---

# 接口约定

请求与响应、错误、幂等和分页以 [API 合同](../../plan/08-API接口与数据合同.md) 为依据。API 接收操作并保存任务；长模型调用由独立 worker 执行。

网页和 CLI 复用服务。处理状态与业务三分类分开；技术失败不生成标签。
