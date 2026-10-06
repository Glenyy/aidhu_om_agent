# AIDHU Agent 项目规则入口

项目根目录：D:/pyProjects/dha_projects/aidhu_om_agent。不要在旧项目 rag-zero-master 中实现本项目。

接手必读：

1. [CLAUDE.md](CLAUDE.md)：项目业务边界及通用约定。
2. [docs/HANDOFF.md](docs/HANDOFF.md)：最新实际进度和批准/报告入口。
3. [S01 前置检查与接手说明](docs/implementation/handoffs/S01-前置检查与接手说明.md)：2026-10-06 的只读检查与交接。
4. [阶段执行规则](.claude/rules/development-workflow.md)、[审阅交付规则](.claude/rules/review-and-handoff.md)、[Agent 接手规则](.claude/rules/agent-handoff.md)。
5. [实施入口](docs/implementation/README.md)、当前阶段文件及最新开发前方案/用户批准记录。

Python 环境由用户用 Anaconda 创建，目标 3.12；先读 [环境规则](.claude/rules/development-environment.md) 与 [依赖清单](docs/implementation/handoffs/S01-开发环境与依赖清单.md)。安装入口为根目录 [requirements.txt](requirements.txt)，用户在 Anaconda 控制台激活环境后通过文件安装。requirements.txt 的 36 个包已固定，参见 [依赖版本解析记录](docs/implementation/handoffs/S01-依赖版本解析记录.md)；不要自动升级这些版本。本次 agent 不创建环境或安装依赖，使用用户后续提供的 Conda 解释器。

以上 rules 在本项目作为开发约定执行，同时读取对应路径的 frontend/api/agent/storage 规则。用户最新明确授权优先；已批准范围内不重复索取相同批准。

2026-10-06：用户已创建 Conda 环境 `aidhu_agent`（Python 3.12.15）并装好 requirements.txt 全部 36 项依赖。S01-01—S01-04 已实现并通过[自动化审阅](docs/implementation/reviews/automated/S01/r01-自动化审阅.md)，阶段状态为**待手动审阅**，[手动指南](docs/implementation/reviews/manual/S01/r01-手动操作审阅.md)已生成、**用户结果尚未反馈**。用户同时决定小步骤讨论定稿后直接写入阶段文档，不再新建 preparations 开发前方案。后续实际状态以 docs/HANDOFF、用户新授权和真实报告为准，不把此快照当永久限制。

每阶段开发完成主动自动化审阅，通过后同次交付真实手动操作审阅 Markdown。用户验收单独记录，不能代填。更新交接与实施入口，保留历史检查事实。

当前工程源码与脚本是占位，实际验证后才写运行命令或通过结论。凭据不入前端、日志、文档、配置快照或版本库。
