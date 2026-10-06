# Python 开发环境规则

适用范围：整个项目，无路径限制。依据为 2026-10-06 用户最新决定：“开发环境我会自己去anaconda创建一个对应的环境，告诉我需要安装哪些依赖就好”。

用户补充要求：激活 Anaconda 环境后通过 requirements txt 安装，不使用逐个列包的安装命令。

## 环境归属

Python 目标仍为 3.12，独立 Conda 环境由用户创建，并在 Anaconda 控制台激活。Python 安装入口统一为项目根目录 [requirements.txt](../../requirements.txt)，用目标环境的 python -m pip install -r 安装；不再让用户逐个列包安装。agent 提供 [说明](../../docs/implementation/handoffs/S01-开发环境与依赖清单.md)，本次不得自动创建环境、安装依赖或修改 base。

收到用户环境名/解释器位置后，验证实际 Python、pip 归属及依赖完整性，再用于已批准的实现/检查。不猜测环境名称，不把本机默认 Python 或旧项目环境当成用户指定环境。

此偏好覆盖原 S01 中由开发者创建 .venv 的默认安排。uv 可用于解析/导出锁定依赖，但不得自动通过 uv sync/uv run 另建 .venv 并绕过用户 Conda 环境。安装/检查命令明确使用用户环境的解释器。

## 依赖与执行

当前由 [requirements.in](../../requirements.in) 保存直接依赖范围，解析器生成 requirements.txt：36 个直接/传递包全部 == 固定版本，Windows x64/Python 3.12 解析及元信息检查已通过，[证据](../../docs/implementation/handoffs/S01-依赖版本解析记录.md)。这不等于用户安装或项目测试通过。

不自动升级已固定版本，不单独手改传递依赖；调整输入范围后重新解析，并验证受影响行为。pyproject.toml 仍是骨架，S01 补齐业务/测试/构建声明后与解析输入同步；用户安装入口始终是 requirements.txt，不改用另一套环境工具。

用户自行创建及本次安装环境；未来用户明确授权 agent 在其环境中执行安装时，按该授权及批准方案操作，不重复索取相同权限。

前端仍通过 Node/pnpm 管理，与 Conda 分开。Node/pnpm 运行时和具体版本在 S01-01 核对，不借 Python 环境准备自动升级全局 Node。

最新状态见 [交接](../../docs/HANDOFF.md)。环境方式已确认，代码小步骤批准及阶段验收仍单独记录。
