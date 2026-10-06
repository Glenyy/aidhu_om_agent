# 开发辅助配置

rules 目前包含四份路径规则和四份全项目流程/接手/环境规则：

- [frontend](rules/frontend.md)、[api](rules/api.md)、[agent](rules/agent.md)、[storage](rules/storage.md)：对应路径约定。
- [development-workflow](rules/development-workflow.md)：阶段前讨论、步骤批准、范围及状态。
- [review-and-handoff](rules/review-and-handoff.md)：阶段自动化审阅、手动 Markdown 指南与用户结果。
- [agent-handoff](rules/agent-handoff.md)：接手必读、实际进度和交接更新；另有根目录 [AGENTS](../AGENTS.md) 通用入口。
- [development-environment](rules/development-environment.md)：用户创建并激活 Conda 环境，通过 requirements.txt 安装；agent 提供文件和验证说明。

流程规则不设置路径限制，适用于整个项目。入口为 [CLAUDE](../CLAUDE.md)，实施文档见 [docs/implementation](../docs/implementation/README.md)，业务依据仍为 [plan](../plan/README.md)。

skills/database-migration、agents 和 .github/workflows 只预留目录，没有可调用技能、自定义审查角色或 CI 工作流。自动化审阅不依赖这些可选配置。

settings.json/settings.local.json 和 CLAUDE.local.md 未创建，没有设置权限、Hooks 或插件。
