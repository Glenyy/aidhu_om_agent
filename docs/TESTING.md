# 测试与阶段审阅入口

当前检查包括文档/目录清单、相对链接、步骤/规则一致性和原文件保留，以及 [36 个依赖版本的解析及元信息检查](implementation/handoffs/S01-依赖版本解析记录.md)；依赖已在用户 Conda 环境 `aidhu_agent` 中安装并核验为 36/36 匹配。

**已执行的检查（截至 2026-10-07，S05 交付）**：`python -m pytest -q` → **594 passed, 2 skipped**（2 项跳过为需显式授权的真实调用用例；S03 交付时 319、S04 交付时 509）；`pnpm typecheck` 与 `pnpm build` 退出码 0；真实运行的 uvicorn 服务端冒烟（模拟模式）多轮通过，S05 两轮为「导出→下载→用 openpyxl 打开核对」与「中断批次导出→恢复跑完→出现新的自动导出」；S05 另以**真子进程**逐条实跑命令行退出码 0/1/2/4/5 与 `--config` 优先级。真实模型服务的**格式与参数兼容性**以 10 次调用实测（结果见 [S03 自动化审阅](implementation/reviews/automated/S03/r01-自动化审阅.md) §3.6；**S04、S05 全程零真实调用**）；前端**浏览器内的实际渲染与点击未由开发者执行**（无浏览器自动化依赖），由用户按各阶段 [界面版手动指南](implementation/reviews/manual/S05/r01-手动操作审阅.md) 确认。

**尚未执行**：人工核定下的**分类质量验收**（属 S07/S08）；记录列表与证据详情、筛选分页、完整任务/产物历史（属 S06）、`evaluate` 的指标计算（属 S07）。工程测试通过、服务兼容与分类质量三者分别报告，不能互相代替，也不能把文档检查记为程序审阅通过。

| 路径 | 后续用途 |
| --- | --- |
| tests/unit/backend | pytest 后端关键逻辑 |
| tests/unit/frontend | Vitest 前端关键逻辑 |
| tests/integration | API、SQLite、worker、多进程与中断恢复 |
| tests/e2e | Playwright 上传到下载关键流程 |
| tests/fixtures | 合成样例与模拟响应 |

业务场景与指标见 [测试规划](../plan/04-测试与质量评估.md)。每阶段必需检查和手动场景见 [实施阶段](implementation/README.md)。

开发完成后主动执行 [审阅交付规则](../.claude/rules/review-and-handoff.md)：记录实际命令及退出结果、代码/合同核对和必要回归。模拟测试、真实服务兼容及人工核定分类成绩分别报告。

报告位置：

- 自动化结果：docs/implementation/reviews/automated/<阶段ID>/r01-自动化审阅.md。
- 实际手动指南：docs/implementation/reviews/manual/<阶段ID>/r01-手动操作审阅.md。
- 用户操作结果：同阶段 manual 目录，单独保存来源与结论。

[自动化模板](implementation/templates/自动化审阅报告模板.md) 与 [手动模板](implementation/templates/手动操作审阅模板.md) 为写作模板；实际结论：S01—S05 **阶段已接受**（各阶段报告见上述路径）。实际工具/命令到阶段开发时补齐并验证。
