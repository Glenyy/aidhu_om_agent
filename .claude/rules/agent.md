---
paths:
  - "src/aidhu_om_agent/agent/**"
  - "src/aidhu_om_agent/llm/**"
  - "src/aidhu_om_agent/prompts/**"
  - "src/aidhu_om_agent/schemas/**"
---

# 判别约定

按 [需求](../../plan/01-需求与验收标准.md) 和 [阶段设计](../../plan/02-架构与详细设计.md) 执行。阶段一仅 q/ref；阶段二包含 a 和全部原 ref，保留阶段一原结果。

只允许既定三分类，不重新检索。证据必须定位原文；资料不足和不确定按业务规则处理。阶段执行器统一重试，SDK 不叠加重试。当前提示词仅占位，正式内容在开发时完善。
