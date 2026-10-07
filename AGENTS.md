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

2026-10-06：用户已创建 Conda 环境 `aidhu_agent`（Python 3.12.15）并装好 requirements.txt 全部 36 项依赖。用户同时决定小步骤讨论定稿后直接写入阶段文档，不再新建 preparations 开发前方案。**S01、S02、S03 为「阶段已接受」**：S01 单独验收（[用户审阅结果](docs/implementation/reviews/manual/S01/r01-用户审阅结果.md)）；**S02（输入与核心数据合同）与 S03（模型适配与两阶段判别，含界面骨架与模拟模式）曾由用户在同一次界面操作中一并验收通过**（[S02 结果](docs/implementation/reviews/manual/S02/r01-用户审阅结果.md)、[S03 结果](docs/implementation/reviews/manual/S03/r01-用户审阅结果.md)，均注明同一次界面操作），界面版[手动指南](docs/implementation/reviews/manual/S03/r01-手动操作审阅.md)即当轮实际验收路径。**随后 S03 经历一次返工**：用户用自己的真实 `.xlsx` 在**真实模式**判一条得到「约四分钟无结果、技术失败、无标签」，根因是**脏输入 × 严格逐字子串判定 × 提示词未给摘录策略**（**不是真实模式故障**）；用户选择**先不改契约**，返工（R-1—R-6）通过 [r02-自动化审阅](docs/implementation/reviews/automated/S03/r02-自动化审阅.md)（**349 passed / 2 skipped、零真实调用**）后，用户完成 [r02 界面指南](docs/implementation/reviews/manual/S03/r02-手动操作审阅.md) 的 A 组复验（原话「A组已过」）并在真实模式下实跑复验（同一条记录从 3 次全拒/无标签/约 4 分 26 秒 → **第 1 次通过/有标签/1 分 19 秒**；另一条记录 `4` 在阶段一 3 次全拒），**2026-10-06 明确接受「先接受 S03，记录 4 这条归 S04 或后续处理」，S03 恢复「阶段已接受」**（[r02-用户审阅结果](docs/implementation/reviews/manual/S03/r02-用户审阅结果.md)）。**S03-06 的真实调用预算 10 次已用满（返工零调用），再次真实调用需另行授权。** **S04（持久化与任务恢复）的小步骤 S04-01—S04-08 已于 2026-10-06 讨论定稿并经用户原话「批准」批准，已全部实现、自动化审阅通过，并经用户 2026-10-07 界面验证接受（阶段已接受）**（定稿与审批记录见 [S04 阶段文档](docs/implementation/stages/S04-持久化与任务恢复.md) §0/§2/§6；验收结论见 [r01-用户审阅结果](docs/implementation/reviews/manual/S04/r01-用户审阅结果.md)；界面增量为最小批次面，S04 全程零真实调用）。**S05 的前置「S04 已接受」由此满足，但 S05—S09 的小步骤均未讨论、未批准**。后续实际状态以 docs/HANDOFF、用户新授权和真实报告为准，不把此快照当永久限制。

每阶段开发完成主动自动化审阅，通过后同次交付真实手动操作审阅 Markdown。用户验收单独记录，不能代填。更新交接与实施入口，保留历史检查事实。

**2026-10-06 用户决定：手动验证只使用本项目的前端界面验证。** 因此每阶段须交付界面验证面（[审阅交付规则](.claude/rules/review-and-handoff.md) §4），涉及模型的步骤默认走**界面模拟模式**，真实调用单独授权并显著标注；界面就绪前已实现的阶段不单独验收，按 §6 与界面就绪后的阶段一并验（S02 即与 S03 一并验）。**S03 的启动前置相应改为“S02 已实现并通过自动化审阅”**。S02 的 CLI 版手动指南保留原文，仅作排错参考。

工程源码与脚本中，S01—S03 部分（配置、路径、脱敏、Excel 输入解析与预检、两阶段判别与模型适配、最小 API 与前端判别页、模拟模式）已实现并经实跑验证；**S04 已获批准、已实现并通过自动化审阅，2026-10-07 经用户界面验证接受（阶段已接受）**（持久化与 repositories、worker、批次/恢复）；导出与下载、批处理命令仍是占位。**界面已可交付用户手动验证**：`python -m aidhu_om_agent serve` 后浏览器打开 `http://127.0.0.1:8000/`。实际验证后才写运行命令或通过结论，界面内的浏览器点击由用户确认、开发者不代填。凭据不入前端、日志、文档、配置快照或版本库。
