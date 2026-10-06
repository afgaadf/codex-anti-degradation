# anti-degradation supervisor

[![CI](https://github.com/afgaadf/codex-anti-degradation/actions/workflows/ci.yml/badge.svg)](https://github.com/afgaadf/codex-anti-degradation/actions/workflows/ci.yml)

一个**独立于生产者**的监督层。目的是让"降智"这件事**可见、可拦、可恢复**，
而不是假装它能被自省消除。

核心判断：**只要闸门是我（生产者）拉的，它就不是闸门，是我的自我评价。**
所以所有真正的控制权都不在生产侧。

---

## 一、权限怎么分离

| 能力 | 生产者(agent) | 监督层(supervisor) | 人 |
|---|---|---|---|
| 记录事件 / 运行检查 | 可 | 可 | 可 |
| 修改监督阈值 | **不可** | 仅记录变更 | **只能由人批准** |
| 自证交付完成 | **不可** | 判定权在此 | — |
| 解除阻断 | **不可** | 不自行解除 | **只能由人** |
| 修改已冻结规则 | 会被检出 | 硬阻断 | 可经批准 |

三道具体防线：

1. **规则哈希锁** —— `rules/rules.json` 冻结后标记只读并记录 SHA-256。
   内容被改动且无批准记录 → 直接 `BLOCKED`（硬阻断，不看分数）。
2. **检查权独占** —— 只有 `supervisor.py check` 产生的通过记录才算数。
   `gate` 见到"没有检查记录"就拒绝，生产者无法自证。
3. **解除权归人** —— `resume` 必须带 `--by` 和 `--reason`，写入批准文件与审计。

---

## 二、降智怎么被检测

监督层吃事件，按加权分升档。**每个信号单独就能触发对应等级**。

> 下表是**出厂默认值**。实际生效值以 `rules/rules.json` 为准 ——
> 人可以用 `rules approve` 改；也可以用 `calibrate` 按历史分布建议新值。
> （本机 2026-10-06 已被人工调高一次，见 `rules/approvals/`。）

| 信号 | 阈值键 | 说明 |
|---|---|---|
| 轮数 | `turns_*` | 会话轮数 |
| 上下文字符数 | `context_chars_*` | 由 transcript 体积上报 |
| 图片数 | `images_*` | view_image 计数 |
| 会话时长(分钟) | `elapsed_min_*` | 墙钟时间（口径 = 监督窗口：init/resume 起算，换线程不归零） |
| 上下文被压缩 | `compactions_*` | **按事件**计：PreCompact / PostCompact 各 +1（一次压缩通常是两个事件） |
| **工具报错总数** | `tool_errors_*` | 来自 PostToolUse 的 `tool_response` |
| **返工** | `rework_*` / `rework_limit` | 同一路径被反复**写入/修改**的次数（只读调查不计） |
| **漏项** | `unfinished_*` | 声明完成但拿不出通过检查 |
| **子代理并发** | `subagents_*` | SubagentStart/Stop 的并发数 |
| 未验证的完成声明 | `weights.unverified_claim` | 逐条加权 |
| 无法解析的工具失败 | `weights.unregistered_failure` | 未登记为欠账的报错 |
| 重复同一命令 | `repeat_command_limit` | ≥N 次算一次 |
| 规则被静默篡改 | `hard_block_on` | **立即 BLOCKED** |

分档阈值：`watch=3`, `degraded=8`, `blocked=14`（权重可在 `rules/rules.json` 调）。

**关键设计：分数可恢复。** 问题被真正解决（检查通过）后，对应扣分会被清除。
只会往上涨的监督器最后一定会误杀正常的长任务。

---

## 三、一旦降智就接入

两个 hook，都**由宿主执行，生产者无法跳过**：

| Hook 文件 | 事件 | 职责 |
|---|---|---|
| `hook_supervisor.ps1` | `PreToolUse` | 拦截/放行工具调用 |
| `hook_session.ps1` | `UserPromptSubmit` `PreCompact` `PostCompact` `SessionStart` `SessionEnd` | 上报上下文体积、采集压缩信号、注入介入警告 |

| 状态 | hook 行为 |
|---|---|
| NORMAL | 输出 `{}`，完全透明 |
| DEGRADED | 放行，但把介入指令注入 `permissionDecisionReason`（强制重新锚定） |
| BLOCKED | `permissionDecision = deny`，工具调用被真正拦下 |
| BLOCKED + 只读命令 | 放行（`status` / `rules verify` / `debt list`），避免把人锁死 |
| 监督层不可达 | 放行 + 记日志（不能让一个 bug 砖掉整个 Codex） |

`DEGRADED` 时注入的指令会强制要求：停止扩大范围 → 写交接单 → 读回确认 → 才允许继续。

---

## 四、安装

本机配置 `hook_config.json` 不入公开仓库；首次安装会由 `install.ps1` 生成。需要手动核对时参考 `hook_config.example.json`。


```powershell
# 先看会做什么（不改任何东西）
powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -DryRun

# 真正安装
powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1
```

安装脚本保证：
- 修改 `hooks.json` **前必定备份**（`hooks.json.bak-<时间戳>`）
- **合并而非覆盖** —— 已有的 `view_image_guard` 等 hook 原样保留
- 幂等 —— 重复执行不会写入重复条目
- 已存在的规则和账本不会被覆盖

卸载：`install.ps1 -Uninstall`（会移除 hook 条目并备份 hooks.json）

> hook 在 **Codex 重启后**才生效。

---

## 五、日常使用

```powershell
$S = "python $env:USERPROFILE\.codex\anti-degradation\supervisor.py"

# 看状态
& $S status

# 交付前必须过闸门（生产者不能自证）
& $S check --name <检查名> --deliverable <交付物名>
& $S gate  --deliverable <交付物名>

# 登记 / 清理欠账
& $S debt add --text "..." --severity critical --deliverable <名字>
& $S debt list
& $S debt clear --id 1 --evidence "..."

# 按历史样本建议阈值（只建议；--apply 写入后仍须 rules approve）
& $S calibrate
& $S calibrate --pct 95 --apply

# 生成人机交接单（降智介入时会自动产出，也可手工生成）
& $S handoff

# 人解除阻断（必须在 Codex 之外执行）
& $S resume --by 你的名字 --reason "已核对交接单"

# 强行绕过（会写永久审计，不推荐）
& $S break-glass --reason "..."
```

写一个检查：在 `checks/<名字>.json` 放

```json
{
  "name": "demo",
  "deliverable": "demo",
  "steps": [
    { "cmd": "python -m pytest -q", "expect_exit": 0 }
  ]
}
```

---

## 五·五、外部维护模式（2026-10-07）

这是给开发和维护用的**外部、限时、非常驻**闸门，不是常驻运行模式，也没有页面内开关。

```powershell
# 在 Codex 之外的普通终端执行
& "$env:USERPROFILE\.codex\anti-degradation\维护模式.cmd" on "临时维护原因"  # 默认 60 分钟
& "$env:USERPROFILE\.codex\anti-degradation\维护模式.cmd" off "维护完成"
& "$env:USERPROFILE\.codex\anti-degradation\维护模式.cmd" status
```

规则：

- `on` / `off` 只能在 Codex 之外执行；检测到 `CODEX_SESSION_ID` /
  `CODEX_THREAD_ID` / `CODEX_SHELL` 会拒绝，防止生产者自开闸。
- 默认 60 分钟，允许 1–480 分钟；写入 `state/maintenance.json`，到期后
  `observe` 自动恢复原有 BLOCKED 决策，不需要任何后台进程。
- 维护期间保留底层分数和等级；只是让 hook 临时放行，并在会话提示中注明
  “底层评分仍为 BLOCKED / DEGRADED”。
- `on` / `off` 都写审计；`resume` 也会顺手清除维护状态。
- 已验证：维护启停拒绝 Codex 内调用、外部可开启、hook 放行、窗口过期自动恢复拒绝。

## 六、诚实的能力边界

这套东西**不能**做到"完全不会降智"。任何声称能做到的方案都是骗人的。它做到的是：

- ✅ 降智**可见** —— 分数、原因、审计日志都落地
- ✅ 降智**可拦** —— BLOCKED 时工具调用真的被拒
- ✅ 降智**可恢复** —— 换线程 + resume，代价有限
- ✅ 规则**不可被静默修改** —— 改了必被检出并硬阻断

做不到的，必须说清楚：

- ❌ **无法用密码学阻止生产者绕过。** 这是同一台机器上的同一份文件系统。
  真正的约束是"绕过会留下响亮且永久的记录"——把静默失败变成可见失败。
- ❌ **判断类质量兜不住。** "好不好看"没有确定性判据，脚本说不了话，
  只能靠人在低成本阶段介入。
- ✅ **上下文体积已能自动上报。** `UserPromptSubmit` 带 `transcript_path`，
  hook 直接读文件体积上报，无需人工干预。
- ❌ **hook 自身有误判可能。** 所以设了只读白名单和"不可达即放行"两道保险，
  避免监督层故障反而砖掉正常工作。
- ⚠️ **工具报错检测依赖 `tool_response` 的字段形状。** `hook_post.ps1` 兼容
  `is_error` / `success=false` / `exit_code` / `error` / `status` 几种常见形状；
  若某个工具的回包不含这些字段，则记为"未报错"（宁可不报，也不制造假信号）。
- ⚠️ **返工是启发式的，且只认写入。** hook 先判命令是否"会改盘"（写盘 cmdlet /
  `>` 重定向 / `sed -i` / `write_text` 等），只对写命令提取目标路径；
  只读命令（`Get-Content` / `Get-ChildItem` / `Select-String` / `git status` …）
  不参与 `touches`。宁可漏判，也不把正常调查误判成返工（见六·六）。

---

## 六·五、盲区可见化 + stdin 编码根因（2026-10-06 定案）

**现象**：hook 周期性收到"非法 JSON"的 stdin，报错位置总落在**字符串中部**
（col 707/833/1071/1383/3149/3240…），**短到 1.6KB 也复现**，与体积无关。
旧代码只写一行 "input parse failed" 就放行 —— "监督层没看见这条事件"
这件事本身完全静默。

**根因（定案：不是"宿主序列化缺陷"）**：

1. **host → hook**：宿主写来的是**合法 UTF-8 JSON**；但 hook 用
   `[Console]::In.ReadToEnd()` 读，本机 `[Console]::InputEncoding = gb2312(cp936)`。
   UTF-8 中文被按 GBK 错解，多字节边界处会**吃掉/插入 ASCII 结构字符**
   （`"` `\`），把合法 JSON 顶成非法 —— 报错位置随中文落点漂移，所以与体积无关。
   *证据*：把 `logs/stdin-fail-*` 按 `cp936 → utf-8` 回转，得到的是**完全合法的 JSON**。
   *附带发现*：`String.StartsWith` 是**文化敏感**比较，U+FEFF 属可忽略字符，
   用它判 BOM 会对任意字符串返回 true —— 修 BOM 必须用 `$raw[0] -eq [char]0xFEFF`。
2. **hook → supervisor**：hook 用 `ProcessStartInfo.StandardInput.Write()` 写子进程，
   默认走控制台代码页（cp936）；supervisor 按 UTF-8 读 → 中文乱码，
   observe 记 `observe_parse_error`，事件被静默丢弃。
3. **崩溃 → 999 硬拒**：错编码会留下孤立代理项（lone surrogate），写
   `events.jsonl` 时 `json.dumps(ensure_ascii=False)` + UTF-8 落盘抛
   `UnicodeEncodeError`，被 `main()` 兜成 `score=999 / deny` ——
   **一次工具调用被硬拒**（2026-10-06 14:43:29 实际发生）。

（原欠账 #2 猜的"rules 读不到"没有发生：`audit.jsonl` 里**没有** `rules_missing`
记录，唯一那条 `supervisor_error` 就是上面这个代理项崩溃。）

**修复**：
- 三个 hook 一律**读原始字节**再显式按 UTF-8 解码（不再用 `[Console]::In`）。
- hook 调 supervisor 时**显式把 UTF-8 字节写进子进程 stdin**
  （`Write-Utf8Stdin` 走 BaseStream；PS5.1 根本没有 `StandardInputEncoding` 属性）。
- supervisor 的 `_force_utf8()` 现在把 **stdin** 也钉死 UTF-8。
- `_json_text()`：落盘前替换掉不可 UTF-8 编码的字符 —— **日志不再因字符串内容而失败**。
- 解析失败落盘的是**原始字节**（不是二次解码后的字符串）。取证所见即宿主所写。

**规则读瞬时失败**：`rules approve` / `freeze_rules` 重写 `rules.sha256` 一律
**原子写**（临时文件 + `os.replace`）；`load_rules` 走 `read_json_retry` 退避重试，
`rules_integrity` 对 hash 中间态短暂重读 —— 并发批准不会再被误判成
"规则被静默篡改"而硬阻断。

**hook 启动器**：`hooks.json` 不再钉死某条 pwsh 路径，统一走
`cmd.exe /d /c run_hook.cmd <supervisor|session|post>`；启动器按顺序探测 pwsh
并回退 `powershell.exe`，避免 WindowsApps/MSIX 别名失效或版本化临时目录被清理
时 hook 静默不跑。（Codex 用 `Command::new(argv[0])` 启动，无 shell、引号会被
当路径一部分，所以用无空格的绝对路径 `cmd.exe`。）

**只读白名单越权洞（同日修）**：原来是 `-match` **整串子串**匹配，于是
`rm -rf / ; python supervisor.py status` 会被整体放行 —— 生产者可借此绕开
BLOCKED 硬阻断。现在要求整条命令**就是**一条只读调用（最多允许一个前置 `cd`）。

**现在**：
- 三个 hook 解析失败时，把原始 stdin 前 4KB **无 BOM 落盘**到
  `logs/stdin-fail-<source>-<时间戳>.txt`，并上报 `parse_failure` 事件。
- supervisor 增加 `parse_failures` 计数，`status` 直接可见，并计入
  `reasons`（`parse_failures_watch`/`_degraded`）。
- **该信号永不单独 BLOCK**（`parse_failures_blocked = null`）：它证明的是
  "监督层盲了"，拦生产者并不能恢复可观测性，只会误杀正常长任务。

**自检**：`python supervisor.py doctor` 检查规则完整性、hooks.json 接线、
解释器路径是否存在、脚本是否在位 —— 解释器一失效 hook 是静默不执行的，
doctor 把它变成可见失败。

**工具名兼容**：本机宿主上报的工具名是 `Bash`（不是 `exec_command`）。
修复前 `command`/`rework` 信号与**只读白名单**对它全部失效（BLOCKED 时
连 `status` 都进不来）。现已同时接受 `exec_command`/`Bash`/`bash`/`shell`/`sh`。

## 六·六、返工口径与监督窗口（2026-10-06 定案）

**本轮修的正是监督层自己误杀自己。**

1. **返工把"调查"当"返工"（误判，已修）。**
   - 症状：本会话两次 `DEGRADED`、一次 `BLOCKED`，`reasons: rework=4 >= 4 (+9)`。
   - 根因：`hook_supervisor.ps1` 对**任何**命令都正则取首个疑似路径并累加 `touches`。
     `events.jsonl` 里 `.codex\anti-degradation` 被计 72 次、某 work 目录 35 次、
     `supervisor.py` 34 次 —— 全是 `Get-ChildItem`/`Get-Content`/`Select-String`
     这类只读调查反复碰同一路径，与"返工"无关。旧正则还会把 `/c`、`/d` 开关当路径。
   - 修法：新增 `Test-MutatingCommand`（只在会改盘时才算）+ `Get-TargetPath`。
     `Get-TargetPath` 按优先级取**真正的写目标**：写重定向 > 显式路径参数
     (`-FilePath`/`-Path`/`-LiteralPath`/`-Destination`/`-OutFile`) > 写 cmdlet 位置参数；
     兜底才用"首个路径令牌"，且**跳过 `cd`/`Set-Location` 的目标**。
     **只读调查不进入 `touches`。**
   - 复查补丁 1（同日）：`cd <dir>; ... > <dir>\log` 原会把 `<dir>` 当写目标 →
     反复跑测试/看目录就虚增返工（`.codex\anti-degradation` 涨到 17）。现改为取重定向目标；
     并排除 `(>=N)` 被误判成写重定向的假信号。
   - 复查补丁 2（同日）：`$x = @'…'@` 这类内联 here-string 的**正文**会被当成真实命令
     （正文里的 `Set-Content` 等关键词让 harness 给无关文件记账）。新增 `Get-CommandLine`，
     先剥掉 here-string / heredoc 正文再分类与取目标。
   - 复查补丁 3（同日）：`Get-CommandLine` 必须按 **PS 规则**匹配 —— 开引号紧跟换行、
     闭引号**在行首**。否则正文里出现 `@…@` 片段会让非贪婪匹配提前收尾，把正文尾部
     当命令行（同一 leak 的第二层）。
   - 复查补丁 4（同日，第四轮）：把残留的只读误判压掉 ——
     写重定向必须指向**像文件的目标**（含路径分隔符或扩展名），否则 `print(1 > 0)`、
     `Select-String -Pattern ">"` 会被当成写盘；去掉歧义的两字母别名（`sc`/`ac`/`ni`…，
     否则 `sc query` 中招）；`.write(` 排除 `stdout`/`stderr`/`stdin`（写标准流不是写文件）；
     `git stash list/show` 这类只读子命令不再算写。
   - 监督层配套：`command` 事件带 `rework_kind`，只有非 `read` 才累加；
     显式 `{"kind":"rework"}` 事件不受影响（仍按写入计）。
   - 取舍：故意偏保守 —— 漏判（真实返工少算）远好于误判（正常调查被顶成 BLOCKED）。

2. **`session_start` 只重置时钟、不清计数（口径打架，已修）。**
   - 症状：换线程后 `started_epoch`/`started_ts` 归零，但 `turns`/`touches`/`compactions`
     继续累积 → `elapsed_min` 与各计数**窗口不一致**。
   - 关键：**计数跨会话留存是设计意图**（恢复模型 = 换线程 + resume；否则生产侧
     只要新开线程就能清零全部计数，等于留逃生门）。所以正确的修法是让**时长也跨会话**，
     而不是让计数归零。
   - 修法：`session_start` **不再**触碰 `started_epoch`/`started_ts`（窗口锚点只在
     `new_session()` 里重置）；改为登记线程接入（`sessions_seen` / `last_session_start_ts`）。
     `elapsed_min` 因此与计数同口径。
   - 附带：`started_ts` 与 `started_epoch` 仍然成对赋值，不再出现"一个动一个不动"。

> ⚠️ **计量单位（2026-10-06 第七轮查证）**：官方仓库定义 `PreCompactRequest` / `PostCompactRequest`
> 为两个独立事件（`codex-rs/hooks/src/events/compact.rs`），本机 hooks.json 两者都接到 session hook，
> 所以 `compactions` 计的是**事件数**。若一次压缩同时触发 pre+post，则数字约为"压缩次数 × 2"。
> 阈值是人工批准过的，**故本轮只澄清口径、不改计数**；是否改成"只计一次压缩"属人的决策。

**验证**：`tests/test_supervisor.py` 第 15/18 组（返工口径 / 窗口锚点）、
`tests/test_hook.py` 第 14 组（返工只认写入，含 cd 归因、`(>=N)`、here-string、
比较/引号/stdout/stash/sc 等边界）覆盖上述两条；`run_tests.ps1` 三套全绿。

## 六·七、view_image 守卫的静默失效（2026-10-06 第六轮）

**症状**：`view_image_guard.ps1` 对**中文名**的大图完全不生效 —— 传一张 4.2MB 的
`东方明珠效果图.png` 时守卫输出 `{}`、日志记 `no usable path in tool_input`，原图照进上下文（413 风险）。
ASCII 名则正常降采样。

**根因**：它用 `[Console]::In.ReadToEnd()` 读 stdin。本机 `[Console]::InputEncoding = gb2312(cp936)`，
宿主写来的是 UTF-8 → 中文路径被按 GBK 错解 → `Test-Path` 失败 → 静默放行。
**这正是六·五记录过的同一个根因** —— 三个 anti-degradation hook 都修了，唯独这个
（在 `tools/` 下、不在本目录）漏了。同理 `Emit` 用 `[Console]::Out.Write` 写 stdout，
中文路径会被转成 GBK，`updatedInput.path` 一乱，view_image 就指向不存在的文件。

**修复**：
- 读：`OpenStandardInput()` 取**原始字节**再显式 UTF-8 解码（去 BOM）。
- 写：`OpenStandardOutput()` 直接写 **UTF-8 字节**（不再走 `[Console]::Out`）。
- 文件加 BOM（现在含中文注释，且 hooks.json 用 `powershell.exe`(5.1) 直接执行）。

**doctor 补检**：该守卫不在 ROOT 下，doctor 原来只验 supervisor/post/session 三条接线，
**完全没检查它** —— 而它失效正是"静默放行大图"，属于 doctor 要消灭的失败模式。
现增加 `wired:view_image_guard`、`file:view_image_guard.ps1`、`file:imgpreview.ps1`
（doctor 现 **14 项**全绿）。

**验证**：中文名大图 4261.5KB → 重写为 126.6KB 预览，且预览文件确实存在；ASCII 名同样正常。
回归见 `tests/test_hook.py` 第 15 组（缺件自动 SKIP）。

## 六·八、hook 信任模型（2026-10-06 第九轮查证）

本机 `config.toml` 里有一节 `[hooks.state.'…hooks.json:<event>:<group>:<handler>']`，带
`trusted_hash = "sha256:…"`。按官方源码（`codex-rs/hooks/src/engine/discovery.rs` /
`config/src/hook_config.rs`）：

- **信任哈希算的是 "hooks.json 里的 handler 定义"**（`hook_hash(event_name, matcher, group, config)`），
  **不是脚本文件内容**；
- key = `<hooks.json 绝对路径>:<snake_event>:<handler组下标>:<组内下标>`；
- `matcher` 语义（`engine/matcher.rs`）：空或 `*` = 全匹配；**只含字母数字下划线和 `|` = 精确匹配**；
  其余按正则。

**对本项目的两个结论**：
1. **改 `hook_*.ps1` / `supervisor.py` 不会让 hook 失信** —— 命令串没变，定义哈希不变。
   （实测：本机 10 个 handler ↔ config.toml 10 条 `hooks.state`，**0 缺失 0 多余**；且 hook 日志持续新增。）
2. ⚠️ **改动 `hooks.json` 的条目（增删/换序/改命令）会改变 key 或哈希 → 该 handler 变为未信任**，
   需要重新在 Codex 里信任。以后维护 hooks.json 时必须知道这一点。
   另：`"view_image"` 这类 matcher 是**精确匹配**（不是正则），只对该工具名生效。

## 六·九、hook **标准输出**的 cp936 乱码（2026-10-06 第十轮查证，真 bug）

**症状**：监督层注入给模型的文字是乱码 —— transcript 里 `role:"developer"` 的消息长这样：
`[SUPERVISOR] ���Ƿ��յȼ�=DEGRADED (score=12)��`。
（原文应为 `[SUPERVISOR] 降智风险等级=DEGRADED …`。用户每一轮看到的也正是这个。）

**取证**：
1. transcript 里该消息含大量 **U+FFFD** 且残留 **GBK 高位字节** → 说明"发出去的是 GBK，被按 UTF-8 读"；
2. 复现：`chcp 936` + `powershell.exe`(5.1) 跑 `hook_session.ps1` → stdout 按 UTF-8 解码**失败**
   （`invalid start byte 0xbd`，即 `降` 的 GBK 首字节），按 GBK 解码正常；
3. 原因：`Emit` 用 `[Console]::Out.Write`，它会跟随**控制台输出码页**。
   pwsh 7 → UTF-8（正常）；**PS 5.1 + cp936 控制台 → GBK → 宿主按 UTF-8 读 → 乱码**。
   这与六·五 的 stdin 根因**同源**，只是方向相反（`view_image_guard` 的输出侧第六轮已修）。

**修复**：`hook_supervisor.ps1` / `hook_session.ps1` / `hook_post.ps1` 的 `Emit`
改为**直接写 UTF-8 原始字节**（`OpenStandardOutput()` + `UTF8.GetBytes`），不再依赖控制台码页。

**验证**：同一 `chcp 936` 复现脚本，修复后 stdout 是**合法 UTF-8**、`降智` 完好；
回归见 `tests/test_hook.py` 第 16 组（该组会强制 cp936，正是能抓住这个 bug 的用例）。

## 六·十、hook 超时与注入体积的上限（2026-10-06 第十一轮查证）

按官方源码（`codex-rs/hooks/src/engine/discovery.rs`、`events/session_end.rs`）：

```rust
// 超时解析
match event_name {
    SessionEnd | Interrupt => {                 // 上限 3 秒
        if timeout_sec > 3 { warnings.push("clamping ... to 3s ..."); }
        timeout_sec.unwrap_or(1).clamp(1, 3)
    }
    _ => timeout_sec.unwrap_or(600).max(1),     // 其它事件：默认 600s，无上限
}
```

- **只有 `SessionEnd` / `Interrupt` 有硬上限 3 秒**（默认 1 秒）；其余事件默认 600 秒、无上限。
- `additionalContextLimit` 默认 **2500 tokens**（`output_spill.rs` 的 `DEFAULT_HOOK_OUTPUT_TOKEN_LIMIT`）；
  超过就 spill 到磁盘只留预览。本机注入约 150 tokens，**不会 spill**。

⚠️ **本机已知配置瑕疵（未改，故意）**：`hooks.json` 给 `SessionEnd` 也配了 `timeout: 30`，
官方会 **clamp 到 3 秒并产生 warning**。功能上无害 —— supervisor 对 `session_end` 本来就是空操作。
**不擅自改**的理由：`hooks.state` 的信任哈希覆盖 handler 定义（见六·八），
**改 hooks.json 会让该 handler 变为未信任，必须由人在 Codex 里重新信任**；这个动作我在 Codex 之外做不了。
若要消除告警，请由人二选一：
- 把该条 `"timeout": 30` 改成 `3`（或删掉该键，走默认 1 秒），**然后重新信任该 hook**；或
- 保持现状（仅一条无害告警）。

## 六·十一、`SessionStart.source` 的透传（2026-10-06 第十二轮查证）

官方 `session-start.command.input.schema.json` 把 `source` 列为 **required**，
取值是枚举 `startup | resume | clear | compact | fork`
（`events/session_start.rs` 的 `SessionStartSource::as_str`）。

**原来的缺陷（信息丢失）**：本机 `hook_session.ps1` 只发 `{"kind":"session_start"}`，
**把 required 的 `source` 丢掉了** —— 监督层因此无法区分"新开线程 / 恢复 / 压缩后 / 清空 / fork"。

**修复（只记录，不改计分）**：
- `hook_session.ps1` 透传 `source`；
- `supervisor.py` 记录 `last_session_source`（`new_session`/`load_session` 有缺省，`status` 可见）；
- 这一步**不动 `hooks.json`**（不动 handler 定义 → 不影响信任，见六·八）。

**验证**：`tests/test_session_hook.py` 第 1 组（source 透传并记录）、
`tests/test_supervisor.py` 第 18 组（记录 `last_session_source`）均通过。

## 六·十二、`tool_errors` 在本机是"结构性不可观测"（2026-10-06 第十三轮查证）

**发现**：官方测试套件把 `tool_response` 断言为**字符串**
（`codex-rs/core/tests/suite/hooks.rs`：`assert_eq!(hook_inputs[0]["tool_response"], Value::String("post-tool-output"))`）。
本机实测**完全一致**：临时探针抓到一次真实 PostToolUse 载荷，`tool_response` 类型是
`System.String`（就是工具的文本输出），**整个载荷里没有任何 `exit_code`/`is_error`/`status` 字段**。

**后果**：`hook_post.ps1` 原来按**对象属性**判失败（`is_error`/`success`/`exit_code`/…），
对本机形状**一个都匹配不到** → `tool_errors` 恒为 0 —— 这是"**看不见**"，不是"没有错误"。
（实测：`cmd /c exit 7` 之后 post.log 无新增、`tool_errors` 仍为 0。）

**处置**（按"宁可不报、也不假报"的原则**不猜文本**，但必须让盲区可见）：
- `hook_post.ps1`：`tool_response` 不是对象时，记录一次 `tool_response_shape` 事件
  （每 `(session,形状)` 只报一次，避免刷屏）；
- `supervisor.py`：记录并在 `status.counters` 暴露
  `tool_response_shape`（本机为 `string`）与 `tool_response_judgeable`（本机为 `false`）；
- 对象形状仍按原逻辑判定（回归测试同时覆盖两种形状）。

**结论**：在本机上 `tool_errors` / `unregistered_failures` 这两条信号**不可用**；
`status` 现在明确显示 `tool_response_judgeable=false`，不会再把"看不见"读成"零错误"。
若将来宿主改为发送结构化响应，本逻辑会自动恢复判定（对象分支仍在）。

## 六·十三、Stop 事件的接线（2026-10-06 第十四轮）

第十二轮把 `SessionStart.source` 透传后，这里再补一个**宿主侧信号**：官方 `Stop`。

- 官方输入（`stop.command.input.schema.json` 的 `required`）：`session_id` / `turn_id` / `stop_hook_active` / `last_assistant_message` / `transcript_path` / …；`stop_hook_active` 是 bool，`last_assistant_message` 是可选字符串（`events/stop.rs`）。
- 本机改法：`hooks.json` 新增一个 `Stop` handler → `run_hook.cmd stop` → `hook_session.ps1` 的 `Stop` 分支。只取**元数据**（是否结束、是否有末条消息、末条消息长度），**不转发整条消息**（末条消息可能很大）。
- `supervisor.py`：记 `last_turn_end_ts` / `last_stop_hook_active` / `last_stop_had_message` / `last_stop_msg_len`，并在 `status.counters` 暴露。**只记录、不改计分。**
- 意义：把“本轮结束 / 有末条消息”从**生产者自报**升级为**宿主侧证据**；后续若判“声明完成却无证据”，可据此而不只信 `completion_claim`。
- ⚠️ 新增 handler 会带来一条**未信任**登记（key `…\hooks.json:stop:0:0`），必须在 **Codex 之外**完成信任后才会执行。已有 10 个 handler 的信任**不受影响**（信任按 handler 定义哈希，新增条目不改动旧条目）。

## 七、目录结构

```
anti-degradation/
├── supervisor.py            # 监督引擎 + CLI
├── hook_supervisor.ps1      # PreToolUse 闸门（宿主执行）
├── hook_post.ps1            # PostToolUse 工具报错采集（宿主执行）
├── hook_config.json         # hook 找到 supervisor 的路径配置
├── run_hook.cmd             # hooks.json 统一启动器（探测 pwsh / 回退 PS5.1）
├── install.ps1              # 安装 / 卸载
├── rules/
│   ├── rules.json           # 阈值（冻结后只读）
│   ├── rules.sha256         # 哈希锁
│   └── approvals/           # 人的批准记录
├── checks/                  # 每个交付物的检查定义
├── ledger/debt.md           # 欠账登记
├── state/                   # 会话状态 / 事件 / 审计 / 介入指令
├── logs/                    # hook 日志
└── tests/
    ├── test_supervisor.py   # 监督层验证（闸门/规则锁/恢复/计数口径）
    ├── test_hook.py         # hook 端到端验证（PreToolUse/PostToolUse）
    └── test_session_hook.py # 会话级 hook 验证（上下文/压缩/注入）
```

跑测试：

```powershell
python tests\test_supervisor.py
python tests\test_hook.py
```
---

## 八、现成工具调研结论（2026-10-06）

装之前查过 npm 与 Codex 本机安装，结论如下。

### 已有的、可复用的

| 来源 | 结论 |
|---|---|
| **Codex 原生 hook 系统** | 最该复用的成熟工具。本机 codex-cli 0.160.0 支持的事件已从二进制确认：`PreToolUse` `PermissionRequest` `PostToolUse` `PreCompact` `PostCompact` `SessionStart` `SessionEnd` `UserPromptSubmit` `SubagentStart` `SubagentStop` `Stop` `Interrupt`。输入字段：`session_id` `transcript_path` `cwd` `hook_event_name` `reason` `turn_id` `model` `permission_mode` `stop_hook_active` `last_assistant_message` `prompt` `tool_input` |
| `@hasna/hooks` (Apache-2.0, v0.12.4, 52K/月) | 成熟的 hook 安装管理库（Claude+Codex）。已吸收其做法：写 hooks.json 前做 drift 检测，漂移则拒绝写入。它本身不做降智监控。 |
| `@deepseek-ai/dsh-hook-protocol` (BSD-3, 0.0.1-rc.1, 1.6M/月) | Codex/Claude hook 线协议实现。版本为预发布，未引入关键路径。 |

### 不采用的理由

- **没有现成的降智监督器。** 哈希锁规则、检查权独占、分数可恢复、硬阻断——
  这套组合在 npm 上没有对应实现，属于定制。
- **不把预发布三方包放进强制拦截路径。** `supervisor.py` 保持零依赖：
  少一个依赖，就少一个监督层自己挂掉的失败模式。
- `@inerrata-corporation/errata` 为 UNLICENSED，直接排除。

### 环境前提

hook 用 **pwsh (PowerShell 7)** 执行，不用 `powershell.exe`。
原因：Windows PowerShell 5.1 会把无 BOM 的 UTF-8 当 ANSI 读，中文直接崩坏；
pwsh 无此问题。安装脚本按 Program Files 到 WindowsApps 的稳定路径顺序选择，
避免绑定到含版本哈希的临时目录。
