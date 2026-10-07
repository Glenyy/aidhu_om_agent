# AIDHU 回答判别 Agent

当前状态（2026-10-07）：S01（工程基础与运行环境）、S02（输入与核心数据合同）、S03（模型适配与两阶段判别，含界面骨架与模拟模式）、S04（持久化与任务恢复）、S05（导出与命令行闭环）、S06（API 与前端完整闭环）、S07（人工基准与规则校准，S07-01—S07-03）七个阶段均已实现完毕、通过自动化审阅并经用户验收，阶段状态均为**阶段已接受**：S01 单独验收（[S01 用户审阅结果](docs/implementation/reviews/manual/S01/r01-用户审阅结果.md)）；**S02 与 S03 由用户在同一次界面操作中一并验收通过**（[S02 结果](docs/implementation/reviews/manual/S02/r01-用户审阅结果.md)、[S03 结果](docs/implementation/reviews/manual/S03/r01-用户审阅结果.md)；S03 另有返工轮次 r02，复验后接受）；**S04 于 2026-10-07 经用户界面验证接受**（[S04 结果](docs/implementation/reviews/manual/S04/r01-用户审阅结果.md)）。**S05（导出与命令行闭环）已实现完毕、自动化审阅通过（[r01](docs/implementation/reviews/automated/S05/r01-自动化审阅.md)），并于 2026-10-07 经用户界面验证接受**（[S05 结果](docs/implementation/reviews/manual/S05/r01-用户审阅结果.md)）。**S06（API 与前端完整闭环）的小步骤含界面验证面已于 2026-10-07 讨论定稿并直接写入其阶段文档（v1.3 → v2.0），经用户原话「确认无误，批准开始」批准；S06-01—S06-06 已全部实现，[r01-自动化审阅](docs/implementation/reviews/automated/S06/r01-自动化审阅.md) 通过（637 passed / 2 skipped、`pnpm typecheck` 与 `pnpm build` 退出码 0、真实 uvicorn 冒烟、**零真实调用**），同次交付 [界面版手动指南](docs/implementation/reviews/manual/S06/r01-手动操作审阅.md)，阶段文档升至 v2.1；**用户 2026-10-07 以原话「阶段6手动审阅通过，我们进行阶段7的讨论」接受，S06 现为「阶段已接受」（阶段文档 v2.2），结果与记录边界见 [S06 结果](docs/implementation/reviews/manual/S06/r01-用户审阅结果.md)——A 组与 D 组有本机批次佐证，B／C／E／F 四组不写库、无独立痕迹、未记为已通过**（S06 的前置「S05 已接受」**已满足**，交付标识已回填为提交 `a46716a`）。**S07 的启动前置「S06 已接受」已满足；S07 的小步骤（含界面验证面）已于 2026-10-07 讨论定稿并写入其阶段文档（v1.0 → v2.0），经用户原话「批准」批准；S07-01—S07-03 已全部实现、[r01-自动化审阅](docs/implementation/reviews/automated/S07/r01-自动化审阅.md) 通过（743 passed / 2 skipped、零真实调用），同次交付 [界面版手动指南](docs/implementation/reviews/manual/S07/r01-手动操作审阅.md)，**用户 2026-10-07 按该指南在界面上操作后以原话「手动验证通过」接受，S07 现为「阶段已接受」（阶段文档 v2.3），结果与记录边界见 [S07 结果](docs/implementation/reviews/manual/S07/r01-用户审阅结果.md)——A 组有本机库内佐证，B 组页面读数／C 组／D 组与指南 §3.1 不写库、无独立痕迹、未记为已通过；S07-04（校准并冻结规则版本）未实现**——其真实模型调用预算不在批准范围内，**保留集全程一次不跑**、**本轮不产出任何准确率结论**；**S08、S09 的前置已满足（S08 需 S07 已接受），但两者小步骤均未讨论、未批准**。技术/目录基线为 v2.3，实施入口为 v2.13，S01 阶段文档为 v1.3，S02 阶段文档为 v1.3，S03 阶段文档为 v1.6，S04 阶段文档为 v2.4，S05 阶段文档为 v2.2，S06 阶段文档为 v2.2，**S07 阶段文档为 v2.3**。最新进度见 [交接状态](docs/HANDOFF.md)。

**手动验证方式（用户 2026-10-06 决定）：只使用本项目的前端界面验证。** 涉及模型的步骤默认走界面模拟模式，真实调用单独授权并显著标注。**前端界面已交付**（S03-07 骨架 + S04-07 最小批次面 + S05-05 最小导出面）：`python -m aidhu_om_agent serve` 后浏览器打开 `http://127.0.0.1:8000/` 即为「上传 → 预检 → 判一条」单页，`/runs` 为批次列表、`/runs/{run_id}` 为批次详情（含导出区块与下载），**整批执行另需一个 `python -m aidhu_om_agent worker` 进程**。用户已按界面版指南完成 S02／S03（一并）、S04、S05、S06 与 S07 的验收；S07 的 [界面版指南](docs/implementation/reviews/manual/S07/r01-手动操作审阅.md) 已执行并接受（2026-10-07）。S02 的 CLI 指南保留原文仅作排错参考。**真实调用预算 10 次（S03-06）已用满，S04、S05、S06 与 S07-01—S07-03 均全程零真实调用；再次真实调用需另行授权。**

项目读取编号、q、a、ref1—ref10，通过 V3 分析资料、R1 检查已有回答，输出三分类结果及人工复核清单。首版在个人电脑运行，通过浏览器上传、查看进度和结果、下载文件。

## 审阅入口

- [S01 自动化审阅报告](docs/implementation/reviews/automated/S01/r01-自动化审阅.md)：S01 实测命令、结果与已知限制。
- [S01 手动操作审阅指南](docs/implementation/reviews/manual/S01/r01-手动操作审阅.md)：S01 的可复制命令与预期结果（用户已验收接受）。
- [S02 自动化审阅报告](docs/implementation/reviews/automated/S02/r01-自动化审阅.md)：输入解析与预检的实测命令、结果与披露项。
- [S02 手动操作审阅指南](docs/implementation/reviews/manual/S02/r01-手动操作审阅.md)：11 步只读命令与预期结果（**已被界面验证取代**，保留作排错参考）。
- [S03 自动化审阅报告](docs/implementation/reviews/automated/S03/r01-自动化审阅.md)：两阶段判别、模拟模式、界面骨架的实测命令、结果、真实调用披露与已知限制。
- [S03 手动操作审阅指南（界面版）](docs/implementation/reviews/manual/S03/r01-手动操作审阅.md)：14 步浏览器点击顺序，A 组复验 S02、B 组复验 S03，默认全程模拟模式。
- S04—S07 的审阅报告与结果：[S04](docs/implementation/reviews/automated/S04/r01-自动化审阅.md)｜[S05](docs/implementation/reviews/automated/S05/r01-自动化审阅.md)｜[S06 自动化审阅](docs/implementation/reviews/automated/S06/r01-自动化审阅.md)｜[S07 自动化审阅](docs/implementation/reviews/automated/S07/r01-自动化审阅.md)；界面版指南 [S04](docs/implementation/reviews/manual/S04/r01-手动操作审阅.md)、[S05](docs/implementation/reviews/manual/S05/r01-手动操作审阅.md)、[S06](docs/implementation/reviews/manual/S06/r01-手动操作审阅.md)、[S07](docs/implementation/reviews/manual/S07/r01-手动操作审阅.md)；用户结果 [S04](docs/implementation/reviews/manual/S04/r01-用户审阅结果.md)、[S05](docs/implementation/reviews/manual/S05/r01-用户审阅结果.md)、[S06](docs/implementation/reviews/manual/S06/r01-用户审阅结果.md)、[S07](docs/implementation/reviews/manual/S07/r01-用户审阅结果.md)。
- [分阶段实施与审阅](docs/implementation/README.md)：9 份阶段文档、43 个可实施小步骤与审阅模板。
- [AGENTS 项目入口](AGENTS.md)：让接手 agent 读取规则与最新交接。
- [开发环境与依赖](docs/implementation/handoffs/S01-开发环境与依赖清单.md)：Conda Python 3.12 环境、依赖清单与实际核验结果。
- [S01 前置检查与接手](docs/implementation/handoffs/S01-前置检查与接手说明.md)：2026-10-06 检查快照（含文末更正）。
- [实际目录与清单](docs/PROJECT_STRUCTURE.md)：实际文件和占位状态。
- [总体规划](plan/README.md)：生命周期、接口、数据库和恢复设计。
- [目录设计](plan/07-项目目录与前后端选型.md)：前后端和模块边界。
- [文档导航](docs/README.md) 与 [开发规则入口](CLAUDE.md)。

实施遵循：讨论小步骤（**含该阶段界面验证面**）→ 用户审阅批准 → 开发 → 自动化审阅 → 生成界面版手动操作指南 → 用户接受阶段结果。S01、S02、S03（含返工复验）、S04、S05、S06、S07 均已走完整个流程并被接受；**S07 的 [界面版手动指南](docs/implementation/reviews/manual/S07/r01-手动操作审阅.md) 已由用户执行并以原话「手动验证通过」接受，S07 现为「阶段已接受」；S07-04 未实现**（真实调用预算未授权）。

## 当前完成范围

**已可用（工程基础）**：用户 Conda 环境 `aidhu_agent` 中依赖 36/36 匹配且 `pip check` 干净；项目包已可编辑安装，包资源（提示词、SQL）可定位；配置可解析、缺配置有明确错误、日志与快照不含凭据；前端可类型检查、可构建到 `dist/frontend`、可启动开发服务器。

**已可用（S02 输入侧）**：读取 13 列无标签 Excel，产出预检报告与解析快照——工作表选择、批次级阻断（缺列/重名列/重复编号/表无法确定/超条数/无有效记录）、逐行部分失败统计、编号与换行归一化、引用位置校验。**只读**，不调模型、不写库、不改写原文件。

**已可用（S03 判别侧）**：单条两阶段判别（阶段一仅 q + 全部 ref，阶段二含 q、a、全部原始 ref 与阶段一结果），每阶段最多 3 次调用（含首次）与退避，非可重试错误直接失败且**不生成标签**，阶段一原结果与阶段二更正分别保留；以及浏览器界面「上传 → 预检 → 判一条」，含**确定性模拟模式**（默认，零真实调用）。~~`/api/judge` 临时接口~~ **已于 S06 删除**（连同首页单条判别），由 `/api/runs/...` 记录体系取代。

**已可用（S04 持久化与批量侧）**：上传/预检/批次/记录/调用尝试落本机 SQLite（结构版本 2，迁移自建、迁移前备份），`worker` 为**唯一**队列消费者（OS 独占锁，第二个退出码 3），批次列表与详情页（计数、进度、`revision`、调用统计、失败摘要、`allowed_actions`、恢复/重试），`interrupted` 只在新的 worker 取得独占锁后标出。**判别结果落在本机 SQLite，原始 xlsx 存 `data/uploads/`。**

**已可用（S05 导出与命令行侧）**：导出四表 Excel（分类结果 / 复核清单 / 失败清单 / 运行概况）与两阶段 JSONL，**成对发布**；判别终态自动排队导出，任意批次状态也可手动导出（概况表标明未处理数）；批次详情页可发起导出并下载；CLI `run`/`resume`/`export` 可用（`--wait` 检测不到 worker 时以退出码 4 退出）。`evaluate` 只定参数合同，指标计算属 S07。

**已可用（S06 API 与前端完整闭环）**：批次详情页新增**记录区块**（按状态／标签／需复核／编号筛选，分页默认 50、上限 200，筛选写在地址栏、刷新与返回都保留）与**记录详情独立页** `/runs/{run_id}/records/{record_key}`（13 列输入原文、阶段一资料分析与逐字摘录、阶段二标签／理由／复核项与「阶段一更正」、逐次调用摘要——第几次、耗时、是否模拟、错误码；**只有被校验拒绝的尝试**带 `raw_output` 正文，成功尝试与导出都不带）；`/runs` 列表支持筛选分页；**首页删除单条判别与模拟/真实开关**，改为**只读模式指示**（模式由 `worker --mode` 决定）。上传防护补齐（解压总量 8 × 上传上限、单表 50 万单元格、可见工作表 50 个，均为内置常量）；`partial_failed` 显示为**收尾摘要**（`failure_summary`）并可一键跳到失败列表，`last_error` 改条件显示。`POST /api/judge` **已删除**，诊断产物改由 worker 失败路径写入。

**已可用（S07 评估侧，S07-01—S07-03）**：人工核定数据合同与只读校验（真实核定件只在 `data/evaluation/` 本地存在、**不入库**）；固定分层划分（种子 `20261005`、校准 20 条、按三类比例最大余数分配、每类校准 ≥1／保留 ≥5，产物 `split-v1.json` + `calibration-v1.xlsx`／`holdout-v1.xlsx`）；指标计算与 `evaluate`（`python -m aidhu_om_agent evaluate <run_id> …`，**零模型调用、不依赖 worker**；混淆矩阵行 = 人工真值、列 = agent 预测，逐类 TP/FP/FN/P/R/F1，Macro-F1 为三个 F1 的算术平均，**每项带分子与分母**，零分母记「不可计算」）与评估落库（`evaluations`／`evaluation_records` 两表，`SCHEMA_VERSION 2 → 3`，迁移前自动备份）。**界面新增只读评估页** `/runs/{run_id}/evaluation`（由批次详情页「评估结果」进入）：评估历史、评估概要、达标判定、混淆矩阵、逐类指标、划分清单、逐条对照、冻结版本清单与报告下载；页面**只读**，不触发计算、不调用模型、不编辑标签。**S07-04（校准并冻结规则版本）未实现**——真实调用预算未获授权，**保留集全程一次不跑**。**S07-01—S07-03 已于 2026-10-07 经用户界面验证接受（原话「手动验证通过」），S07 现为「阶段已接受」，但本轮演示数据为合成数据、预测取自模拟模式，故不产出任何准确率结论。**

~~~bat
python -m aidhu_om_agent --version          :: 最小入口
python -m aidhu_om_agent --check-config     :: 配置自检，输出不含凭据的快照
python -m aidhu_om_agent inspect 样例.xlsx  :: 只读解析预检（通过 0 / 阻断 1 / 调用错误 2）
python -m aidhu_om_agent serve              :: 界面服务，浏览器打开 http://127.0.0.1:8000/
python -m aidhu_om_agent worker             :: 队列消费者（整批执行必需；--mode mock|real、--once）
python -m aidhu_om_agent run 样例.xlsx      :: 入队整批判别（--wait 等待；无 worker 时退出码 4）
python -m aidhu_om_agent export <run_id>    :: 入队导出（--wait 等待）
python -m aidhu_om_agent evaluate <run_id>  :: 按核定与划分计算指标并落库（0 完成 / 1 配置 / 2 参数或状态）
python -m pytest -q                         :: 后端测试（743 passed, 2 skipped）
pnpm typecheck && pnpm build                :: 前端类型检查与构建
pnpm dev                                    :: 开发服务器 http://127.0.0.1:5173/
~~~

`inspect` 的完整操作见 [S02 手动操作审阅指南](docs/implementation/reviews/manual/S02/r01-手动操作审阅.md)（含样例生成命令，**作排错参考**）；界面操作见 [S03 界面版指南](docs/implementation/reviews/manual/S03/r01-手动操作审阅.md) 与 [S04](docs/implementation/reviews/manual/S04/r01-手动操作审阅.md)、[S05](docs/implementation/reviews/manual/S05/r01-手动操作审阅.md)、[S06](docs/implementation/reviews/manual/S06/r01-手动操作审阅.md)、[S07](docs/implementation/reviews/manual/S07/r01-手动操作审阅.md)（**评估页，已由用户执行并接受**）的界面版指南。

**尚未实现（业务）**：在线人工改标签与复核回读、**S07-04 的校准与规则冻结**，以及分类质量验收（属 S07-04／S08）。**其余占位命令返回未实现提示。**

依赖已安装（requirements.txt，36 项固定；前端由 pnpm-lock.yaml 锁定）。未引入 uv.lock（已批准决定）。分类质量验收属 S07-04／S08，**尚未执行——S07 本轮只交付了指标计算与评估落库，不产出任何准确率结论**。

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
