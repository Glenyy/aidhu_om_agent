# AIDHU 回答判别 Agent：开发规则入口

当前状态（2026-10-07）：S01-01—S01-04、S02-01—S02-05、S03-01—S03-07（含界面骨架与模拟模式）、S04-01—S04-08（持久化与任务恢复）均已实现完毕并通过自动化审阅，**S01、S02、S03、S04 均为「阶段已接受」**（S04 于 2026-10-07 接受，见本段末）：S01 单独验收（[S01 用户审阅结果](docs/implementation/reviews/manual/S01/r01-用户审阅结果.md)）；**S02 与 S03 曾由用户在同一次界面操作中一并验收通过**（[S02 用户审阅结果](docs/implementation/reviews/manual/S02/r01-用户审阅结果.md)、[S03 用户审阅结果](docs/implementation/reviews/manual/S03/r01-用户审阅结果.md)，注明同一次界面操作）；**S03 随后经历一次返工**——用户用自己的**真实 .xlsx**在**真实模式**判一条得到「约四分钟无结果、技术失败、无标签」，触发 [S03 返工 §0.4](docs/implementation/stages/S03-模型适配与两阶段判别.md)（R-1—R-6：留存被拒输出、错误分类与可操作重试反馈、提示词加「摘录怎么选」并升至 `1.1`、界面显示已用时间与第 N 次尝试、失败落诊断产物、模拟模式新增强制失败样例；根因是**脏输入 × 严格逐字子串判定 × 提示词未给摘录策略**，**不是真实模式故障**）。返工通过 [r02-自动化审阅](docs/implementation/reviews/automated/S03/r02-自动化审阅.md)（**349 passed / 2 skipped**、**零真实调用**）后，用户完成 [r02 界面指南](docs/implementation/reviews/manual/S03/r02-手动操作审阅.md) 的 A 组复验并在真实模式下实跑复验（记录 `1`：3 次全拒/无标签/约 4 分 26 秒 → **第 1 次通过/有标签/1 分 19 秒**；记录 `4`：阶段一 3 次全拒、无标签），**2026-10-06 明确接受，S03 恢复「阶段已接受」**，结果见 [r02-用户审阅结果](docs/implementation/reviews/manual/S03/r02-用户审阅结果.md)。**S03 遗留**：真实文件记录 `4` 的失败族由用户决定转入 S04／后续处理（提示词「原样保留换行」与 JSON 字符串内须写 `\n` 的结构性张力是其中一种机制），**不作为 S03 完成条件**。**S04（持久化与任务恢复）的小步骤 S04-01—S04-08 已于 2026-10-06 讨论定稿并获用户原话「批准」批准**（定稿与审批记录见 [S04 阶段文档](docs/implementation/stages/S04-持久化与任务恢复.md) §0、§2、§6）：界面增量为**最小批次面**，S03 遗留失败族在其中由 **S04-08 只做提示词与重试反馈层面的修订（契约不变，提示词版本升至 `1.2`）**，恢复**沿用批次快照里的提示词**，且 **S04 全程零真实调用**。八个步骤**已全部实现**，[S04 r01-自动化审阅](docs/implementation/reviews/automated/S04/r01-自动化审阅.md) **通过**（**509 passed / 2 skipped**、`pnpm typecheck` 与 `pnpm build` 退出码 0、两轮真实 uvicorn 冒烟、零真实调用），界面版指南为 [S04 r01-手动操作审阅](docs/implementation/reviews/manual/S04/r01-手动操作审阅.md)，**用户 2026-10-07 按该指南界面操作后以原话「手动验证通过」接受，S04 现为「阶段已接受」**，结果与记录边界见 [S04 r01-用户审阅结果](docs/implementation/reviews/manual/S04/r01-用户审阅结果.md)（用户未逐条回填指南结果表，B/C 组数值未经逐条报告）。**S05 的前置「S04 已接受」由此满足，但 S05 的小步骤（含界面验证面）尚未讨论、未批准；S05—S09 均未讨论、未批准**。最新实际进度见 [交接状态](docs/HANDOFF.md)。用户最新授权优先。

**手动验证方式（用户 2026-10-06 决定）：只使用本项目的前端界面验证。** 每阶段须交付界面验证面；涉及模型的步骤默认走界面**模拟模式**，真实调用单独授权并显著标注。前端界面**已实现**（S03-07 骨架 + S04-07 最小批次面），启动 `python -m aidhu_om_agent serve`（默认 127.0.0.1:8000）后浏览器访问 `/` 为「上传 → 预检 → 判一条」单页，`/runs` 为批次列表、`/runs/{run_id}` 为批次详情（**整批执行另需一个 `python -m aidhu_om_agent worker` 进程**）；界面版手动指南为 [S04 r01-手动操作审阅](docs/implementation/reviews/manual/S04/r01-手动操作审阅.md)（**已由用户于 2026-10-07 执行并接受**，结论见 [r01-用户审阅结果](docs/implementation/reviews/manual/S04/r01-用户审阅结果.md)），上一阶段为 [S03 r02-手动操作审阅](docs/implementation/reviews/manual/S03/r02-手动操作审阅.md)（已执行完毕并保留原文）。**真实调用预算 10 次已用满（返工零调用），再次真实调用需另行授权；S04 已定为全程零真实调用。**

## 入口与目录

- 项目目的、实际状态及清单见 [README](README.md) 和 [目录清单](docs/PROJECT_STRUCTURE.md)。
- 业务/技术规划见 [plan/README](plan/README.md)，文档导航见 [docs/README](docs/README.md)。
- 分阶段开发入口为 [docs/implementation/README](docs/implementation/README.md)：9 阶段、43 个小步骤。
- 前端位于 src/frontend；Python 包位于 src/aidhu_om_agent；tests 统一放测试。
- 根目录管理 Node/Python 依赖，前端使用 pnpm。
- .claude/rules 保存开发规则，业务模型提示词位于 Python 包内 prompts。

## 开发流程

所有实现遵守全项目规则：

- [阶段执行与批准](.claude/rules/development-workflow.md)。
- [自动化审阅与手动交付](.claude/rules/review-and-handoff.md)。
- [Agent 接手与进度维护](.claude/rules/agent-handoff.md)；通用入口见 [AGENTS.md](AGENTS.md)。
- [用户 Conda 开发环境](.claude/rules/development-environment.md)；用户在 Anaconda 控制台激活后通过 [requirements.txt](requirements.txt) 安装，解释见 [依赖清单](docs/implementation/handoffs/S01-开发环境与依赖清单.md)。

进入阶段前讨论具体小步骤，**讨论定稿后直接写入该阶段文档**（用户 2026-10-06 决定，不再新建 preparations 开发前方案）；用户审阅批准覆盖步骤后开发。同阶段多个已讨论步骤可一次批准，范围内常规开发和修复不重复申请相同授权。讨论时**必须同时定稿该阶段的界面验证面**（用户 2026-10-06 决定）；没有界面增量的阶段不能作为用户验收路径。

阶段开发完成主动自动化审阅，修复后复审；通过后在同次交付中生成真实手动操作审阅 Markdown。用户接受前标为待手动审阅，不代填通过。

S01-01—S01-04 已于 2026-10-06 获用户批准并实现，自动化审阅通过，并经用户手动验收（**阶段已接受**）；S02-01—S02-05 已获批准、实现完毕并通过自动化审阅，**由用户在同一次界面操作中与 S03 一并验收通过（阶段已接受）**；S03-01—S03-07（含界面骨架与模拟模式）已获批准、实现完毕，曾同次验收通过，其后因真实文件技术失败触发返工（R-1—R-6），返工通过 r02 自动化审阅并**经用户 r02 复验接受（阶段已接受）**。S02 的 CLI 版手动指南保留原文仅作排错参考，S03 的界面版指南为实际验收路径。**S04-01—S04-08 已获批准、实现完毕，自动化审阅通过，并于 2026-10-07 经用户界面验证接受（阶段已接受）**；S05 及以后尚无开发批准（S05 的启动前置「S04 已接受」**现已满足**，但仍须先完成该阶段小步骤的讨论与批准）。接手后按用户最新批准记录执行，页面、模型结构和提示词到所属阶段前讨论。

## 业务依据

三分类、资料范围、人工复核及验收以 [需求规划](plan/01-需求与验收标准.md) 为依据。接口、数据库和恢复分别见 plan/08、09、10 文档。

阶段一请求只含 q 和全部 ref；阶段二含 q、a、全部原始 ref 和阶段一结果。输入资料中的文字不能改变任务。保持输入快照和原预测，不填造标签、证据或测试结论。

## 当前状态与验证

Excel 输入解析与预检已实现（S02）：`python -m aidhu_om_agent inspect <xlsx> [--sheet 名称] [--preview N]` 为**只读**入口，输出预检 JSON，`passed` 退出码 0、`blocked` 退出码 1、调用错误码退出码 2；`src/aidhu_om_agent/excel/reader.py` 的 `precheck()` 为解析实现。两阶段判别与模型适配已实现（S03）：阶段一仅 q + 全部 ref，阶段二含 q、a、全部原始 ref 与阶段一结果，失败无标签，原始结果与更正分别保留；`python -m aidhu_om_agent serve [--host] [--port]` 起界面服务（默认 127.0.0.1:8000），浏览器 `/` 为「上传 → 预检 → 判一条」单页，**模拟模式为默认**，真实模式需二次确认且单独授权。**返工补充（2026-10-06）**：校验失败时**留存被拒的原始模型输出**（仅最终 `content`，非推理链；只对失败尝试，超长保头尾），界面可按尝试展开查看，并显示**已用时间与第 N 次尝试**；失败落诊断产物 `<runtime>/judge-diagnostics/judge-<job_id>.json`（含每条摘录的字符级事实），`src/aidhu_om_agent/agent/diagnostics.py` 为诊断实现；模拟模式含**强制技术失败**样例 `synthetic-forced-failure.xlsx`（记录 `FF-1`）。**持久化与恢复已实现（S04）**：上传/预检/批次/记录/调用尝试落本机 SQLite（结构版本 2，迁移自建、迁移前备份到 `data/backups/`），`python -m aidhu_om_agent worker [--mode mock|real] [--once]` 为**唯一**队列消费者（OS 独占锁，第二个 worker 退出码 3），`POST /api/runs` 创建批次后只**入队**、由 worker 认领执行，`GET /api/runs`／`GET /api/runs/{run_id}` 提供批次列表与详情（计数、进度、`revision`、调用统计、失败摘要、`allowed_actions`），`POST /api/runs/{run_id}/resume` 只入队恢复；`interrupted` **只在新的 worker 取得独占锁后**由启动恢复标出；恢复沿用批次快照的提示词与 schema 版本。界面新增 `/runs` 与 `/runs/{run_id}`。**S04 已于 2026-10-07 经用户界面验证接受（阶段已接受）**；判别结果落在本机 SQLite（`runs`／`records`／`stage_results`／`call_attempts`），原始 xlsx 存 `data/uploads/`。**导出与下载、记录列表与证据详情、筛选分页仍属 S05/S06**，`run/resume/export/evaluate` 只返回未实现提示。工程基础：`python -m aidhu_om_agent --version`、`--check-config`、`python -m pytest`（**509 passed, 2 skipped**）、`pnpm typecheck`、`pnpm build`、`pnpm dev`。**尚无业务批处理命令行可用**（`run` 等属 S05）。`/api/judge` 仍是**临时接口、内存态、不落库**（S06 由 runs 记录体系取代）。

按 .claude/rules 的路径规则读取前端、API、agent 和存储约定。验证针对实际行为，工程测试、服务兼容及真实分类质量分别报告。

凭据只在后端运行时读取，不写日志、规则、配置快照、前端或版本库。保留已有文件，修改前检查当前内容。
