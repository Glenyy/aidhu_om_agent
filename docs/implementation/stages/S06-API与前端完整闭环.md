# S06 API与前端完整闭环

实施文档版本：**v2.2**｜日期：**2026-10-07**｜状态：**阶段已接受**（S06-01—S06-06 共 6 个步骤已于 2026-10-07 讨论定稿并**直接写入本文件**，经用户原话「**确认无误，批准开始**」批准；**六步已全部实现，[r01-自动化审阅](../reviews/automated/S06/r01-自动化审阅.md) 通过**——**637 passed / 2 skipped**、`pnpm typecheck` 与 `pnpm build` 退出码 0、真实 uvicorn 构建产物模式冒烟、**零真实调用**；同次交付 [界面版手动指南](../reviews/manual/S06/r01-手动操作审阅.md)，**用户 2026-10-07 以原话「阶段6手动审阅通过，我们进行阶段7的讨论」接受，S06 状态由「待手动审阅」变更为「阶段已接受」**，结果与记录边界见 [r01-用户审阅结果](../reviews/manual/S06/r01-用户审阅结果.md)。审批与交付记录见 §6。**前置「S05 已接受」已满足**——S05 经用户界面验证接受，见 [S05 用户审阅结果](../reviews/manual/S05/r01-用户审阅结果.md)。**v2.0 取代 v1.3 的「待讨论」状态**：原 S06-01／S06-04 的部分内容已在 S03（界面骨架与上传接口）与 S05（最小导出面）提前交付，本次按实际代码重新拆分，见 §0.5。**v2.0 → v2.1 只更新状态与交付记录，v2.1 → v2.2 只更新状态与验收记录，均未改任何步骤内容**）

对应：M3；DEV-14—DEV-17  
前置：S05 已接受（**已满足**，2026-10-07）；用户审阅并批准本节定稿的小步骤、界面验证面与错误合同。  
设计依据：[前后端选型](../../../plan/07-项目目录与前后端选型.md)、[API 合同](../../../plan/08-API接口与数据合同.md)、[任务状态](../../../plan/10-任务状态与恢复设计.md)、[持久化设计](../../../plan/09-数据库与持久化设计.md)  
统一流程：[实施与审阅入口](../README.md)

## 0. 2026-10-07 讨论定稿

本轮讨论先核对了实际代码（事实见 §0.4），随后就七项议题取得用户决定（§0.2）。**本文件取代 v1.3**：v1.3 写「本阶段补齐完整交互」但未区分「已由 S03／S05 交付的部分」，若按原文开发会重复实现已存在的上传接口与导出面，故按实际代码重新拆分为 §2 的六步。

### 0.1 由 S04 验收带出的待决项（2026-10-07 用户决定）

S04 验收时挂起的三个问题已由用户定稿，其中两项**留到本阶段动手**（记录见 [plan/08 §5](../../../plan/08-API接口与数据合同.md)、[plan/09](../../../plan/09-数据库与持久化设计.md)、[S04 阶段文档](S04-持久化与任务恢复.md) §6）：

| 项 | 用户决定 | 本阶段要做的事 |
| --- | --- | --- |
| `order_index` 起点 | **定为 0 起**，plan/09 已按实际合同更正，不改已验收代码、不迁移存量数据 | 本阶段无动作。人读序号 1..N 由**导出层**生成（S05 已落地），不在存储层平移 |
| `partial_failed` 的 `last_error` 取 `{code: PARTIAL_FAILED, failed_count: N}` | **做语义分工**：批次收尾摘要不属于「错误」，改由 `failure_summary` 承载 | **由 S06-03 落实**，改法见 §0.3 第 8 项；界面横幅改条件显示并补上现在模板未渲染的失败数 |
| 详情页 `recent_jobs`、`call_statistics`、`failure_summary` | **追认进 plan/08 §5 合同** | 保留并使用；`execution_control.last_worker`、`latest_export` 本就在合同内，不属新增。本阶段只读、不改写入路径 |

### 0.2 本轮讨论的用户决定（2026-10-07，七项）

| 议题 | 决定 |
| --- | --- |
| **首页「判一条」与 `POST /api/judge`** | **删除**，判一条能力由整批 + 批次记录详情取代。首页保留「上传 → 预检 → 开始判别（整批）」，不再保留单条判别按钮。依据是 [plan/08 §1 结尾](../../../plan/08-API接口与数据合同.md) 已把 `/api/judge` 明确列为临时接口、由 `records` 体系取代 |
| **记录详情形态** | **独立路由页** `/runs/:runId/records/:recordKey`——可深链接、可刷新、可返回列表；不用页内抽屉（刷新即丢上下文、无法把某条记录发给别人） |
| **上传防护（plan/08 §3 的「解压总量、单元格及工作表读取保护」）** | **本阶段补齐**；参数为**内置常量**，**不新增 `config.toml` 键**（沿用 S04「不新增配置项」的先例），其中解压总量与既有上限**联动**，见 §0.3 第 7 项 |
| **前端自动化测试** | **不引入新依赖**（不安装 vitest／playwright）。沿用 S01—S05 做法：`pnpm typecheck` + `pnpm build` 退出码 0、真实 uvicorn 冒烟、界面手动验证；`tests/e2e/` 与 `tests/unit/frontend/` 保持空占位 |
| **被拒原始输出（S03 返工 R-1 的界面成果）** | **承接**：记录详情接口给出失败尝试的 `raw_output`。数据**已在库**（`call_attempts.final_content`），无需新落库；这**修改了 S04 的决定**（原为「`raw_output` 落本地库、不进 API 契约」），只在**记录详情**暴露，列表与导出不带 |
| **失败诊断产物（S03 返工 R-5 的界面成果）** | **承接**：把 `judge-diagnostics/` 的写入从临时注册表**迁到 worker 的失败路径**。写盘失败只记日志，**绝不改变任务状态** |
| **首页模拟／真实模式开关** | **改为只读指示**：删掉可点的开关（它只影响已被删除的单条判别），改为显示 worker 的**实际模式**，取自批次详情已有的 `execution_control.last_worker.mode` 与「含模拟调用」计数，避免开关看起来能控制整批 |

### 0.3 技术定稿（随步骤一并审阅）

1. **全程零真实模型调用**：本阶段读的全是已落库数据，判别执行仍由 `worker --mode` 决定。S03-06 的 10/10 预算维持**用满**；任何真实调用需另行授权。
2. **记录接口契约以 [plan/08 §6](../../../plan/08-API接口与数据合同.md) 为准**：列表参数 `page`、`page_size`、`label`、`review_required`、`status`、`record_id`（精确匹配）；多项筛选 AND；非法枚举或分页值 422；**固定按 `order_index` 升序**；返回 `revision` 与筛选后的 `total`；**列表不传 `a` 与全部 ref**，摘要字段为 `record_key`、`record_id`、`source_row`、`order_index`、`q_preview`、`status`、`label`、`reason`、`review_required`、`failure`。**分页默认 50、上限 200**（plan/08 未定，本阶段定；`/runs` 列表沿用 plan/08 §5 的默认 50、上限 100）。
3. **记录详情字段**：完整 `input`（`record_id`、`q`、`a`、`ref1`—`ref10`）、`stage1`、`stage2`、`failure`、`attempt_summary`。**不返回**模型推理链、请求头、SDK 调试堆栈（[plan/08 §6](../../../plan/08-API接口与数据合同.md)）。`attempts` 数组**沿用** [`agent/pipeline.py`](../../../src/aidhu_om_agent/agent/pipeline.py) `result_to_payload()` 的**现成形状**（`stage`／`attempt`／`outcome`／`model`／`latency_ms`／`simulated`／`usage`／`error_code`／`error_message`／`raw_output`／`raw_output_truncated`）——成功尝试**不带** `raw_output`，只有被校验拒绝的那几次带，超长保头尾并以 `raw_output_truncated` 标记。这样 S03 返工的两项界面成果（被拒原文、第 N 次尝试与耗时）在批次路径上同样可见。
   - 该改动需在 [plan/08 §6](../../../plan/08-API接口与数据合同.md) 补一句说明；`storage/migrations/001_init.sql` 对 `call_attempts.final_content` 的建表注释已写明「是否经 API 暴露由 S06 定」，此处即该决定。（**已落实**：该句已补入 plan/08 §6。）
4. **`record_key` 取内部 UUID**：`GET /api/runs/{run_id}/records/{record_key}` 的键是 `records.record_key`（程序生成的 UUID），与 [plan/08 §1](../../../plan/08-API接口与数据合同.md)「输入编号可能缺失或含不适合 URL 的字符，不直接作为路径键」一致；**不用**上传预检返回的可读键。
5. **`GET /api/jobs/{job_id}` 改读持久化任务**：现在只查内存注册表（[jobs.py:89-95](../../../src/aidhu_om_agent/api/routes/jobs.py#L89-L95)），落库的批次任务查不到。改为按 `job_id` 查 `jobs` 表，返回 [plan/08 §7](../../../plan/08-API接口与数据合同.md) 字段（`job_id`、`run_id`、`kind`、`mode`、`status`、`created_at`、`started_at`、`finished_at`、`current_record_key`、`current_stage`、`error`、`result`）。**注意**：`jobs.mode` 对判别任务是 `initial`／`resume`／`retry_failed`、对导出是 `automatic`／`manual`，**不是** mock／real——界面**不得**用它判断模拟/真实，模拟/真实只看 `call_statistics.*.simulated` 与 `execution_control.last_worker.mode`。
6. **诊断产物迁移**：把 `_write_diagnostic` 从 `services/judging.py` 抽到 [`agent/diagnostics.py`](../../../src/aidhu_om_agent/agent/diagnostics.py)（该模块已有 `diagnose_*` 函数），由 **worker 的记录失败路径**调用，判定口径与 S03 相同（`completed` 且全部尝试 `outcome == "ok"` 才不写）。文件名改为 `judge-<job_id>-<record_key>.json`（一个批次任务可能有多条失败记录，避免互相覆盖）；**历史诊断文件不迁移、不删除**（S03 的 `judge-<job_id>.json` 保留原样）。写盘失败按 S03 的做法**整段 try/except**，只记日志。
7. **上传防护（内置常量，落在 `excel/reader.py`，不新增配置键）**：
   - **解压总量** ≤ **8 × `limits.max_upload_bytes`**（现为 400 MiB，随配置联动，不写死）；
   - **单表单元格数** ≤ **500,000**；
   - **可见工作表数** ≤ **50**。
   检查在**打开工作簿之前**用 `zipfile` 读成员 `file_size` 求和完成（不解压）；超限返回 422 `BAD_WORKBOOK` 并说明**实际值与上限**，不裁剪资料。`413 FILE_TOO_LARGE` 仍只用于压缩包字节数超限（现状 [reader.py:194](../../../src/aidhu_om_agent/excel/reader.py#L194)），两者不混用。
8. **`last_error` 语义分工**：`worker._finalize` 对 `partial_failed` **不再写 `last_error`**（写 `None`），收尾摘要由 `failure_summary` + `counts` 承载；`_fail_systemically` 路径**继续写** `last_error`（那是真错误）。**存量批次已有的 `{code: PARTIAL_FAILED, failed_count}` 不迁移、不改写**，界面需两种都正确显示。
9. **删除与保留清单（随 §0.2 第 1 项）**：删 `POST /api/judge`、`services/judging.py` 内的 `JudgeJob`／`JudgeJobRegistry`／`_ReportingClient`／`_now_iso`、`api/schemas.py::JudgeRequest`、`api/app.py` 的 `app.state.jobs`；**保留** `execute_record`、`SqliteBudgetLedger`、`RecordExecution` 等 worker 依赖的部分。上传预检响应里的 `records[].record_key` **保留**（仍是可读的「编号或来源行」，界面用它做有效记录预览），但**不再作为判别选择键**，其 docstring 说明更新。
10. **有意修改已验收的界面行为（报告须单列）**：① 首页删掉模拟/真实开关与单条判别区（S03 已验收）；② 首页模式改为只读指示；③ 批次详情页 `last_error` 横幅改条件显示（S04／S05 已验收）；④ 预检的 `records[].record_key` 语义变化。四项都不改业务规则。
11. **`/runs` 列表筛选分页**：接口**已支持**（`page`／`page_size`／`status`，见 [runs.py:120-138](../../../src/aidhu_om_agent/api/routes/runs.py#L120-L138)），本阶段只加界面控件，并把筛选条件写进 **URL query**（刷新与分享后仍生效）。
12. **前端轮询**：批次 `revision` 变化时刷新当前页与统计（plan/08 §6 要求，避免把不同轮次统计组合成最终报告）；记录列表与详情沿用 2 秒轮询、终态停止。
13. **本地开发与静态服务**：`vite` dev 代理 `/api → 127.0.0.1:8000`、后端静态挂载 + SPA 回落（且 `/api` 不被回落吞掉，[app.py:97-127](../../../src/aidhu_om_agent/api/app.py#L97-L127)）**均已实现**；本阶段只**核对**两种运行方式并补文档，不重复实现。
14. **不引入任何新依赖**（随 §0.2 第 4 项）。

### 0.4 核对到的实际代码（讨论依据）

**已交付、本阶段不重复实现**：

- **上传与预检接口已在 S03 交付、S04 落库**：`POST /api/uploads`（扩展名、`max_upload_bytes` 超限 413、工作簿不可读 422）、`POST /api/uploads/{upload_id}/validate`（blocked 用 200 返回结构化报告）。文件名只作元信息、服务端生成存储名、登记失败不留孤儿文件，均已实现（[services/uploads.py](../../../src/aidhu_om_agent/services/uploads.py)）。
- **批次接口已在 S04 交付**：`POST /api/runs`（幂等）、`GET /api/runs`（**已有** `page`／`page_size`／`status`）、`GET /api/runs/{run_id}`、`POST /api/runs/{run_id}/resume`（幂等）。
- **导出面已在 S05 交付**：`POST`／`GET /api/runs/{run_id}/exports`（幂等）、`GET /api/artifacts/{artifact_id}/download`；`/runs/{run_id}` 的导出区块与轮询已在界面可用。
- **前端骨架已在 S03 交付**：三条路由（`/`、`/runs`、`/runs/:runId`）、模拟/真实切换、上传→预检→判一条单页、批次列表与详情页（含导出区块）。
- **静态服务已实现**：`vite.config.ts` 的 dev 代理，以及 `api/app.py` 的 SPA 回落（`/api` 前缀返回 404 JSON，不被 `index.html` 吞掉）。

**本阶段要补的缺口**：

| 缺口 | 证据 |
| --- | --- |
| `GET /api/runs/{run_id}/records`（分页 + 筛选）**不存在** | [runs.py](../../../src/aidhu_om_agent/api/routes/runs.py) 只有 run 层接口 |
| `GET /api/runs/{run_id}/records/{record_key}`（详情）**不存在** | 同上 |
| `repositories/records.py::list_records` 是**整批返回、无分页无筛选** | [records.py:189](../../../src/aidhu_om_agent/repositories/records.py#L189) |
| `GET /api/jobs/{job_id}` **只查内存注册表**，落库任务查不到 | [jobs.py:89-95](../../../src/aidhu_om_agent/api/routes/jobs.py#L89-L95) |
| 上传防护只有压缩包字节数与记录条数，**plan/08 §3 要求的解压总量／单元格／工作表保护未实现** | [config.py:127-133](../../../src/aidhu_om_agent/config.py#L127-L133)、[reader.py:194](../../../src/aidhu_om_agent/excel/reader.py#L194)、[reader.py:457](../../../src/aidhu_om_agent/excel/reader.py#L457) |
| `partial_failed` 写 `last_error={code: PARTIAL_FAILED, failed_count}` | [worker.py:621-625](../../../src/aidhu_om_agent/worker.py#L621-L625) |
| **诊断产物只有临时路径在写**（`JudgeJobRegistry._write_diagnostic`），worker 路径不写 | [judging.py:247](../../../src/aidhu_om_agent/services/judging.py#L247) |
| 被拒原文**已落库**（`call_attempts.final_content`，失败尝试经 [judging.py:472](../../../src/aidhu_om_agent/services/judging.py#L472) 写入，超长保头尾），但**只有 `/api/judge` 会发给界面** | `AttemptRow.final_content` 已被读取 |
| `/runs` 列表**无筛选分页控件**；`/runs/{id}` **无记录列表与证据详情**（页面第 425 行自己注明属 S06） | [RunsView.vue](../../../src/frontend/pages/RunsView.vue)、[RunDetailView.vue](../../../src/frontend/pages/RunDetailView.vue) |
| `tests/e2e/`、`tests/unit/frontend/` **只有 `.gitkeep`**，无 vitest／playwright 依赖 | `package.json`、`pnpm-lock.yaml` |

**可复用的现成结构**：首页单条结果区**已经能渲染** stage1 证据（`ref_id` + `quote`）与 stage2 更正——记录详情页照这套结构做，不需要新设计。

### 0.5 v1.3 → v2.0 的范围变化

| 项 | v1.3 写法 | v2.0 |
| --- | --- | --- |
| 上传与预检接口 | 「本阶段实现（S06-01）」 | **已由 S03／S04 交付**；本阶段只补防护与错误合同（S06-02） |
| 导出与下载 | 「本阶段首次实现」 | **最小导出面已由 S05 交付**；本阶段只做记录侧与历史打磨（§0 原表已注明） |
| 记录与证据 | 只写「实现页面及证据查看」 | 拆为**接口（S06-01）**与**界面（S06-04）**两步，并明确详情独立路由 |
| 临时接口 `/api/judge` | 未提及去留 | **本阶段删除**（§0.2 第 1 项） |
| S03 返工成果 | 未提及 | **本阶段承接**（被拒原文进详情、诊断产物迁到 worker；§0.2 第 5、6 项） |

## 1. 阶段目标与边界

本地浏览器完成完整闭环：上传 Excel → 预检 → 启动整批 → 看进度 → **筛选与分页浏览记录** → **打开单条记录核对原始 q/a/ref 与分析证据** → 发起恢复 → 导出并下载；关闭页面不影响 worker。骨架、批次面与导出面已在 S03—S05 交付，本阶段把它们补成完整闭环。

**边界**：

- 不做在线人工改标签、复核回读与评估页面（属 S07）。
- 不改变三分类与复核规则；**不引入任何新依赖**；**全程零真实模型调用**。
- 不放宽导出字段合同（S05 已定稿）；不放宽 `raw_output` 的暴露范围——**只在记录详情**，列表与导出不带（§0.3 第 3 项）。
- 启动/停止脚本到 S09 完成，本阶段提供经过验证的直接启动命令。

## 2. 可实施小步骤

| 步骤 | 实施内容 | 主要位置 | 交付与验证 | 开发前需讨论 |
| --- | --- | --- | --- | --- |
| S06-01 | 记录查询接口：列表（分页 + 筛选）+ 详情 | `api/routes/runs.py`、`api/schemas.py`、`services/batches.py`、`repositories/records.py` | 参数、排序、`revision`、筛选语义与 [plan/08 §6](../../../plan/08-API接口与数据合同.md) 逐条一致；列表不传 `a` 与全部 ref；详情含完整 input、stage1/stage2、failure、attempt_summary 与失败尝试的 `raw_output` | 分页默认与上限、`q_preview` 长度、`reason` 来源、`record_id` 精确匹配口径 |
| S06-02 | 上传防护、错误合同收口与任务查询落到持久化任务 | `excel/reader.py`、`api/routes/uploads.py`、`api/routes/jobs.py`、`services/uploads.py` | 解压总量／单元格／工作表保护生效且报 422 `BAD_WORKBOOK` 并给出实际值与上限；`413` 与 `422` 不混用；`GET /api/jobs/{job_id}` 按库返回 [plan/08 §7](../../../plan/08-API接口与数据合同.md) 字段 | 防护参数取值（§0.3 第 7 项）、错误文案、`record_key` 语义调整 |
| S06-03 | 批次层面收尾：`last_error` 语义分工 + `/runs` 列表筛选分页 | `worker.py`、`services/batches.py`、`RunsView.vue` | `partial_failed` 不再写 `last_error`；界面横幅条件显示并给出失败数；`/runs` 可筛选状态与分页且条件进 URL query；存量 `last_error` 仍正确显示 | 横幅文案、筛选与分页同 URL 的关系、空态 |
| S06-04 | 记录列表、筛选分页与证据详情界面 | `RunDetailView.vue`、新 `RecordDetailView.vue`、`router/index.ts`、`api/client.ts`、`types/api.ts` | 见 §2.1；`pnpm typecheck` 与 `pnpm build` 退出码 0 | 列表列与筛选控件、详情分区、证据展示、深链接 |
| S06-05 | 首页改造与轮询、交互打磨 | `JudgeView.vue`、`RunDetailView.vue`、`api/client.ts`、`types/api.ts` | 首页删除单条判别与模式开关、模式改只读指示；`revision` 变化刷新当前页；重复提交受控；失败/复核可分辨；错误不被页面兜底吞掉 | 只读指示的取值与措辞、刷新时机、按钮条件、空状态 |
| S06-06 | 本地开发与静态服务核对 | `vite.config.ts`、`api/app.py`、`tests/e2e`（策略另定） | dev 三进程与构建后二进程均可访问并走通闭环；深链接刷新可用；`/api` 前缀错误不被兜底吞 | 端口/代理核对方式、界面外检查清单 |

### 2.1 界面清单（S06-04／S06-05，用户验收路径）

**保留**：`/` 的上传 → 预检流程（含样例下载、工作表选择、预检计数、blockers 与 row_errors）、`/runs` 批次列表、`/runs/{run_id}` 详情页的既有区块（状态、计数、进度、任务、调用统计、失败摘要、恢复面板、导出区块）。

**S06-04 新增——批次详情页的「记录」区块**：

1. **记录表格**：按 `order_index` 升序；列 = 序号（`order_index` + 1）／编号（`record_id`，空显示「（无编号）」）／来源行／q 摘要／状态／标签／需复核／失败码。
2. **筛选控件**：状态、标签、需复核（是／否／全部）、编号（精确匹配）；多项 AND；**条件写进 URL query**，刷新与分享后仍生效；「清除筛选」按钮。
3. **分页控件**：默认 50，可选 20／50／100／200；显示筛选后的 `total` 与当前 `revision`。
4. **空态**：无记录、全部被筛选掉、批次还在跑三种情况文案不同（不把「还没跑」显示成「没有失败」）。

**S06-04 新增——记录详情页 `/runs/:runId/records/:recordKey`**：

5. **原始输入**：`record_id`、来源行、完整 `q`、完整 `a`、`ref1`—`ref10` 全文（为空显示「（空）」而不隐藏）。
6. **阶段一**：资料充分性、证据列表（`ref_id` + `quote` 逐字）、必需点、缺失信息、冲突。
7. **阶段二**：理由、`review_required` 与复核原因、问题、`stage1_corrections`（更正项、原因、是否影响分类）。
8. **失败信息**：失败阶段、错误码、错误消息、尝试次数、是否可重试；未失败时显示「本条无技术失败」。
9. **调用尝试摘要**：逐次列出阶段、第几次、结果、模型、耗时、是否模拟、用量；**被校验拒绝的那几次可展开查看 `raw_output`**（超长时显示截断标记），并在页面上说明这是**模型输出正文、不是推理链**。
10. **返回**：回到带原筛选条件的记录列表（用 URL query 还原）。
11. 页面顶部显示该记录所属批次与状态；`revision` 变化时刷新本条（批次仍在跑时）。

**S06-05 改动——首页**：

12. **删除**单条判别的按钮、结果区与模拟/真实开关；保留上传、预检与「开始判别（整批）」。
13. **模式改为只读指示**：显示当前 worker 的实际模式（取自批次详情的 `execution_control.last_worker.mode` 与「含模拟调用（N/M 次）」），并明确「整批模式由 worker 启动参数决定，页面不可切换」。
14. 未启动 worker 时给出提示（批次会排队但不执行），不把排队显示成失败。

**S06-05 改动——批次详情页**：

15. `partial_failed` 不再显示成红色错误横幅，改为**收尾摘要**：「失败 N 条、输入无效 M 条」，并可一键跳到筛好的失败记录列表。
16. 失败摘要区块的「单条详情属 S06」文案改为**可点击进入记录详情**。

**S06-03 改动——`/runs` 列表**：状态筛选 + 分页控件，条件进 URL query；更新「筛选分页属 S06」的过时文案。

## 3. 阶段完成条件

用户通过浏览器完成批处理闭环；进度、最终判断、处理失败及需复核记录展示清楚；**任一记录可打开、看到原始 q/a/ref 与分析证据**，被校验拒绝的模型输出仍可查看。

步骤开发完成后自动执行以下审阅，保存 reviews/automated/S06/r01-自动化审阅.md。命令必须在实际开发时验证后填写；不能把尚未安装工具或未实现测试标为通过。

- 核对记录列表与详情的参数、排序、分页、筛选语义、`revision` 与 [plan/08 §6](../../../plan/08-API接口与数据合同.md) 逐条一致；非法枚举与分页值返回 422。
- 核对上传防护：解压总量／单元格／工作表三项各自触发一次，确认报 422 `BAD_WORKBOOK`、给出实际值与上限、**不裁剪资料**，且 `413` 仍只用于压缩包超限。
- 核对 `partial_failed` 不再写 `last_error`、`_fail_systemically` 仍写；**存量带 `last_error` 的批次在界面上仍正确显示**（两者都要有用例）。
- 核对被拒原文只在**记录详情**出现：列表与导出**不含** `raw_output`；诊断产物由 worker 路径生成且命名不互相覆盖。
- 核对 `/api/judge` 已删除（调用返回 404）、`GET /api/jobs/{job_id}` 能查到落库任务；确认删除未波及 `execute_record` 与 worker 的执行路径。
- 执行 `python -m pytest`、`pnpm typecheck`、`pnpm build`（均须退出码 0）；真实 uvicorn 冒烟走「上传 → 批次 → 进度 → 记录 → 详情 → 导出 → 下载」，并复验深链接刷新与 `/api` 前缀 404 不被 `index.html` 吞掉。
- 复验与本阶段有关的关键回归：幂等创建/恢复、中断恢复、导出成对发布、`mock` 与 `real` 模式的可分辨性。

分列报告：工程模拟测试与实现完整性、实际模型服务兼容性（**本轮零真实调用，沿用 S03-06 既有结论，不重复付费验证**）、人工核定下的分类质量（**本轮不适用，属 S07**）。

## 4. 阶段结束需生成的手动操作审阅文档（界面路径）

自动化审阅通过后，根据真实实现生成 reviews/manual/S06/r01-手动操作审阅.md。

指南至少覆盖：

1. **前置启动命令**（规则允许的必要命令）：`python -m aidhu_om_agent serve` 与 `python -m aidhu_om_agent worker`，**全程模拟模式、零费用**。
2. 上传样例 → 预检 → 「开始判别（整批）」→ 在详情页看到进度与终态。
3. **筛选与分页**：按「需复核 = 是」筛选、翻页、记下计数；**刷新页面**确认筛选条件仍在；对照详情页的 `revision` 与列表 `total`。
4. **打开一条记录**：核对原始 q/a/ref1—ref10 与阶段一证据摘录、阶段二理由；对一条**技术失败**记录展开被拒的 `raw_output`（模拟模式下用 `synthetic-forced-failure.xlsx` 造出），确认页面上标注「模型输出正文，不是推理链」。
5. **改标签？不改**：本阶段不做人工改标签，指南明确说明该能力属 S07，避免用户误以为遗漏。
6. **`partial_failed` 收尾摘要**：确认不再是红色错误横幅，而是「失败 N 条」并可跳到筛选好的失败列表。
7. 用户结果填写表（**不代填**）、反馈要求、停止/恢复及文件保存说明。

### 4.3 界面外检查（单列小节，写明原因与替代证据）

| 检查 | 为何无法界面化 | 界面内可核对的替代证据 |
| --- | --- | --- |
| 上传防护三项的具体阈值 | 需要构造超限文件并观察拒绝 | 界面上传超限文件后的错误提示（实际值与上限），配合自动化轮的三组注入用例 |
| `413` 与 `422` 的错误码区分 | 错误码只在响应体 | 界面错误提示文案区分「文件太大」与「工作簿或其规模不可用」 |
| `/api/judge` 已删除、`GET /api/jobs/{job_id}` 已落库 | 是接口层，无对应按钮 | 首页不再出现单条判别；自动化轮的接口用例与 404 断言 |
| 诊断产物文件与命名 | 是工作区文件 | 记录详情页可看到被拒原文；文件位置与命名在自动化轮核对 |
| dev 三进程与构建后二进程 | 是进程与端口配置 | 两种方式下界面都能走通同一流程；深链接刷新可用 |

本阶段结束时**所有已实现能力都应有界面入口**；不得让用户下载空产物或无入口查看的结论。

## 5. 交付与进入下一阶段

交付：实际源码/配置/测试、本文件批准版本、自动化审阅报告、界面版手动指南、阶段状态、交付标识（提交号或 sha256 清单）。用户操作结果单独记录，不代填通过。

**本阶段已交付并经用户接受（2026-10-07）**：界面版手动指南为 [r01-手动操作审阅](../reviews/manual/S06/r01-手动操作审阅.md)，用户以原话「阶段6手动审阅通过，我们进行阶段7的讨论」接受，结果与记录边界见 [r01-用户审阅结果](../reviews/manual/S06/r01-用户审阅结果.md)（用户未逐条回填指南结果表；经**只读查库**，A 组与 D 组有本机批次佐证，**B／C／E／F 四组不写库、无独立痕迹、未记为已通过**）。**交付标识 = 提交 `a46716a`**（"阶段6"，2026-10-07，用户手动提交并推送；交付时工作区未提交、曾以 `data/runtime/s06_code_identity.json` 的 33 条 sha256 清单标识，提交后按提交树逐项核对 **33/33 逐字一致、0 处 EOL 差异**后回填）。

用户接受本阶段后，进入 S07（人工基准与规则校准）的开发前讨论——分类质量与人读字段属 S07，本阶段**不作分类准确率结论**。

用户已批准范围内的常规开发、验证与缺陷修复不重复申请相同授权；出现下列情形须先更新本文件并取得对应审阅：改变三分类或复核规则、放宽记录详情与导出字段合同（含 `raw_output` 暴露范围）、扩大界面范围、引入新依赖、**任何真实模型调用**。

## 6. 审批记录

| 项 | 内容 |
| --- | --- |
| 讨论日期 | 2026-10-07 |
| 批准日期 | **2026-10-07** |
| 用户原话 | 「**确认无误，批准开始**」（就本文档 v2.0 全文的提问「要改哪条我改完再定稿；确认无误请给出批准原话」作答） |
| 批准版本 | 本文件 **v2.0**（讨论定稿即为 v2.0，批准未改步骤内容） |
| 覆盖步骤 | **S06-01、S06-02、S06-03、S06-04、S06-05、S06-06 全部 6 个步骤** |
| 一并确认 | §0.3 第 1 项**全程零真实调用**；§0.3 第 2 项记录列表分页**默认 50、上限 200**（`/runs` 列表沿用 plan/08 §5 的默认 50、上限 100）；§0.3 第 7 项上传防护三个数值（解压总量 **8 × `limits.max_upload_bytes`**、单表 **500,000** 单元格、可见工作表 **50** 个） |
| 批准范围含 | §0.1 三项既有决定、§0.2 七项用户决定、§0.3 十四项技术定稿、§2.1 界面清单（用户验收路径）、§4 界面版手动指南要求 |
| 明确未批准 | 改变三分类或复核规则、放宽记录详情与导出字段合同（含 `raw_output` 暴露范围）、在线人工改标签、扩大界面范围、引入新依赖、**任何真实模型调用**（S03-06 的 10/10 预算仍为用满） |
| 状态变更 | 待讨论 → 待用户批准（2026-10-07 讨论定稿并直接写入本文件）→ **已批准**（2026-10-07，用户原话「确认无误，批准开始」）→ 开发中 → **待手动审阅**（2026-10-07 六步全部实现、自动化审阅通过、界面版手动指南已交付；用户结论未填、未代填）→ **阶段已接受**（2026-10-07，用户原话「阶段6手动审阅通过，我们进行阶段7的讨论」，见下方验收记录行） |
| 验收记录（2026-10-07） | 用户原话「**阶段6手动审阅通过，我们进行阶段7的讨论**」——**接受**。验收方式：**前端界面操作**（页面上传 → 预检 → 整批 → 记录列表 → 记录详情），全程**模拟模式、零真实模型调用**。用户**未逐条回填**指南 §2 结果表、**未提供截图**；开发者以**只读查库**核对（`file:data/state.sqlite3?mode=ro`）：**A 组**（`synthetic-mixed-outcome.xlsx`，批次 `2c7a0b06…`，`09:32:53`，`partial_failed`、`revision 6`、计数 3/3/0、已分类 2、技术失败 1、需复核 2，记录 FF-1／M-1／M-2 与自动导出一份）与 **D 组**（`synthetic-partial-errors.xlsx`，批次 `822ab68d…`，`09:38:16`，`partial_failed`、`revision 2`、总 4／有效 1／输入失败 3／空行 2）各有本机批次佐证，与指南「应观察」逐项一致；**B／C／E／F 四组不写库、无独立痕迹，未记为已通过**；D6 的阻断样例按设计不建批次。**不构成分类准确率结论**（模拟模式；质量属 S07）。边界见 [r01-用户审阅结果](../reviews/manual/S06/r01-用户审阅结果.md) §2、§3 |
| 验收后未表态项 | 界面上「序号 = `order_index` + 1」的呈现粒度、`raw_output` 在详情页的展示粒度、上传防护三个内置常量的取值——用户本轮只给结论，**未逐项表态，保持现状、未记为已确认** |
| 交付记录（2026-10-07） | **S06-01—S06-06 全部实现**。[r01-自动化审阅](../reviews/automated/S06/r01-自动化审阅.md)：**通过，待手动审阅**——`python -m pytest -q` **637 passed / 2 skipped**（S05 交付时 594，本轮新增 43）、`pnpm typecheck` 与 `pnpm build` 退出码 0（产物 `assets/index-UZaMuiYy.js`）、**真实 uvicorn 构建产物模式**冒烟（`/api/...` 恒返回 404 信封、静态写入 405、记录深链接由 SPA 回退承接）、参数边界与回归、**零真实调用**。界面版手动指南：[r01-手动操作审阅](../reviews/manual/S06/r01-手动操作审阅.md)。**有意修改的已验收界面行为 4 项**（§0.3 第 10 项）已在报告 §1.1 逐条列出。**本阶段状态为「待手动审阅」——开发者不代填用户结论** |
| 交付标识 | **提交 `a46716a`**（"阶段6"，2026-10-07，用户手动提交并推送）。交付时工作区未提交（开发前最近提交 `cd14aba`），按 [审阅交付规则](../../../.claude/rules/review-and-handoff.md) §2 以 `data/runtime/s06_code_identity.json` 的 **33 条代码/测试路径 sha256 清单**标识；用户提交后按**提交树**逐项核对（`git cat-file blob HEAD:<path>` 与工作区、再去 `\r` 比对）：**33/33 逐字一致、0 处 EOL 差异、0 内容差异、0 缺失**，`git status --porcelain` 为空，故回填为上述提交号 |
