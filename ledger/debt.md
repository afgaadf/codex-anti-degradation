# 欠账登记

结项条件：open 项清零。

| ID | 欠账内容 | 严重度 | 交付物 | 状态 |
|---|---|---|---|---|
| 1 | 【P1 未结】宿主把 hook stdin 序列化成非法 JSON：18 份原始 payload 已落盘 logs/stdin-fail-*.txt，报错位置在字符串中部（col 707/833/1071/1384/3149/3240），短到 1.6KB 也复现；已排除：体积截断、纯引号/反斜杠、多行 here-string、编码。触发条件待定，需新线程专门排查。 | critical | anti-degradation | closed |
| 2 | 【P2 未结】observe 曾瞬时返回 score=999（rules 读不到/完整性校验瞬时失败），导致单次调用被硬拒；随后自愈。需查 audit.jsonl 的 rules_missing 记录并加抗瞬时失败的读重试。 | normal | anti-degradation | closed |
| 3 | 【文档】README 已补 parse_failures / stdin-fail 落盘 / doctor / Bash 工具名修复；剩余：把 run_hook.cmd 作为 hooks.json 启动器的切换（需重启 Codex 才能验证）登记为待办。 | normal | anti-degradation | closed |
