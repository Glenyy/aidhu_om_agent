-- S04-04：worker 归属与「模拟/真实」可见性（结构版本 2）。
--
-- 为什么需要这两列：
--   1) `call_attempts.simulated` —— 模拟模式的调用必须在**数据层**留下痕迹。
--      [.claude/rules/review-and-handoff.md §4.3] 要求「模拟模式的界面必须有明显
--      标识，防止模拟结果被当成真实结果」；只靠 `returned_model_id` 里的
--      "mock-deterministic" 字样是约定，不是约束。
--   2) `runtime_state.worker_mode` —— 批次详情页要显示当前 worker 处于哪种模式，
--      否则用户无法判断屏幕上的结果是模拟还是真实（零费用还是计费）。
--
-- 两列都**不是**业务标签，不进入三分类；`simulated` 默认为 0（真实），
-- 只有模拟客户端写入 1，避免缺省值把未知调用误标成模拟。

ALTER TABLE call_attempts ADD COLUMN simulated INTEGER NOT NULL DEFAULT 0
    CHECK (simulated IN (0, 1));

ALTER TABLE runtime_state ADD COLUMN worker_mode TEXT
    CHECK (worker_mode IS NULL OR worker_mode IN ('mock', 'real'));
