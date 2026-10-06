# 变更日志

本文件记录 anti-degradation 监督内核的重要变更。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [0.2.0] - 2026-10-07

### Added
- **紧急恢复 `supervisor.py rescue`** —— "搞砸了也收得了场"的那条通道：
  - **不读 rules.json**（规则坏了也能跑）、**不检查是否在 Codex 内**（随时可用）、
    **不要求 --by/--reason**（卡住的人不该再被参数难住）、**幂等**。
  - 做四件事：归档修不好的 state 文件 → 会话重置 NORMAL → 清阻断/指令 → 关维护模式。
  - 只动运行状态，**不碰规则、不碰信任、不碰 hook 接线**；全程写审计（`actor=rescue`）。
- 桌面一键入口 **`管家-紧急恢复.lnk`**（指向同目录 `紧急恢复.cmd`）。
- 维护面板新增 **「紧急恢复（卡住了点这里）」** 按钮。
- `status` 在 DEGRADED/BLOCKED 时打印自救指引；JSON 里带 `rescue_command` 字段。
- `doctor` 新增 4 项：`maintenance_state_readable` / `rescue_command_available` /
  `desktop_rescue_shortcut` / `state_files_parseable`。
- 新测试 `tests/test_rescue.py`（9 项）：正常态幂等、**session/intervention/maintenance
  三类文件损坏后能救回**、损坏文件被归档、BLOCKED 后不再拒绝、Codex 环境内 rescue 可用
  而维护模式 on 仍被拒、写审计。

### Notes
- 维护模式本身的端到端行为（外部启停 / BLOCKED 下放行 / 到期自动恢复拒绝）
  已由 `tests/test_supervisor.py` 覆盖并通过，本轮未改动其语义。

## [0.1.1] - 2026-10-07

### Changed
- 维护模式和面板改用 `%~dp0` / `$PSScriptRoot` / `%LOCALAPPDATA%`，不再写死本机用户名和 Python 路径。
- `hook_config.json` 改为只保留本机配置，不进入公开仓库；新增 `hook_config.example.json`。
- 增加 GitHub Actions Windows CI。

### Fixed
- `test_session_hook.py` 的 PowerShell 查找改为跨机器可移植。
## [0.1.0] - 2026-10-07

### Added
- 初始纳入 git 版本控制。
- 外部、限时、非常驻的维护模式：`maintenance on/off/status`。
- 全中文外部维护面板：`维护模式面板.ps1` 与桌面快捷方式。
- Hook 放行、过期自动恢复、Codex 内禁止启停的维护模式回归测试。

### Security
- 维护模式启停要求外部环境 `--by` / `--reason`，检测到 Codex 环境变量时拒绝。
- 维护期间保留底层分数与 BLOCKED/DEGRADED 等级，启停写审计。