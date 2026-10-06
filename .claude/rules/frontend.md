---
paths:
  - "src/frontend/**"
  - "tests/unit/frontend/**"
  - "tests/e2e/**"
---

# 前端约定

前端通过 /api 展示数据与操作，不直接调用模型。后端返回的状态、判断与证据按文本展示，不能在页面推导业务标签。

根目录使用 pnpm 管理依赖，源码在 src/frontend，构建目标为 dist/frontend。页面及交互细节开发时按需明确。参照 [目录设计](../../plan/07-项目目录与前后端选型.md)。
