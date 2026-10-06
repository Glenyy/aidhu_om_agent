# AIDHU 回答判别 Agent

当前状态（2026-10-06）：S01（工程基础与运行环境）已实现并通过自动化审阅，阶段状态为**待手动审阅**，用户结果尚未反馈；S02—S09 未开始。技术/目录基线为 v2.3，实施入口为 v1.2，S01 阶段文档为 v1.2。

项目读取编号、q、a、ref1—ref10，通过 V3 分析资料、R1 检查已有回答，输出三分类结果及人工复核清单。首版在个人电脑运行，通过浏览器上传、查看进度和结果、下载文件。

## 审阅入口

- [S01 自动化审阅报告](docs/implementation/reviews/automated/S01/r01-自动化审阅.md)：S01 实测命令、结果与已知限制。
- [S01 手动操作审阅指南](docs/implementation/reviews/manual/S01/r01-手动操作审阅.md)：**待你操作**的可复制命令与预期结果。
- [分阶段实施与审阅](docs/implementation/README.md)：9 份阶段文档、43 个可实施小步骤与审阅模板。
- [AGENTS 项目入口](AGENTS.md)：让接手 agent 读取规则与最新交接。
- [开发环境与依赖](docs/implementation/handoffs/S01-开发环境与依赖清单.md)：Conda Python 3.12 环境、依赖清单与实际核验结果。
- [S01 前置检查与接手](docs/implementation/handoffs/S01-前置检查与接手说明.md)：2026-10-06 检查快照（含文末更正）。
- [实际目录与清单](docs/PROJECT_STRUCTURE.md)：实际文件和占位状态。
- [总体规划](plan/README.md)：生命周期、接口、数据库和恢复设计。
- [目录设计](plan/07-项目目录与前后端选型.md)：前后端和模块边界。
- [文档导航](docs/README.md) 与 [开发规则入口](CLAUDE.md)。

实施遵循：讨论小步骤 → 用户审阅批准 → 开发 → 自动化审阅 → 生成手动操作指南 → 用户接受阶段结果。S01 已走到“等待用户操作”；S02 之后仍按此流程，小步骤在所属阶段前讨论。

## 当前完成范围

**已可用（工程基础）**：用户 Conda 环境 `aidhu_agent` 中依赖 36/36 匹配且 `pip check` 干净；项目包已可编辑安装，包资源（提示词、SQL）可定位；配置可解析、缺配置有明确错误、日志与快照不含凭据；前端可类型检查、可构建到 `dist/frontend`、可启动开发服务器。

~~~bat
python -m aidhu_om_agent --version          :: 最小入口
python -m aidhu_om_agent --check-config     :: 配置自检，输出不含凭据的快照
python -m pytest -q                         :: 后端单测
pnpm typecheck && pnpm build                :: 前端类型检查与构建
pnpm dev                                    :: 开发服务器 http://127.0.0.1:5173/
~~~

**尚未实现（业务）**：Excel 解析、模型调用、两阶段判别、持久化与恢复、导出、API 与业务页面、worker 仍为占位；`run/resume/export/evaluate` 只返回未实现提示。**当前程序不能完成业务判别流程。**

依赖已安装（requirements.txt，36 项固定；前端由 pnpm-lock.yaml 锁定）。未引入 uv.lock（已批准决定）。分类质量验收属 S07/S08，尚未执行。

src/frontend 放 Vue 前端；src/aidhu_om_agent 放 Python API、worker 与判别代码。根目录 package.json/pyproject.toml 分别管理前端与 Python，tests 放测试。

## Python 依赖安装

你在 Anaconda 控制台激活自己的 Python 3.12 环境后，通过 [requirements.txt](requirements.txt) 安装：

~~~bat
python -m pip install -r "D:\pyProjects\dha_projects\aidhu_om_agent\requirements.txt"
~~~

文件已固定全部 36 个包的精确版本；[requirements.in](requirements.in) 保存直接依赖范围。[Windows/Python 3.12 解析与元信息检查](docs/implementation/handoffs/S01-依赖版本解析记录.md) 已通过；用户环境 `aidhu_agent` 已实际安装并核验为 36/36 匹配（见[依赖清单 §5](docs/implementation/handoffs/S01-开发环境与依赖清单.md)）。前端由 Node/pnpm 管理（Node v22.16.0 + pnpm 12.9.1）。

## 配置

[.env.example](.env.example) 列出后端可用的环境变量名与空值；需要时复制为 `.env`，真实凭据不提交。前端不读取该文件。优先级为：进程环境变量 > `.env` > TOML > 内置默认值。

[configs/config.example.toml](configs/config.example.toml) 是非敏感模板。首次使用需复制为 `configs/config.toml`（或已忽略提交的 `config.local.toml`）；缺失时会给出明确的复制提示。模型服务地址、模型标识与参数兼容性要到 S03 用真实服务验证。

`data`、`outputs`、`logs`、`dist` 为运行与构建位置，除目录标记外由实际运行生成。S01 的[自动化审阅报告](docs/implementation/reviews/automated/S01/r01-自动化审阅.md)与[手动操作审阅指南](docs/implementation/reviews/manual/S01/r01-手动操作审阅.md)已生成；用户操作结果待反馈。
