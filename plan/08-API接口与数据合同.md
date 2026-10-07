# 08 API 接口与数据合同

规划版本：v2.3｜日期：2026-10-05｜状态：待用户审阅

本文件细化本地 Vue 前端与 FastAPI 的接口。与[数据库设计](09-数据库与持久化设计.md)、[状态与恢复](10-任务状态与恢复设计.md)配套使用。接口是拟定合同，尚未实现；业务三分类与质量标准不变。

## 1. 通用约定

- 所有接口使用 /api 前缀，JSON 字段采用 snake_case；ID 采用程序生成的 UUID 字符串。
- 输入编号称为 record_id；内部查询键称为 record_key。输入编号可能缺失或包含不适合 URL 的字符，不直接作为路径键。
- 数据时间统一为 UTC ISO 8601，例如 2026-10-05T02:00:00Z；页面转换为本地时间。业务上的提问时间不能用运行时间代替。
- 成功 JSON 使用 data 与 request_id；错误 JSON 使用 error 与 request_id；下载接口直接返回文件。
- 未产生判断、用量未知、尚未开始的时间等使用 null，不用空字符串或零冒充。
- 状态和 label 由后端返回，前端不根据理由自行推断类别。label 只允许三个中文字符串，未完成/失败时为 null。
- 进度和结果查询不缓存。编号、原文及引文按文本展示，不执行原文中的指令或网页代码。

错误示例：

~~~json
{
  "error": {
    "code": "STATE_CONFLICT",
    "message": "该批次已有排队或运行中的判别任务",
    "details": [{"field": "run_id", "job_id": "<已有任务ID>"}]
  },
  "request_id": "<本次请求ID>"
}
~~~

## 2. 接口总表

| 方法与路径 | 请求 | 成功状态与返回 |
| --- | --- | --- |
| POST /api/uploads | multipart：file | 201；upload_id、文件信息、工作表列表 |
| POST /api/uploads/{upload_id}/validate | sheet_name，可选 | 200；validation_id、预检状态、统计和问题 |
| POST /api/runs | validation_id | 202；run_id、job_id |
| GET /api/runs | page、page_size、status | 200；分页批次列表 |
| GET /api/runs/{run_id} | 无 | 200；批次、进度、当前任务、允许操作、导出概况 |
| GET /api/runs/{run_id}/records | 分页与筛选参数 | 200；分页记录摘要 |
| GET /api/runs/{run_id}/records/{record_key} | 无 | 200；原始输入、阶段结果、失败与调用摘要 |
| POST /api/runs/{run_id}/resume | retry_failed，默认 false | 202；run_id、job_id、选中记录数、重开预算数 |
| POST /api/runs/{run_id}/exports | 空 JSON 对象 | 202；export_id、job_id |
| GET /api/runs/{run_id}/exports | page、page_size | 200；历史导出与文件标识 |
| GET /api/jobs/{job_id} | 无 | 200；任务状态、活动阶段、错误、结果标识 |
| GET /api/artifacts/{artifact_id}/download | 无 | 200；已登记的完整文件 |
| GET /api/runs/{run_id}/evaluations | page、page_size | 200；该批次的评估历史（新→旧） |
| GET /api/evaluations/{evaluation_id} | 无 | 200；指标、达标判定、划分要点、冻结清单与报告正文 |
| GET /api/evaluations/{evaluation_id}/records | 分页与筛选参数 | 200；逐条对照（人工标签 vs agent 原预测） |

运行模型、修改模型配置和人工编辑标签不属于网页接口。质量评估的计算入口仍只有 CLI；S07 新增**只读**评估展示页，不提供网页计算或标签编辑入口。

上传使用 multipart/form-data；其余带请求体的接口使用 JSON。FastAPI 官方文档说明文件上传的表单编码不能与同一请求中的 JSON Body 混用，所以先上传获得 upload_id，再通过 JSON 完成预检和创建。[FastAPI 文件与表单](https://fastapi.tiangolo.com/tutorial/request-forms-and-files/)

## 3. 上传和预检

### 上传

file 必需，接受 .xlsx。初始保护值为压缩文件大小 50 MiB、非空记录最多 1000 条，均在后端配置；这些是建议的工程边界，开发时用最长样例验证。超出限制拒绝处理，不裁剪资料或只取前 1000 条。

文件流采用受限读取。另设解压总量、单元格及工作表读取保护，开发兼容性验证后锁定参数，避免仅检查压缩文件大小。原始文件不修改；服务端生成存储名，原文件名只作元信息。

返回：

~~~json
{
  "data": {
    "upload_id": "<UUID>",
    "original_filename": "qa.xlsx",
    "size_bytes": 120000,
    "sha256": "<文件摘要>",
    "sheets": [
      {"name": "QA_REF", "visible": true}
    ],
    "suggested_sheet": "QA_REF"
  },
  "request_id": "<UUID>"
}
~~~

上传成功不代表内容通过预检，也不调用模型。工作簿不可读取时返回 422，大小超限返回 413。

### 预检

请求体为 {"sheet_name":"QA_REF"}。允许省略 sheet_name：优先 QA_REF，否则仅有一个可见表时自动选择；无法确定时返回 blocked，并列出工作表，由用户明确选择。

预检结果字段：

| 字段 | 类型与规则 |
| --- | --- |
| validation_id | UUID；一次预检的不可变标识 |
| status | passed / blocked |
| sheet_name | 实际选定表；未确定时 null |
| input_contract_version | 输入解析与规范版本 |
| counts | total、valid、input_invalid、skipped_blank_rows；不可统计时对应值为 null |
| blockers | 批次问题，含 code、message、列名/来源行等 |
| row_errors | 单条输入问题，含 source_row、可读编号、原因 |
| warnings | 不阻止创建的提示，例如额外列未使用 |

能够统计时 counts.total = valid + input_invalid；空行另计。必要列缺失、重复编号、工作表不确定、超过记录限制或没有有效记录时 blocked。单条 q/a/编号缺失不阻止其余有效记录创建，但必须在 row_errors 明示。

资料单元格为空不构成输入错误。首版不隔离原人工标签；额外列只忽略，不传给模型。

每次预检固定文件摘要、工作表和解析版本。创建时重新确认文件未变；若发生变化或解析版本不兼容，拒绝使用旧 validation_id。不可读取的单元格保留来源位置，不能把错误值当作资料。

## 4. 创建与重复请求

创建请求只包含 validation_id。模型 ID、地址、超时等由后端读取配置并冻结；浏览器不提交服务地址、密钥或模型参数。

创建、恢复和导出接口要求 Idempotency-Key 请求头，由前端为一次用户动作生成 UUID。网络结果不确定时保留原 key 与请求体后重试；明确开始另一次动作时使用新 key。

后端以“方法 + 标准化路径 + key”为范围，并校验请求体摘要：

- 同 key、同请求返回原资源 ID 和原成功状态码，不创建第二项任务。
- 同 key、不同请求体返回 409 IDEMPOTENCY_CONFLICT。
- 幂等记录与批次/任务同事务提交。数据库写入失败不返回创建成功。
- 成功响应的 data 保存后复用，request_id 每次生成；资源最新状态以 GET 为准。
- 失败操作不留下已接受任务的幂等记录，修正后可重试。
- 幂等记录首版不自动到期，不通过文件内容摘要禁止有意重新运行。

不同 key 的重复恢复仍受到“一批次仅一个活跃判别任务”的约束。上传与预检不要求该请求头；它们不产生模型调用。

页面在未知结果时保存待确认动作，刷新后先重试同一请求。创建按钮等待响应时禁用；仅禁用按钮不能代替后端幂等性。

## 5. 批次和进度

GET /runs 列表默认每页 50 条，上限 100，按 created_at、run_id 倒序；筛选 status 仅接受合法批次状态。分页响应统一 items、page、page_size、total。

批次详情核心字段：

| 字段 | 类型/含义 |
| --- | --- |
| run_id、original_filename、sheet_name | 来源与批次标识 |
| status | queued / running / completed / partial_failed / failed / interrupted |
| created_at、started_at、finished_at | 开始前或未终止的时间为 null |
| revision | 每次可见批次/记录状态提交后递增，用于识别数据变化 |
| counts | total、valid、input_invalid、classified、failed、remaining、processed、review_required |
| progress_percent | processed / total × 100，展示保留一位小数 |
| active_job | 活跃判别任务；没有时 null |
| recent_jobs | 最近若干次任务摘要（job_id、kind、mode、status、时间、当前记录/阶段、错误）；批次结束后 active_job 为 null，界面靠它看到刚跑完那次落到什么状态 |
| execution_control | 后端模型消费是否暂停、暂停原因及最近 worker 标识；不据时间戳认定进程死亡 |
| model_config | 两阶段模型与已验证非敏感参数，不含凭据 |
| versions | 程序、规则、提示词、结构和输入合同版本 |
| last_error | 脱敏错误；没有时 null |
| allowed_actions | can_resume、can_retry_failed、can_export、可选/重开/跳过数、finalization_required 与禁用原因 |
| failure_summary | 失败记录摘要：count、受限条数的 items（record_key/record_id/source_row/order_index/failure_stage/code/message/retryable/attempt_count）与 truncated |
| call_statistics | 按阶段（stage1/stage2）的调用尝试统计：attempts、succeeded、failed、unknown_after_interrupt、simulated；恢复前后阶段一 attempts 不变即证明检查点被复用而非整条重跑 |
| latest_export | 最近导出任务与文件标识；没有时 null |

`recent_jobs`、`failure_summary`、`call_statistics` 由 2026-10-07 用户决定**追认进本节**：它们是界面验证面要求的可核对值（批次结束后仍能看到刚跑完任务的状态、失败摘要，以及「其中模拟」的调用计数），**均为只读**，不改变写入路径与状态机。`execution_control` 里的 `last_worker` 与 `latest_export` **本就在本节合同内**，不是新增字段——S04 阶段 `latest_export` 恒为 `null`（导出属 S05，不登记假产物）。

`last_error` 的**目标语义**是本行的「脱敏错误」。S04 当前实现对 `partial_failed` 批次写入 `{code: PARTIAL_FAILED, failed_count: N}`（批次收尾摘要，非系统性故障；系统性故障由 `execution_control.model_dispatch_paused` 表达），属实现与该语义的偏差。**2026-10-07 用户决定：在 S06 做语义分工**——收尾摘要改由 `failure_summary` 承载，`last_error` 只保留真正的错误，界面横幅相应改为条件显示（并补上现在未显示的 `failed_count`）。S06 之前按现状保留。

counts.failed 只计算有效记录的技术失败；input_invalid 单列。processed = classified + failed + input_invalid；remaining = total - processed。review_required 是 classified 的子集，不加到 processed。

上述计数与状态在同一读取快照中获得。重试选中失败项重新处理后，processed 可能下降；页面注明重新执行，不把此前失败进度当作当前成功数。failed 批次通常仍有 remaining，不强制显示 100%。

终止时间在恢复入队时清空；created_at 不变，操作历史由任务表保存。批次完成与导出完成分开显示。

## 6. 记录查询

列表支持 page、page_size、label、review_required、status、record_id（精确匹配）。多项筛选使用 AND；非法枚举或分页值返回 422。固定按输入 order_index 排序，返回 revision 和筛选后的 total。

记录摘要包含：

~~~text
record_key、record_id（可空）、source_row、order_index、
q_preview、status、label（可空）、reason（可空）、
review_required（未分类时可空）、failure（可空）
~~~

列表不传 a 和全部 ref。详情包含完整 input（record_id、q、a、ref1—ref10）、stage1、stage2、failure、attempt_summary。未取得阶段结果时对应项为 null；输入失败时 q/a 可为 null，以原工作簿与来源行追踪。

stage1/stage2 按详细设计的结构返回，包括资料充分性、证据和更正。证据元素为 {"ref_id":"ref3","quote":"原文摘录"}。每阶段原始记录保留，阶段二更正不覆盖 stage1。

详情不返回模型推理链、请求头或 SDK 调试堆栈。调用摘要可以包含次数、实际模型标识、耗时及服务返回的用量，缺失用量为 null。

被校验拒绝的尝试在**记录详情**中额外返回其最终正文（`raw_output`，超长保头尾并以 `raw_output_truncated` 标记）；成功的尝试不带正文，记录列表与导出同样不带。正文只用于人工核对失败原因，仍不包含推理链、请求头与调试堆栈。此决定见 [S06 阶段文档 §0.2](../docs/implementation/stages/S06-API与前端完整闭环.md)，它修改了 S04 原定的「`raw_output` 落本地库、不进 API 契约」。

分页是读取时的实时结果，不保证运行中不同页属于同一时刻。前端观察到 revision 改变时刷新统计及当前页，避免把不同轮次统计组合成最终报告。

## 7. 恢复、导出和下载

### 恢复

请求 {"retry_failed":false} 表示只继续剩余预算可以处理的未完成阶段，不重开次数。true 同时纳入需要新一轮预算的可重试失败项；输入失败、超限等必须修正输入/配置的问题不纳入。

后端先校验版本、状态和可选记录，返回 selected_records、renewed_campaigns（计划重开数量）、skipped_budget_exhausted、finalize_only 及 job_id。响应只表示入队，不表示已经调用模型。全部记录已保存但批次需收尾时允许 finalize_only=true、selected_records=0；它不调用模型。无可处理记录且无需收尾时返回 409 NOTHING_TO_RESUME；已有活跃判别任务返回 409 STATE_CONFLICT。

可重试范围、预算占用和系统性故障详见状态文档；前端使用 allowed_actions 展示按钮，提交时后端仍重新检查。

### 导出

空对象请求创建一份 Excel + JSONL 结果组。返回 export_id、job_id。export_id 与 artifact_id 分开：一份导出有两个文件。

导出记录包括 source（automatic/manual）、job_status、captured_at、run_revision、run_status_at_capture、counts_at_capture、artifacts、error。捕获时间未到时为 null。

快照在 worker 执行导出任务时捕获，不是在用户点击时捕获；排队期间可能已有新的分类结果。页面展示 captured_at 与对应 revision。导出可能包含未处理记录，必须在概况标明，不将其当作完整成功批次。

自动导出在判别任务结束时入队；其失败只改变导出任务状态。再次手动导出生成新 export_id，原文件及人工修改不被覆盖。

### 任务与下载

任务字段包括 job_id、run_id、kind（classify/export）、mode、status、created_at、started_at、finished_at、current_record_key、current_stage、error、result。current_stage 为 stage1 / stage2 / export / null，只显示阶段，不返回推理内容。

任务状态：queued、running、completed、partial_failed、failed、interrupted。partial_failed 只用于判别任务，导出需两个文件都登记成功才 completed。

下载通过 artifact_id 定位已登记文件，校验它属于完成的导出任务。响应使用文件下载名称；不接受用户磁盘路径。文件缺失返回 404 ARTIFACT_MISSING，返回明确错误，不当作空文件下载。

## 8. 错误码与开发验证

| HTTP | error.code | 处理 |
| --- | --- | --- |
| 404 | NOT_FOUND / ARTIFACT_MISSING | 展示资源不存在或文件缺失 |
| 409 | INPUT_NOT_VALIDATED / INPUT_SNAPSHOT_CHANGED | 重新预检或上传 |
| 409 | STATE_CONFLICT / NOTHING_TO_RESUME | 查询最新状态与允许操作 |
| 409 | VERSION_INCOMPATIBLE / IDEMPOTENCY_CONFLICT | 按原版本恢复或检查请求 |
| 409 | CONFIG_CHANGE_REQUIRED | 系统性故障需要改变快照，修正后新建批次 |
| 413 | FILE_TOO_LARGE | 调整输入或已验证配置，不截取内容 |
| 422 | PARAM_VALIDATION / BAD_WORKBOOK | 修正参数或文件 |
| 503 | CONFIG_INVALID / DB_BUSY / STORAGE_UNAVAILABLE | 展示配置或本地存储问题，未承诺入队成功 |
| 500 | INTERNAL_ERROR | 脱敏信息和 request_id，详细错误留后端日志 |

预检 blocked 使用 200 返回结构化报告，不与 HTTP 参数错误混淆。后台模型失败通过 job/run 的 error 返回，不让已完成的创建请求变成事后的 HTTP 500。

开发时生成 OpenAPI 合同并核对前端类型。重点验证重复创建、旧预检、新旧版本、筛选分页、下载缺失、真实计数、错误映射和后台失败。接口测试通过后再进行浏览器联调。
