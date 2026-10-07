/**
 * 与后端枚举一致的**筛选取值**（S06-04）。
 *
 * 这里只是把「能作为查询参数的值」列出来给下拉框用，**不在这里判断业务结论**：
 * 标签、状态、是否需复核一律按接口返回值展示（.claude/rules/frontend.md）。
 * 改这里就必须同步后端：[plan/08 §6] 的记录列表枚举来自
 * `services/batches.py` 的 `RECORD_LABELS`／`RECORD_STATUSES`。
 */

/** 三分类标签；顺序与后端一致。 */
export const RECORD_LABELS = ['回答正确', '未检索到正确资料', '检索到正确资料但回答错误'] as const;

/** `records.status`。 */
export const RECORD_STATUSES = [
  'pending',
  'input_invalid',
  'stage1_done',
  'completed',
  'failed',
] as const;

/** 记录列表每页条数；后端上限 200（S06 阶段文档 §0.3 第 2 项）。 */
export const RECORD_PAGE_SIZES = [20, 50, 100, 200] as const;

/** 批次列表每页条数；后端上限 100（plan/08 §5）。 */
export const RUN_PAGE_SIZES = [20, 50, 100] as const;

/** 评估历史每页条数；后端上限 100（S07，`services/evaluation.py`）。 */
export const EVALUATION_PAGE_SIZES = [20, 50, 100] as const;

/**
 * 「是否一致」的三态筛选值（S07-03 的逐条对照）。
 *
 * 只列 `true`／`false`：**没有预测的行 `agree` 是空值**，要用 `record_status` 筛
 * （`failed` 是技术失败、`pending`／`stage1_done` 是没跑完）。把「没判出来」混进
 * 「不一致」会让人以为模型判错了，这是两种完全不同的质量信号。
 */
export const EVALUATION_AGREE_VALUES = ['true', 'false'] as const;
