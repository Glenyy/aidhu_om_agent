# 开发与交付文档入口

S01—S03 已实现并通过自动化审阅，**三个阶段均已获用户验收接受**（S02 与 S03 曾是**一次界面操作一并验收**；S03 另有返工轮次 r02——用户用真实文件在真实模式遇到技术失败后返工，经 r02 自动化审阅与用户复验后于 2026-10-06 接受，残留的真实文件记录 `4` 失败族转 S04／后续）；持久化、批次/恢复、导出与下载、worker 属后续阶段，尚未开发。

| 文档 | 当前内容 |
| --- | --- |
| [分阶段实施与审阅](implementation/README.md) | 9 阶段、43 小步骤、讨论/批准流程、自动化与手动审阅要求 |
| [S01 前置检查与接手](implementation/handoffs/S01-前置检查与接手说明.md) | 2026-10-06 检查结论、工具链差异和接手任务 |
| [开发环境与依赖](implementation/handoffs/S01-开发环境与依赖清单.md) | 用户 Conda 环境、固定版本安装清单及前端分工 |
| [依赖版本解析](implementation/handoffs/S01-依赖版本解析记录.md) | Windows/Python 3.12 解析及元信息证据；不是安装/运行测试 |
| [目录清单](PROJECT_STRUCTURE.md) | 实际目录与文件、占位和延期项 |
| [需求](REQUIREMENTS.md) | 业务与验收规划入口 |
| [架构](ARCHITECTURE.md) | 当前骨架与拟定运行架构 |
| [测试](TESTING.md) | 检查与阶段审阅入口、实际验证状态 |
| [开发约定](CONVENTIONS.md) | 目录、配置、实现与批准约定 |
| [交接](HANDOFF.md) | 完成范围、待审阅及后续条件 |

[完整规划](../plan/README.md) · [项目 README](../README.md) · [开发规则](../CLAUDE.md)

implementation/stages 保存阶段拆分，**小步骤讨论定稿后直接写入阶段文档**（[阶段执行规则](../.claude/rules/development-workflow.md) §2，2026-10-06 起不再新建 preparations 开发前方案，该目录与模板仅作历史参考）；reviews 在真实开发后记录自动化报告、手动指南和用户结果；templates 已生成三种通用模板。

**手动验证方式（用户 2026-10-06 决定）：只使用本项目的前端界面验证**，规则见 [审阅交付规则](../.claude/rules/review-and-handoff.md) §4 与 §6；界面（S03-07 骨架）已交付，界面就绪前已实现的阶段（S02）与界面就绪后的阶段（S03）**在同一次界面操作中一并验收**，界面版指南见 [S03 r02-手动操作审阅](implementation/reviews/manual/S03/r02-手动操作审阅.md)（返工复验；上一轮 [r01](implementation/reviews/manual/S03/r01-手动操作审阅.md) 已执行完毕并保留原文）。

adr 和 plans 仍是预留目录。重要决策/专项变更在有实际需求时补充，不与 implementation 重复维护。
