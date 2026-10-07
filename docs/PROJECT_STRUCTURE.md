# 实际目录与生成清单

更新日期：2026-10-07。基线：v2.3 规划；实施入口 v2.9，S01 阶段文档 v1.3，S02 阶段文档 v1.3，S03 阶段文档 v1.6，**S04 阶段文档 v2.4**，**S05 阶段文档 v2.2**，**S06 阶段文档 v2.1**，其余阶段 v1.0。状态：**S01、S02、S03、S04、S05 五个阶段均已实现、通过自动化审阅并经用户验收（阶段已接受）**——S02 与 S03 由用户在同一次界面操作中一并验收，S03 另有返工轮次（R-1—R-6）经 r02 自动化审阅与用户复验后接受；**S04 的小步骤 S04-01—S04-08 已讨论定稿并获用户批准，已全部实现、自动化审阅通过，并于 2026-10-07 经用户界面验证接受**；**S05（导出与命令行闭环）的小步骤含界面验证面已于 2026-10-07 讨论定稿并直接写入其阶段文档，经用户原话「直接批准」批准，S05-01—S05-06 已全部实现、[r01-自动化审阅](implementation/reviews/automated/S05/r01-自动化审阅.md) 通过（594 passed / 2 skipped、零真实调用）并交付界面版手动指南，**用户 2026-10-07 按该指南界面操作后以原话「手动验证通过」接受，现为「阶段已接受」（阶段文档 v2.2）**，结果与边界见 [r01-用户审阅结果](implementation/reviews/manual/S05/r01-用户审阅结果.md)**；**S06（API 与前端完整闭环）的小步骤含界面验证面（§2.1）已于 2026-10-07 讨论定稿并直接写入其阶段文档（v1.3 → v2.0），经用户原话「确认无误，批准开始」批准；S06-01—S06-06 已全部实现，[r01-自动化审阅](implementation/reviews/automated/S06/r01-自动化审阅.md) 通过（637 passed / 2 skipped、零真实调用），并交付 [界面版手动指南](implementation/reviews/manual/S06/r01-手动操作审阅.md)，阶段文档升至 v2.1，现处「待手动审阅」（全程零真实调用）**，S07—S09 未开始、未批准。

初次骨架新增 50 个目录、94 个文件（含 23 个 .gitkeep）。2026-10-05 追加实施阶段、模板和规则；2026-10-06 追加交接检查、用户 Conda 依赖清单、AGENTS 入口及接手/环境规则；同日实现 S01（后端配置/日志、前端骨架、单测与构建产物、锁文件）、生成自动化与手动审阅文档、记录用户验收结论，并将项目纳入 Git；同日完成 S02 输入解析与预检实现、生成 S02 自动化与手动审阅文档；同日按用户要求把手动验证改为**只经前端界面**，相应更新审阅与阶段执行规则、调整 S03 范围（新增界面骨架与模拟模式）并同步各状态文档；同日实现 S03（模型适配、两阶段判别、提示词、重试与预算、模拟模式、界面骨架与样例下载接口），在批准预算内完成真实服务兼容实测（10 次调用），生成 S03 自动化审阅报告与**界面版**手动指南（一次操作一并验收 S02 与 S03）。原 plan 保持不变。

当前项目根目录之下（不含根目录自身）共 67 个目录、164 个文件，其中 26 个 .gitkeep、13 个原 plan 文件；计数不含 .git、node_modules、.pytest_cache 与 __pycache__，含本机 `memory/` 记忆目录、`dist/frontend` 构建产物与 `outputs/s02_samples/` 合成样例（后两者不进版本库）。下树展示实际路径，隐藏 .gitkeep；空目录不表示功能已实现。

## 实际目录

~~~text
aidhu_om_agent/
├── .claude/
│   ├── agents/
│   ├── rules/
│   │   ├── agent-handoff.md
│   │   ├── agent.md
│   │   ├── api.md
│   │   ├── development-environment.md
│   │   ├── development-workflow.md
│   │   ├── frontend.md
│   │   ├── review-and-handoff.md
│   │   └── storage.md
│   ├── skills/
│   │   └── database-migration/
│   │       ├── references/
│   │       └── scripts/
│   └── README.md
├── .github/
│   └── workflows/
├── configs/
│   └── config.example.toml
├── data/
│   ├── runtime/
│   └── uploads/
├── dist/
│   └── frontend/
│       ├── assets/
│       │   ├── index-*.css
│       │   └── index-*.js
│       └── index.html
├── docs/
│   ├── adr/
│   ├── implementation/
│   │   ├── handoffs/
│   │   │   ├── S01-依赖版本解析记录.md
│   │   │   ├── S01-前置检查与接手说明.md
│   │   │   └── S01-开发环境与依赖清单.md
│   │   ├── preparations/
│   │   ├── reviews/
│   │   │   ├── automated/
│   │   │   │   ├── S01/
│   │   │   │   │   └── r01-自动化审阅.md
│   │   │   │   ├── S02/
│   │   │   │   │   └── r01-自动化审阅.md
│   │   │   │   └── S03/
│   │   │   │       ├── r01-自动化审阅.md
│   │   │   │       └── r02-自动化审阅.md
│   │   │   └── manual/
│   │   │       ├── S01/
│   │   │       │   ├── r01-手动操作审阅.md
│   │   │       │   └── r01-用户审阅结果.md
│   │   │       ├── S02/
│   │   │       │   ├── r01-手动操作审阅.md
│   │   │       │   └── r01-用户审阅结果.md
│   │   │       └── S03/
│   │   │           ├── r01-手动操作审阅.md
│   │   │           ├── r01-用户审阅结果.md
│   │   │           ├── r02-手动操作审阅.md
│   │   │           └── r02-用户审阅结果.md
│   │   ├── stages/
│   │   │   ├── S01-工程基础与运行环境.md
│   │   │   ├── S02-输入与核心数据合同.md
│   │   │   ├── S03-模型适配与两阶段判别.md
│   │   │   ├── S04-持久化与任务恢复.md
│   │   │   ├── S05-导出与命令行闭环.md
│   │   │   ├── S06-API与前端完整闭环.md
│   │   │   ├── S07-人工基准与规则校准.md
│   │   │   ├── S08-保留集验收与工程回归.md
│   │   │   └── S09-交付与运行复验.md
│   │   ├── templates/
│   │   │   ├── 开发前方案模板.md
│   │   │   ├── 手动操作审阅模板.md
│   │   │   └── 自动化审阅报告模板.md
│   │   └── README.md
│   ├── plans/
│   ├── ARCHITECTURE.md
│   ├── CONVENTIONS.md
│   ├── HANDOFF.md
│   ├── PROJECT_STRUCTURE.md
│   ├── README.md
│   ├── REQUIREMENTS.md
│   └── TESTING.md
├── logs/
├── outputs/
│   └── s02_samples/
├── plan/
│   ├── assets/
│   │   ├── AIDHU回答判别Agent流程图-v1.0.png
│   │   └── AIDHU回答判别Agent流程图-v1.0.svg
│   ├── 01-需求与验收标准.md
│   ├── 02-架构与详细设计.md
│   ├── 03-开发任务与里程碑.md
│   ├── 04-测试与质量评估.md
│   ├── 05-交付与运行指南.md
│   ├── 06-维护与迭代计划.md
│   ├── 07-项目目录与前后端选型.md
│   ├── 08-API接口与数据合同.md
│   ├── 09-数据库与持久化设计.md
│   ├── 10-任务状态与恢复设计.md
│   └── README.md
├── scripts/
│   ├── dev.ps1
│   ├── start-local.ps1
│   └── stop-local.ps1
├── src/
│   ├── aidhu_om_agent/
│   │   ├── agent/
│   │   │   ├── __init__.py
│   │   │   ├── diagnostics.py
│   │   │   ├── mock_samples.py
│   │   │   ├── pipeline.py
│   │   │   ├── stage1.py
│   │   │   ├── stage2.py
│   │   │   └── validation.py
│   │   ├── api/
│   │   │   ├── routes/
│   │   │   │   ├── __init__.py
│   │   │   │   ├── artifacts.py
│   │   │   │   ├── jobs.py
│   │   │   │   ├── runs.py
│   │   │   │   ├── samples.py
│   │   │   │   └── uploads.py
│   │   │   ├── __init__.py
│   │   │   ├── app.py
│   │   │   ├── responses.py
│   │   │   └── schemas.py
│   │   ├── evaluation/
│   │   │   ├── __init__.py
│   │   │   ├── metrics.py
│   │   │   └── split.py
│   │   ├── excel/
│   │   │   ├── __init__.py
│   │   │   ├── reader.py
│   │   │   ├── samples.py
│   │   │   └── writer.py
│   │   ├── llm/
│   │   │   ├── __init__.py
│   │   │   ├── client.py
│   │   │   └── errors.py
│   │   ├── prompts/
│   │   │   ├── stage1.md
│   │   │   └── stage2.md
│   │   ├── repositories/
│   │   │   ├── __init__.py
│   │   │   ├── jobs.py
│   │   │   ├── records.py
│   │   │   └── runs.py
│   │   ├── schemas/
│   │   │   ├── __init__.py
│   │   │   ├── analysis.py
│   │   │   ├── judgement.py
│   │   │   └── qa.py
│   │   ├── services/
│   │   │   ├── __init__.py
│   │   │   ├── batches.py
│   │   │   ├── exports.py
│   │   │   ├── judging.py
│   │   │   └── uploads.py
│   │   ├── storage/
│   │   │   ├── migrations/
│   │   │   ├── __init__.py
│   │   │   ├── database.py
│   │   │   └── schema.sql
│   │   ├── __init__.py
│   │   ├── __main__.py
│   │   ├── cli.py
│   │   ├── config.py
│   │   └── worker.py
│   └── frontend/
│       ├── api/
│       │   └── client.ts
│       ├── components/
│       ├── composables/
│       ├── pages/
│       │   └── JudgeView.vue
│       ├── router/
│       │   └── index.ts
│       ├── styles/
│       │   └── main.css
│       ├── types/
│       │   └── api.ts
│       ├── App.vue
│       ├── env.d.ts
│       ├── index.html
│       └── main.ts
├── tests/
│   ├── e2e/
│   ├── fixtures/
│   │   └── excel_samples.py
│   ├── integration/
│   │   ├── test_api_s03.py
│   │   └── test_real_service_s03.py
│   ├── conftest.py
│   └── unit/
│       ├── backend/
│       │   ├── test_config.py
│       │   ├── test_excel_reader.py
│       │   ├── test_excel_samples.py
│       │   ├── test_judging.py
│       │   ├── test_llm_client.py
│       │   ├── test_mock_samples.py
│       │   ├── test_model_output_parsing.py
│       │   ├── test_pipeline.py
│       │   ├── test_qa_schema.py
│       │   ├── test_stage_messages.py
│       │   └── test_validation.py
│       └── frontend/
├── .env.example
├── .gitignore
├── AGENTS.md
├── CLAUDE.md
├── package.json
├── pnpm-lock.yaml
├── pnpm-workspace.yaml
├── pyproject.toml
├── README.md
├── requirements.in
├── requirements.txt
├── tsconfig.json
└── vite.config.ts
~~~

## 文件状态

| 类别 | 状态 |
| --- | --- |
| README、AGENTS、CLAUDE、docs 导航 | 已同步 S01/S02/S03 的**阶段已接受**结论（S03 含返工与 r02 复验接受）、**手动验证改为只经前端界面的决定**、用户环境决定和 Git 仓库事实（公开远程已推送 `ae427fb`，第二次提交含 S02/S03） |
| docs/implementation/stages | 9 份阶段文档、43 个小步骤；S01 为 v1.3 且阶段已接受，S02 为 v1.3 且阶段已接受，S03 为 v1.6 且 S03-01—S03-07（含 §0.2、§0.3 追加项与 §0.4 返工）已实现、经返工复验后阶段已接受，**S04 为 v2.4 且 S04-01—S04-08 已获批准、实现完毕、自动化审阅通过并经用户 2026-10-07 界面验证接受（阶段已接受）**，**S05 为 v2.2 且 S05-01—S05-06 已于 2026-10-07 讨论定稿并以用户原话「直接批准」批准、实现完毕、自动化审阅通过（594 passed / 2 skipped、零真实调用）并经用户界面验证接受（阶段已接受；含界面验证面 §2.1：最小导出面）**，**S06 为 v2.1 且 S06-01—S06-06（含界面验证面 §2.1）已于 2026-10-07 讨论定稿、以用户原话「确认无误，批准开始」批准、**已全部实现**、自动化审阅通过（637 passed / 2 skipped、零真实调用）并交付界面版手动指南，现处「待手动审阅」（§0.1 保留由 S04 验收带出的三项待决项；§0.2 为本轮七项用户决定——删除 `/api/judge` 与首页单条判别、记录详情独立路由、上传防护补齐、不引入新依赖、被拒原文与诊断产物承接、模式开关改只读指示；最小导出面已提前到 S05；**全程零真实调用**）**，其余未批准开发 |
| docs/implementation/handoffs | S01 前置检查、Conda 依赖说明与版本解析记录；不是阶段完成报告 |
| docs/implementation/templates | 开发前方案（已弃用）、自动化报告、手动指南三份模板 |
| reviews/automated/S01、reviews/manual/S01 | r01 自动化审阅报告（通过）、r01 手动指南（结果表用户未逐项填写）、r01 用户审阅结果（**接受**） |
| reviews/automated/S02、reviews/manual/S02 | r01 自动化审阅报告（**通过**，含 3 项披露与 1 项 S01 遗留问题）、r01 手动指南（11 步，命令均已实跑，**已标注被界面验证取代、仅作排错参考**）；用户结果为 [r01-用户审阅结果](implementation/reviews/manual/S02/r01-用户审阅结果.md)（**接受**，2026-10-06，与 S03 同一次界面操作） |
| reviews/automated/S03、reviews/manual/S03 | r01 自动化审阅报告（**通过**；含真实调用 10/10 次披露、1 项非阻塞遗留与 2 项同轮复审，其一为用户界面操作后修复的终态显示文案）、r01 手动指南（**界面版 14 步**，A 组复验 S02、B 组复验 S03，默认全程模拟模式）、r01 用户审阅结果（**接受**，与 S02 同一次界面操作）；另有 r02 自动化审阅（返工 R-1—R-6，**349 passed / 2 skipped、零真实调用**）与 **r02 界面版手动指南**、r02 用户审阅结果（**接受**） |
| reviews/automated/S04、reviews/manual/S04 | r01 自动化审阅报告（**通过**；509 passed / 2 skipped、两轮真实 uvicorn 冒烟、零真实调用）、r01 手动指南（**界面版**，最小批次面，默认模拟模式）、r01 用户审阅结果（**接受**，用户原话「手动验证通过」；指南结果表用户未逐条回填） |
| reviews/automated/S05、reviews/manual/S05 | r01 自动化审阅报告（**通过**；**594 passed / 2 skipped**、`pnpm typecheck` 与 `pnpm build` 退出码 0、两轮真实 uvicorn 冒烟〔导出→下载→打开核对、中断批次导出→恢复→再自动导出〕、**零真实调用**；含 1 项经修复的同轮缺陷）、r01 手动指南（**界面版**，最小导出面，默认模拟模式）；**用户结果已生成**：[r01-用户审阅结果](implementation/reviews/manual/S05/r01-用户审阅结果.md)（**接受**，2026-10-07，用户原话「手动验证通过」；A/B 组有只读核对佐证，C/D/E 三组无痕迹未记为通过） |
| reviews/automated/S06、reviews/manual/S06 | r01 自动化审阅报告（**通过，待手动审阅**；**637 passed / 2 skipped**、`pnpm typecheck` 与 `pnpm build` 退出码 0、真实 uvicorn **构建产物模式**冒烟〔缺口回退 404 信封／静态写入 405／记录深链接 SPA 回退〕、参数边界与回归、**零真实调用**；含 4 项如实记录的问题与 1 项同轮修复）、r01 手动指南（**界面版**，A 组首页只读指示与整批闭环、B 组记录列表筛选分页与 URL 往返、C 组记录详情与被拒原文、D 组输入失败与控制项分离、E 组 `partial_failed` 收尾摘要与一键跳转、F 组本阶段不做的事，另附「界面外检查」；默认全程模拟模式、零费用）。**用户结果未生成，用户槽位未代填** |
| preparations | 用户 2026-10-06 决定不再新建；保持空目录 |
| .claude/rules | 四份路径规则、四份全项目执行/审阅/接手/环境规则 |
| requirements.in、requirements.txt | 直接依赖范围与解析生成的 36 个精确版本；用户环境实测 36/36 匹配、`pip check` 干净 |
| package.json、pyproject.toml | 已配置构建、脚本与固定版本依赖；后端可编辑安装通过 |
| Vite、TypeScript 配置 | 路径与构建配置就绪；`pnpm typecheck`、`pnpm build` 均通过 |
| .env.example、config.example.toml | 无密钥模板；配置解析、缺失提示与脱敏经单测验证 |
| Python 配置与日志、CLI 入口 | config.py 与 `--version`/`--check-config` 已实现；S02 增加只读 `inspect`；S03 增加 `serve`（界面服务，默认 127.0.0.1:8000）；S04 增加 `worker`；**S05 增加 `run`/`resume`/`export`（含 `--config`、`--wait`、`--retry-failed`；退出码 0/1/2/3/4/5）**；`evaluate` 仍为未实现提示（只定参数合同，指标计算属 S07） |
| S02 输入解析（excel/reader.py、schemas/qa.py、schemas/judgement.py、agent/validation.py） | 已实现：13 列读取、工作表选择、批次级阻断、逐行部分失败、编号与换行归一化、三分类枚举与引用位置校验；不调模型、不落库、不改写原文件 |
| S03 判别与模型适配（agent/{stage1,stage2,pipeline,validation,diagnostics,mock_samples}.py、llm/{client,errors}.py、prompts/*.md、schemas/analysis.py） | 已实现：阶段一仅 q+全部 ref、阶段二含 q/a/全部原 ref/阶段一结果；每次调用前记账、每阶段最多 3 次（含首次）并退避，非可重试错误立即失败且**不生成标签**；阶段一原结果与阶段二更正分别保留；模拟模式为确定性合成响应（`simulated: true`）。**2026-10-06 返工**：校验失败**留存被拒的原始输出**（仅 `content`，非推理链；`raw_output`/`raw_output_truncated`）；`ValidationError` 带 `kind` 分类用于选重试反馈措辞；`diagnostics.py` 提供只报事实的字符级摘录诊断；提示词与 schema 版本升至 `1.1`；新增强制失败模拟情境（显式编号 `FF-1` 命中，**既有五情境映射不变**） |
| S03 接口与界面（api/{app,responses}.py、api/routes/{samples,uploads,jobs}.py、services/{uploads,judging}.py、frontend/pages/JudgeView.vue） | 已实现最小闭环：上传 → 预检 → 判一条 → 轮询结果，以及样例清单与下载。上传与预检**已由 S04 落库**；`/api/judge`（临时接口）与首页单条判别**已于 S06 删除**，由 `/api/runs/...` 记录体系取代；`GET /api/jobs/{job_id}` 改读持久化任务 |
| 业务提示词（prompts/stage1.md、stage2.md） | 已实现（S03）；三分类枚举仍在 Python 包内，不由提示词定义 |
| 前端入口与业务页（S03-07） | main.ts、App.vue、router、JudgeView 已实现（「上传 → 预检 → 判一条」单页，路由 `/`）；api/client.ts 与 types/api.ts 为接口封装与类型；模拟模式为默认，真实模式需二次确认。S01 占位页 HomeView.vue 已删除。批次列表/详情、进度、恢复已由 S04-07 交付（`/runs`、`/runs/{run_id}`）；**导出与下载的最小面已由 S05-05 交付（2026-10-07 实现、自动化审阅通过、经用户界面验证接受）**；**记录列表、筛选分页与记录详情独立页（`/runs/{run_id}/records/{record_key}`）已由 S06-04／S06-05 交付（2026-10-07 实现、自动化审阅通过、待用户手动审阅）**，首页同时**删除单条判别与模拟/真实开关**、改为**只读模式指示**（模式由 `worker --mode` 决定） |
| SQL | `src/aidhu_om_agent/storage/` 为建表与迁移实现（`schema.sql` + `migrations/`，`SCHEMA_VERSION = 2`，迁移前备份到数据库同级 `backups/`，默认 `data/backups/`）；持久化经 S04 实现并验收 |
| S04 持久化与 worker（repositories/*、services/batches.py、worker.py） | 已实现（S04，阶段已接受）：`runs`／`records`／`stage_results`／`call_attempts` 落本机 SQLite；`worker` 为唯一队列消费者（独占锁，第二个退出码 3）；`interrupted` 只在新的 worker 取得独占锁后由启动恢复标出 |
| S05 导出与命令行（schemas/export.py、repositories/exports.py、services/exports.py、excel/writer.py 扩展） | 已实现（S05，2026-10-07 生产完毕、自动化审阅通过、经用户界面验证接受）：四表 Excel 与两阶段 JSONL 生成（人读序号、长文本截断标记、公式注入防护）、导出任务与成对发布（终态同事务入队、`run_id + scheduled_revision` 去重）、`/api/runs/{run_id}/exports`、`/api/artifacts/{artifact_id}/download`、`run`/`resume`/`export` 命令；**导出不含人工核定成绩** |
| 三个 PowerShell 脚本 | 明确报未实现，不启动/停止服务 |
| data、logs、outputs | `outputs/s02_samples/` 为 S02 手动审阅步骤 1 生成的合成样例；`data/runtime/` 为实跑证据与冒烟脚本（已被忽略，非业务数据）；**`data/runtime/judge-diagnostics/`（2026-10-06 返工新增）为判别失败的诊断产物**，每个失败任务一份 `judge-<job_id>.json`（只含 job/记录标识、时间、模型与版本、各次尝试结果与被拒输出、摘录的字符级事实；**不含凭据、不含推理链**），成功且无被拒尝试时不生成；仍无业务运行数据 |
| configs/config.local.toml、.env | **本机真实配置与凭据**，两者均被 `.gitignore` 忽略、不进版本库；`configs/` 下只提交无密钥模板。凭据只在后端运行时读取，快照只暴露 `api_key_present` |
| dist/frontend | 有真实构建产物（index.html、assets/index-BCkl9iws.js、assets/index-8cRK3lei.css）；每次构建会清空该目录，并会删除其中的 `.gitkeep`（已知 S01 遗留问题，见 S02 审阅报告 §4） |
| tests | **349 passed, 2 skipped（收集 351）**（2026-10-06 返工后全量）：配置 22、Excel 输入 26、样例 21、标签与校验 41、文本归一化 55、阶段消息构造 17、流水线重试与预算 26、判别服务与诊断 9、模拟客户端，以及 `tests/integration/test_api_s03.py` 的接口级用例 33；`test_real_service_s03.py` 为真实调用用例，**默认跳过**（`AIDHU_REAL_CALLS=1` 才跑）；E2E 与前端测试仍预留 |
| pnpm-lock.yaml、pnpm-workspace.yaml | 前端锁文件与 pnpm 12 设置（`allowBuilds: esbuild`） |
| 可选技能、子代理、CI、ADR、docs/plans | 仅预留目录，未启用内容 |

## 尚未生成的实际产物

- uv.lock：已批准不引入（用户选择仅用 requirements.txt 锁定）；也不会绕过用户 Conda 环境。
- CLAUDE.local.md、.claude/settings.json、settings.local.json、.env、config.local.toml：按实际需要生成，个人文件不提交。
- .claude/skills/database-migration/SKILL.md、.claude/agents/reviewer.md、.github/workflows/ci.yml：有需要再启用。
- data/state.sqlite3：存储实现后生成，没有假数据库。
- S04 及以后各阶段的方案/批准、自动化审阅与手动指南：到所属阶段按真实状态生成（S01—S04 的用户审阅结果均已生成）。

[实施入口](implementation/README.md) · [S01 自动化审阅](implementation/reviews/automated/S01/r01-自动化审阅.md) · [S01 用户审阅结果](implementation/reviews/manual/S01/r01-用户审阅结果.md) · [S02 自动化审阅](implementation/reviews/automated/S02/r01-自动化审阅.md) · [S02 手动操作审阅](implementation/reviews/manual/S02/r01-手动操作审阅.md) · [S02 用户审阅结果](implementation/reviews/manual/S02/r01-用户审阅结果.md) · [S03 自动化审阅 r02](implementation/reviews/automated/S03/r02-自动化审阅.md) · [S03 手动操作审阅（界面版 r02）](implementation/reviews/manual/S03/r02-手动操作审阅.md) · [S03 用户审阅结果 r02](implementation/reviews/manual/S03/r02-用户审阅结果.md) · [S04 自动化审阅](implementation/reviews/automated/S04/r01-自动化审阅.md) · [S04 手动操作审阅（界面版）](implementation/reviews/manual/S04/r01-手动操作审阅.md) · [S04 用户审阅结果](implementation/reviews/manual/S04/r01-用户审阅结果.md) · [S05 自动化审阅](implementation/reviews/automated/S05/r01-自动化审阅.md) · [S05 手动操作审阅（界面版）](implementation/reviews/manual/S05/r01-手动操作审阅.md) · [S05 用户审阅结果](implementation/reviews/manual/S05/r01-用户审阅结果.md) · [环境与依赖](implementation/handoffs/S01-开发环境与依赖清单.md) · [交接状态](HANDOFF.md)

目录与文档生成不等于业务开发完成。S01 工程基础、S02 的输入解析与预检、S03 的两阶段判别与界面骨架、S04 的持久化与 worker／批次与恢复、S05 的导出与命令行闭环均已实现、通过自动化审阅并经用户验收（**五个阶段均为阶段已接受**；S02 与 S03 为同一次界面操作一并验收，S03 的返工经 r02 复审与用户复验后接受，S04 与 S05 均于 2026-10-07 经界面验证接受）。**S05（导出与命令行闭环）的小步骤含界面验证面已于 2026-10-07 讨论定稿并获用户原话「直接批准」批准，S05-01—S05-06 已实现完毕、自动化审阅通过（594 passed / 2 skipped、零真实调用）并交付界面版手动指南，用户已于 2026-10-07 界面验证接受**（导出下载的最小面已在本阶段交付，完整交互仍属 S06）；**S06（API 与前端完整闭环）的小步骤含界面验证面已于 2026-10-07 讨论定稿并获用户原话「确认无误，批准开始」批准，S06-01—S06-06 已全部实现、自动化审阅通过（637 passed / 2 skipped、零真实调用）并交付界面版手动指南，现处「待手动审阅」**（记录列表与证据详情、筛选分页、上传防护补齐、删除 `/api/judge` 与首页单条判别均属本阶段；**全程零真实调用**）；**用户手动验证只经前端界面，每个阶段须交付界面验证面**。
