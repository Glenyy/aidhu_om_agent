# AIDHU 回答判别 Agent

当前状态（2026-10-06）：S01（工程基础与运行环境）、S02（输入与核心数据合同）、S03（模型适配与两阶段判别，含界面骨架与模拟模式）三个阶段均已实现完毕、[自动化审阅](docs/implementation/reviews/automated/S03/r01-自动化审阅.md)通过并经用户验收，阶段状态均为**阶段已接受**：S01 单独验收（[S01 用户审阅结果](docs/implementation/reviews/manual/S01/r01-用户审阅结果.md)）；**S02 与 S03 由用户在同一次界面操作中一并验收通过**（[S02 结果](docs/implementation/reviews/manual/S02/r01-用户审阅结果.md)、[S03 结果](docs/implementation/reviews/manual/S03/r01-用户审阅结果.md)）。**S04—S09 未开始，小步骤均待讨论与批准**（S04 的启动前置「S03 已接受」已满足，但不构成开发批准）。技术/目录基线为 v2.3，实施入口为 v1.9，S01 阶段文档为 v1.3，S02 阶段文档为 v1.3，S03 阶段文档为 v1.4。

**手动验证方式（用户 2026-10-06 决定）：只使用本项目的前端界面验证。** 涉及模型的步骤默认走界面模拟模式，真实调用单独授权并显著标注。**前端界面已交付**（S03-07）：`python -m aidhu_om_agent serve` 后浏览器打开 `http://127.0.0.1:8000/` 即为「上传 → 预检 → 判一条」单页；用户已按 [S03 界面版手动操作审阅指南](docs/implementation/reviews/manual/S03/r01-手动操作审阅.md) 完成验收。S02 的 CLI 指南保留原文仅作排错参考。**真实调用预算 10 次（S03-06）已用满，再次真实调用需另行授权。**

项目读取编号、q、a、ref1—ref10，通过 V3 分析资料、R1 检查已有回答，输出三分类结果及人工复核清单。首版在个人电脑运行，通过浏览器上传、查看进度和结果、下载文件。

## 审阅入口

- [S01 自动化审阅报告](docs/implementation/reviews/automated/S01/r01-自动化审阅.md)：S01 实测命令、结果与已知限制。
- [S01 手动操作审阅指南](docs/implementation/reviews/manual/S01/r01-手动操作审阅.md)：S01 的可复制命令与预期结果（用户已验收接受）。
- [S02 自动化审阅报告](docs/implementation/reviews/automated/S02/r01-自动化审阅.md)：输入解析与预检的实测命令、结果与披露项。
- [S02 手动操作审阅指南](docs/implementation/reviews/manual/S02/r01-手动操作审阅.md)：11 步只读命令与预期结果（**已被界面验证取代**，保留作排错参考）。
- [S03 自动化审阅报告](docs/implementation/reviews/automated/S03/r01-自动化审阅.md)：两阶段判别、模拟模式、界面骨架的实测命令、结果、真实调用披露与已知限制。
- [S03 手动操作审阅指南（界面版）](docs/implementation/reviews/manual/S03/r01-手动操作审阅.md)：14 步浏览器点击顺序，A 组复验 S02、B 组复验 S03，默认全程模拟模式。
- [分阶段实施与审阅](docs/implementation/README.md)：9 份阶段文档、43 个可实施小步骤与审阅模板。
- [AGENTS 项目入口](AGENTS.md)：让接手 agent 读取规则与最新交接。
- [开发环境与依赖](docs/implementation/handoffs/S01-开发环境与依赖清单.md)：Conda Python 3.12 环境、依赖清单与实际核验结果。
- [S01 前置检查与接手](docs/implementation/handoffs/S01-前置检查与接手说明.md)：2026-10-06 检查快照（含文末更正）。
- [实际目录与清单](docs/PROJECT_STRUCTURE.md)：实际文件和占位状态。
- [总体规划](plan/README.md)：生命周期、接口、数据库和恢复设计。
- [目录设计](plan/07-项目目录与前后端选型.md)：前后端和模块边界。
- [文档导航](docs/README.md) 与 [开发规则入口](CLAUDE.md)。

实施遵循：讨论小步骤（**含该阶段界面验证面**）→ 用户审阅批准 → 开发 → 自动化审阅 → 生成界面版手动操作指南 → 用户接受阶段结果。S01 已走完整个流程并被接受；S02 与 S03 已走到“生成界面版手动操作指南”，按用户 2026-10-06 决定**在同一次界面操作中一并验收**。

## 当前完成范围

**已可用（工程基础）**：用户 Conda 环境 `aidhu_agent` 中依赖 36/36 匹配且 `pip check` 干净；项目包已可编辑安装，包资源（提示词、SQL）可定位；配置可解析、缺配置有明确错误、日志与快照不含凭据；前端可类型检查、可构建到 `dist/frontend`、可启动开发服务器。

**已可用（S02 输入侧）**：读取 13 列无标签 Excel，产出预检报告与解析快照——工作表选择、批次级阻断（缺列/重名列/重复编号/表无法确定/超条数/无有效记录）、逐行部分失败统计、编号与换行归一化、引用位置校验。**只读**，不调模型、不写库、不改写原文件。

**已可用（S03 判别侧）**：单条两阶段判别（阶段一仅 q + 全部 ref，阶段二含 q、a、全部原始 ref 与阶段一结果），每阶段最多 3 次调用（含首次）与退避，非可重试错误直接失败且**不生成标签**，阶段一原结果与阶段二更正分别保留；以及浏览器界面「上传 → 预检 → 判一条」，含**确定性模拟模式**（默认，零真实调用）。上传、预检与任务**都在进程内存**，服务重启即丢失；持久化、批次/恢复、导出与下载属后续阶段。

~~~bat
python -m aidhu_om_agent --version          :: 最小入口
python -m aidhu_om_agent --check-config     :: 配置自检，输出不含凭据的快照
python -m aidhu_om_agent inspect 样例.xlsx  :: 只读解析预检（通过 0 / 阻断 1 / 调用错误 2）
python -m aidhu_om_agent serve              :: 界面服务，浏览器打开 http://127.0.0.1:8000/
python -m pytest -q                         :: 后端测试（319 passed, 2 skipped）
pnpm typecheck && pnpm build                :: 前端类型检查与构建
pnpm dev                                    :: 开发服务器 http://127.0.0.1:5173/
~~~

`inspect` 的完整操作见 [S02 手动操作审阅指南](docs/implementation/reviews/manual/S02/r01-手动操作审阅.md)（含样例生成命令，**作排错参考**）；界面操作见 [S03 界面版指南](docs/implementation/reviews/manual/S03/r01-手动操作审阅.md)。

**尚未实现（业务）**：持久化与恢复、批次列表与进度、导出与下载、worker、人工复核回读仍为占位；`run/resume/export/evaluate` 只返回未实现提示。**当前程序不能完成批量业务判别流程与结果导出。**

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

[configs/config.example.toml](configs/config.example.toml) 是非敏感模板。首次使用需复制为 `configs/config.toml`（或已忽略提交的 `config.local.toml`）；缺失时会给出明确的复制提示。**注意：两者只读其一，`config.toml` 优先。** 模型服务地址、模型标识与参数兼容性已由 S03-06 用真实服务实测（模型 ID 与配置一致、只发 `model` 与 `messages`、延迟与超时余量见 [S03 自动化审阅报告](docs/implementation/reviews/automated/S03/r01-自动化审阅.md) §3.6）。

`data`、`outputs`、`logs`、`dist` 为运行与构建位置，除目录标记外由实际运行生成。S01 已由用户验收接受；S02 的[自动化审阅报告](docs/implementation/reviews/automated/S02/r01-自动化审阅.md)与[手动操作审阅指南](docs/implementation/reviews/manual/S02/r01-手动操作审阅.md)已生成，**改由界面验收，用户结果待反馈**。
