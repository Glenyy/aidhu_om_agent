# 开发与交付文档入口

当前已生成目录骨架、阶段实施文档与规则，待用户审阅；业务运行功能尚未开发。

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

implementation/stages 保存阶段拆分；preparations 到阶段前填写具体方案；reviews 在真实开发后记录自动化报告、手动指南和用户结果；templates 已生成三种通用模板。

adr 和 plans 仍是预留目录。重要决策/专项变更在有实际需求时补充，不与 implementation 重复维护。
