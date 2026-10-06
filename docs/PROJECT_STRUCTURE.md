# 实际目录与生成清单

更新日期：2026-10-06。基线：v2.3 规划；实施入口 v1.2，S01 阶段文档 v1.2，其余阶段 v1.0。状态：**S01 已实现并通过自动化审阅，待手动审阅（未验收）**；S02—S09 未开始。

初次骨架新增 50 个目录、94 个文件（含 23 个 .gitkeep）。2026-10-05 追加实施阶段、模板和规则；2026-10-06 追加交接检查、用户 Conda 依赖清单、AGENTS 入口及接手/环境规则；同日实现 S01（后端配置/日志、前端骨架、单测与构建产物、锁文件）并生成自动化与手动审阅文档。原 plan 保持不变。

当前项目根目录之下（不含根目录自身）共 63 个目录、144 个文件，其中 26 个 .gitkeep、13 个原 plan 文件；计数不含 node_modules、.pytest_cache 与 __pycache__。下树展示实际路径，隐藏 .gitkeep；空目录不表示功能已实现。

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
│   │   │   │   └── S01/
│   │   │   │       └── r01-自动化审阅.md
│   │   │   └── manual/
│   │   │       └── S01/
│   │   │           └── r01-手动操作审阅.md
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
│   │   │   │   └── uploads.py
│   │   │   ├── __init__.py
│   │   │   ├── app.py
│   │   │   └── schemas.py
│   │   ├── evaluation/
│   │   │   ├── __init__.py
│   │   │   ├── metrics.py
│   │   │   └── split.py
│   │   ├── excel/
│   │   │   ├── __init__.py
│   │   │   ├── reader.py
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
│   │   │   └── exports.py
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
│       ├── components/
│       ├── composables/
│       ├── pages/
│       │   └── HomeView.vue
│       ├── router/
│       │   └── index.ts
│       ├── styles/
│       │   └── main.css
│       ├── types/
│       ├── App.vue
│       ├── env.d.ts
│       ├── index.html
│       └── main.ts
├── tests/
│   ├── e2e/
│   ├── fixtures/
│   ├── integration/
│   └── unit/
│       ├── backend/
│       │   └── test_config.py
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
| README、AGENTS、CLAUDE、docs 导航 | 已同步 S01 实现/审阅进度和用户环境决定 |
| docs/implementation/stages | 9 份阶段文档、43 个小步骤；S01 为 v1.2 且已批准实施，S02—S09 未批准开发 |
| docs/implementation/handoffs | S01 前置检查、Conda 依赖说明与版本解析记录；不是阶段完成报告 |
| docs/implementation/templates | 开发前方案（已弃用）、自动化报告、手动指南三份模板 |
| reviews/automated/S01、reviews/manual/S01 | 已生成 r01 自动化审阅报告与待用户操作的手动指南；用户结果未反馈 |
| preparations | 用户 2026-10-06 决定不再新建；保持空目录 |
| .claude/rules | 四份路径规则、四份全项目执行/审阅/接手/环境规则 |
| requirements.in、requirements.txt | 直接依赖范围与解析生成的 36 个精确版本；用户环境实测 36/36 匹配、`pip check` 干净 |
| package.json、pyproject.toml | 已配置构建、脚本与固定版本依赖；后端可编辑安装通过 |
| Vite、TypeScript 配置 | 路径与构建配置就绪；`pnpm typecheck`、`pnpm build` 均通过 |
| .env.example、config.example.toml | 无密钥模板；配置解析、缺失提示与脱敏经单测验证 |
| Python 配置与日志、CLI 入口 | config.py 与 `--version`/`--check-config` 已实现；其余模块与业务命令仍占位 |
| 前端入口与占位页 | main.ts、App.vue、router、HomeView 已实现（工具链占位页，非业务页面） |
| SQL、业务提示词 | 占位，无业务实现 |
| 三个 PowerShell 脚本 | 明确报未实现，不启动/停止服务 |
| data、logs、outputs | 预留，无业务运行数据 |
| dist/frontend | 有真实构建产物（index.html、assets/index-*.js/.css）；每次构建会清空该目录 |
| tests | 22 项后端配置单测通过；集成/E2E/前端测试仍预留 |
| pnpm-lock.yaml、pnpm-workspace.yaml | 前端锁文件与 pnpm 12 设置（`allowBuilds: esbuild`） |
| 可选技能、子代理、CI、ADR、docs/plans | 仅预留目录，未启用内容 |

## 尚未生成的实际产物

- uv.lock：已批准不引入（用户选择仅用 requirements.txt 锁定）；也不会绕过用户 Conda 环境。
- CLAUDE.local.md、.claude/settings.json、settings.local.json、.env、config.local.toml：按实际需要生成，个人文件不提交。
- .claude/skills/database-migration/SKILL.md、.claude/agents/reviewer.md、.github/workflows/ci.yml：有需要再启用。
- data/state.sqlite3：存储实现后生成，没有假数据库。
- S01 用户审阅结果、S02 及以后各阶段的方案/批准、自动化审阅与手动指南：到所属阶段按真实状态生成。

[实施入口](implementation/README.md) · [S01 自动化审阅](implementation/reviews/automated/S01/r01-自动化审阅.md) · [S01 手动操作审阅](implementation/reviews/manual/S01/r01-手动操作审阅.md) · [环境与依赖](implementation/handoffs/S01-开发环境与依赖清单.md) · [交接状态](HANDOFF.md)

目录与文档生成不等于业务开发完成。S01 工程基础已实现并通过自动化审阅，但**尚未经用户手动审阅验收**，业务判别流程（Excel、模型、持久化、导出、API/页面）仍未实现；S02 须待用户接受 S01 后再讨论。
