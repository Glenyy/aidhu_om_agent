# 测试与阶段审阅入口

当前检查包括文档/目录清单、相对链接、步骤/规则一致性和原文件保留，以及 [36 个依赖版本的解析及元信息检查](implementation/handoffs/S01-依赖版本解析记录.md)。依赖没有安装，源码、配置和脚本继续为占位。

业务与工程测试尚未开发，模型兼容性、前端类型检查/构建和分类验收尚未执行；不能把文档检查记为 S01—S09 的程序审阅通过。

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

当前只有 [自动化模板](implementation/templates/自动化审阅报告模板.md) 和 [手动模板](implementation/templates/手动操作审阅模板.md)，没有阶段通过结论。实际工具/命令到阶段开发时补齐并验证。
