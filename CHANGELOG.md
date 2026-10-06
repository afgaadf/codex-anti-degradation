# 变更日志

本文件记录 anti-degradation 监督内核的重要变更。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [0.1.0] - 2026-10-07

### Added
- 初始纳入 git 版本控制。
- 外部、限时、非常驻的维护模式：`maintenance on/off/status`。
- 全中文外部维护面板：`维护模式面板.ps1` 与桌面快捷方式。
- Hook 放行、过期自动恢复、Codex 内禁止启停的维护模式回归测试。

### Security
- 维护模式启停要求外部环境 `--by` / `--reason`，检测到 Codex 环境变量时拒绝。
- 维护期间保留底层分数与 BLOCKED/DEGRADED 等级，启停写审计。