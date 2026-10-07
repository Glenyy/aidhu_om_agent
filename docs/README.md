# 开发与交付文档入口

S01—S07 已实现并通过自动化审阅，**七个阶段均已获用户验收接受**（S02 与 S03 曾是**一次界面操作一并验收**；S03 另有返工轮次 r02——用户用真实文件在真实模式遇到技术失败后返工，经 r02 自动化审阅与用户复验后于 2026-10-06 接受，残留的真实文件记录 `4` 失败族转后续；S04 于 2026-10-07 经用户界面验证接受）。**S05（导出与命令行闭环）已实现完毕、自动化审阅通过，并于 2026-10-07 经用户界面验证接受（阶段已接受）**（[用户审阅结果](implementation/reviews/manual/S05/r01-用户审阅结果.md)）；**S06（API 与前端完整闭环）的小步骤含界面验证面已于 2026-10-07 讨论定稿并直接写入其阶段文档（v1.3 → v2.0），经用户原话「确认无误，批准开始」批准；S06-01—S06-06 已全部实现，[r01-自动化审阅](implementation/reviews/automated/S06/r01-自动化审阅.md) 通过（637 passed / 2 skipped、零真实调用），同次交付 [界面版手动指南](implementation/reviews/manual/S06/r01-手动操作审阅.md)，阶段文档升至 v2.1；用户 2026-10-07 以原话「阶段6手动审阅通过，我们进行阶段7的讨论」接受，S06 现为「阶段已接受」（阶段文档 v2.2），结果见 [用户审阅结果](implementation/reviews/manual/S06/r01-用户审阅结果.md)**；**S07（人工基准与规则校准）的小步骤（含界面验证面）已于 2026-10-07 讨论定稿（阶段文档 v1.0 → v2.0），经用户原话「批准」批准；S07-01—S07-03 已全部实现，[r01-自动化审阅](implementation/reviews/automated/S07/r01-自动化审阅.md) 通过（743 passed / 2 skipped、零真实调用），同次交付 [界面版手动指南](implementation/reviews/manual/S07/r01-手动操作审阅.md)，用户 2026-10-07 按该指南操作后以原话「手动验证通过」接受，阶段文档升至 v2.3，S07 现为「阶段已接受」，结果与记录边界见 [用户审阅结果](implementation/reviews/manual/S07/r01-用户审阅结果.md)（A 组有本机库内佐证，B 组页面读数／C 组／D 组与指南 §3.1 不写库、无独立痕迹、未记为已通过）；S07-04（校准并冻结规则版本）未实现**——其真实模型调用预算不在本次批准范围内，**保留集全程一次不跑、本轮不产出任何准确率结论**。**S08—S09 的前置已满足（S08 需 S07 已接受），但两者小步骤均未讨论、未批准**。最新进度见 [交接](HANDOFF.md)。

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

**手动验证方式（用户 2026-10-06 决定）：只使用本项目的前端界面验证**，规则见 [审阅交付规则](../.claude/rules/review-and-handoff.md) §4 与 §6；界面（S03-07 骨架）已交付，界面就绪前已实现的阶段（S02）与界面就绪后的阶段（S03）**在同一次界面操作中一并验收**，界面版指南见 [S03 r02-手动操作审阅](implementation/reviews/manual/S03/r02-手动操作审阅.md)（返工复验；上一轮 [r01](implementation/reviews/manual/S03/r01-手动操作审阅.md) 已执行完毕并保留原文）；后续界面增量分别为 [S04 r01](implementation/reviews/manual/S04/r01-手动操作审阅.md)（最小批次面，已执行并接受）、[S05 r01](implementation/reviews/manual/S05/r01-手动操作审阅.md)（最小导出面，**已由用户执行并接受**，2026-10-07）、[S06 r01](implementation/reviews/manual/S06/r01-手动操作审阅.md)（记录列表与证据详情面，**已由用户执行并接受**，2026-10-07）与 [S07 r01](implementation/reviews/manual/S07/r01-手动操作审阅.md)（**只读评估页**，**已由用户执行并接受**，2026-10-07；默认全程模拟模式、零费用）。

adr 和 plans 仍是预留目录。重要决策/专项变更在有实际需求时补充，不与 implementation 重复维护。
