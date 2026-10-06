# -*- coding: utf-8 -*-
"""对监督层的客观验证 —— 验证"闸门真的会拦"，而不是"代码看起来对"。

每个场景用独立临时副本，互不污染。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

SRC = Path(__file__).resolve().parent.parent
PY = sys.executable
FAILURES = []


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    line = f"[{mark}] {name}"
    if detail and not cond:
        line += f"  :: {detail}"
    print(line)
    if not cond:
        FAILURES.append(name)


def run(root, *args, event=None, env=None):
    r = subprocess.run([PY, str(root / "supervisor.py"), *args],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       input=event, cwd=str(root), env=env)
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def run_as_codex(root, *args, event=None):
    """Simulate the producer environment even on a clean GitHub runner."""
    env = os.environ.copy()
    env["CODEX_SESSION_ID"] = "ci-test"
    env["CODEX_THREAD_ID"] = "ci-test"
    env["CODEX_SHELL"] = "1"
    return run(root, *args, event=event, env=env)


def jload(raw):
    try:
        return json.loads(raw)
    except Exception:
        return {}


def run_external(root, *args, event=None):
    """Run as a human would from an external shell (no CODEX_* producer env)."""
    env = os.environ.copy()
    for k in list(env):
        if k.startswith("CODEX_") or k == "OPENAI_API_KEY":
            env.pop(k, None)
    r = subprocess.run([PY, str(root / "supervisor.py"), *args],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       input=event, cwd=str(root), env=env)
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def fresh(init=True):
    d = Path(tempfile.mkdtemp(prefix="sup_test_"))
    dst = d / "sup"
    shutil.copytree(SRC, dst,
                    ignore=shutil.ignore_patterns("state", "work", "tests", "ledger", "__pycache__", ".git", "backup"))
    (dst / "state").mkdir(exist_ok=True)
    (dst / "checks").mkdir(exist_ok=True)
    if init:
        run(dst, "init")
    return dst


def unlock(p):
    subprocess.run(["attrib", "-R", str(p)], capture_output=True)


def seed_thresholds(root, **kw):
    """把阈值换成测试自己的受控值并批准。

    生产阈值会被人调大/调小；测试若硬编码 60/100 轮迟早会和配置脱节。
    这里让测试自带阈值，行为只取决于机制，而不取决于当前标定。
    """
    rf = root / "rules" / "rules.json"
    unlock(rf)
    d = json.loads(rf.read_text(encoding="utf-8"))
    d["thresholds"].update(kw)
    rf.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    code, out, _ = run(root, "rules", "approve", "--by", "test", "--reason", "测试受控阈值")
    assert code == 0, f"seed_thresholds approve failed: {out}"


def turns(root, n):
    last = None
    for _ in range(n):
        last = run(root, "observe", event=json.dumps({"kind": "turn"}))
    return last


def banner(t):
    print()
    print("=" * 68)
    print(t)
    print("=" * 68)


def main():
    banner("1. 初始化 + 规则完整性")
    root = fresh()
    code, out, _ = run(root, "rules", "verify")
    check("规则完整性通过", code == 0 and jload(out).get("ok") is True, out[:120])

    banner("2. 未验证的完成声明必须被拒绝")
    code, out, _ = run(root, "gate", "--deliverable", "demo", "--claim", "已完成")
    check("gate 拒绝未验证完成", code == 2 and jload(out).get("decision") == "deny", out[:150])
    st = jload(run(root, "status", "--json")[1])
    check("未验证声明被记账", st.get("counters", {}).get("unverified_claims") == 1,
          f"got={st.get('counters',{}).get('unverified_claims')}")

    banner("3. 监督层检查通过 -> 交付闸门放行 -> 扣分清除（可恢复）")
    (root / "checks" / "demo.json").write_text(json.dumps({
        "name": "demo", "deliverable": "demo",
        "steps": [{"cmd": "echo ok", "expect_exit": 0}]}), encoding="utf-8")
    code, out, _ = run(root, "check", "--name", "demo", "--deliverable", "demo")
    check("检查通过", code == 0 and jload(out).get("passed") is True, out[:150])
    code, out, _ = run(root, "gate", "--deliverable", "demo")
    check("gate 放行已验证交付", code == 0 and jload(out).get("decision") == "allow", out[:150])
    st = jload(run(root, "status", "--json")[1])
    check("通过检查后未验证扣分被清除（能恢复，不是只涨不降）",
          st.get("counters", {}).get("unverified_claims") == 0,
          f"got={st.get('counters',{}).get('unverified_claims')}")

    banner("4. 失败检查不放行；空检查定义必须被拒绝")
    (root / "checks" / "bad.json").write_text(json.dumps({
        "name": "bad", "deliverable": "bad2",
        "steps": [{"cmd": "exit 1", "expect_exit": 0}]}), encoding="utf-8")
    code, out, _ = run(root, "check", "--name", "bad", "--deliverable", "bad2")
    check("失败检查返回非零", code == 2 and jload(out).get("passed") is False)
    code, out, _ = run(root, "gate", "--deliverable", "bad2")
    check("失败检查后 gate 仍拒绝", code == 2 and jload(out).get("decision") == "deny")
    (root / "checks" / "empty.json").write_text(json.dumps({"steps": []}), encoding="utf-8")
    code, out, _ = run(root, "check", "--name", "empty", "--deliverable", "empty")
    check("空检查定义被拒绝（禁止空过）", code == 2, out[:120])
    (root / "checks" / "missing.json").write_text("{}", encoding="utf-8")
    code, _ = run(root, "check", "--name", "nosuch", "--deliverable", "x")[0], None
    check("不存在的检查被拒绝", code == 2)

    banner("5. 降智升级：达到配置阈值 -> DEGRADED，自动介入")
    r5 = fresh()
    seed_thresholds(r5, turns_watch=10, turns_degraded=20, turns_blocked=30)
    turns(r5, 20)
    st = jload(run(r5, "status", "--json")[1])
    check("turns 计 20", st.get("counters", {}).get("turns") == 20,
          f"got={st.get('counters',{}).get('turns')}")
    check("等级 = DEGRADED", st.get("level") == "DEGRADED",
          f"level={st.get('level')} score={st.get('score')}")
    check("自动写入介入指令", bool(st.get("directive")),
          (st.get("directive") or "")[:50].replace("\n", " "))
    inter = jload((r5 / "state" / "intervention.json").read_text(encoding="utf-8"))
    check("⑤ DEGRADED 自动产出交接单", bool(inter.get("handoff_path")),
          str(inter.get("handoff_path")))

    banner("6. 继续恶化：达到 blocked 阈值 -> BLOCKED，observe 返回 deny")
    last = turns(r5, 10)
    code, out = last[0], last[1]
    j = jload(out)
    check("observe 返回 deny", code == 2 and j.get("decision") == "deny",
          f"code={code} decision={j.get('decision')}")
    check("等级 = BLOCKED", j.get("level") == "BLOCKED",
          f"level={j.get('level')} score={j.get('score')}")
    st = jload(run(r5, "status", "--json")[1])
    check("介入处于激活态", st.get("intervention_active") is True)

    banner("7. 无人在场时不能自行恢复")
    j = jload(run(r5, "observe", event=json.dumps({"kind": "turn"}))[1])
    check("阻断持续生效", j.get("decision") == "deny", f"decision={j.get('decision')}")

    banner("8. 人执行 resume 后恢复（并重置会话，重新锚定）")
    code, out, _ = run(r5, "resume", "--by", "human", "--reason", "已核对交接单")
    check("resume 成功", code == 0 and jload(out).get("ok") is True, out[:120])
    code, out, _ = run(r5, "observe", event=json.dumps({"kind": "turn"}))
    j = jload(out)
    check("resume 后恢复 allow", code == 0 and j.get("decision") == "allow",
          f"code={code} decision={j.get('decision')} level={j.get('level')}")

    banner("9. 规则被静默篡改 -> 硬阻断")
    r9 = fresh()
    rf = r9 / "rules" / "rules.json"
    unlock(rf)
    d = json.loads(rf.read_text(encoding="utf-8"))
    d["levels"]["blocked"] = 9999
    rf.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    code, out, _ = run(r9, "rules", "verify")
    check("篡改被检出", code == 2 and jload(out).get("ok") is False, out[:150])
    code, out, _ = run(r9, "observe", event=json.dumps({"kind": "turn"}))
    j = jload(out)
    check("篡改后直接 BLOCKED（硬阻断，不看分数）", j.get("level") == "BLOCKED",
          f"level={j.get('level')} score={j.get('score')}")

    banner("10. 规则经人批准后可合法变更")
    code, out, _ = run(r9, "rules", "approve", "--by", "human", "--reason", "调高阈值测试")
    check("批准成功", code == 0 and jload(out).get("approved") is True, out[:120])
    code, out, _ = run(r9, "rules", "verify")
    check("批准后完整性恢复", code == 0 and jload(out).get("ok") is True, out[:120])

    banner("11. break-glass 可绕过，但永久留痕")
    code, out, _ = run(r9, "break-glass", "--reason", "手工测试")
    check("break-glass 返回警告", code == 0 and "break_glass" in out, out[:150])
    bg = r9 / "state" / "break_glass.jsonl"
    check("审计已落盘", bg.exists() and bg.stat().st_size > 0)

    banner("12. 监督层自身异常必须朝阻断走")
    r12 = fresh()
    rf = r12 / "rules" / "rules.json"
    unlock(rf)
    rf.write_text("{ broken json", encoding="utf-8")
    code, out, _ = run(r12, "observe", event=json.dumps({"kind": "turn"}))
    j = jload(out)
    check("规则损坏 -> deny 而非 allow",
          code in (2, 3) and j.get("decision") == "deny",
          f"code={code} decision={j.get('decision')}")

    banner("13. 事件无法解析时不误伤正常调用")
    r13 = fresh()
    code, out, _ = run(r13, "observe", event="}{ not json")
    j = jload(out)
    check("坏事件 -> 保守放行且留痕", code == 0 and j.get("decision") == "allow",
          f"code={code} {out[:80]}")
    au = (r13 / "state" / "audit.jsonl")
    check("坏事件已写审计", au.exists() and "observe_parse_error" in au.read_text(encoding="utf-8"))

    banner("14. compactions 阈值真正生效（原来是死配置）")
    rc = fresh()
    seed_thresholds(rc, compactions_watch=3, compactions_blocked=6)
    for _ in range(2):
        run(rc, "observe", event=json.dumps({"kind": "compact"}))
    st = jload(run(rc, "status", "--json")[1])
    check("2 次压缩 < watch(3) 不扣分", st.get("score") == 0, f"score={st.get('score')}")
    for _ in range(4):
        run(rc, "observe", event=json.dumps({"kind": "compact"}))
    st = jload(run(rc, "status", "--json")[1])
    check("6 次压缩 >= blocked(6) -> BLOCKED",
          st.get("level") == "BLOCKED", f"level={st.get('level')} score={st.get('score')}")

    banner("15. ①新信号：tool_errors / rework / subagents 计数")
    rn = fresh()
    for _ in range(3):
        run(rn, "observe", event=json.dumps({"kind": "tool_error", "debt_registered": False}))
    for _ in range(3):
        run(rn, "observe", event=json.dumps({"kind": "rework", "path": "a.py"}))
    run(rn, "observe", event=json.dumps({"kind": "subagent_start"}))
    st = jload(run(rn, "status", "--json")[1])
    c = st.get("counters", {})
    check("tool_errors 计数", c.get("tool_errors") == 3, f"got={c.get('tool_errors')}")
    check("rework 计数（同路径>=limit）", c.get("rework") == 1, f"got={c.get('rework')}")
    check("④ subagents_active 计数", c.get("subagents_active") == 1, f"got={c.get('subagents_active')}")
    run(rn, "observe", event=json.dumps({"kind": "subagent_stop"}))
    st = jload(run(rn, "status", "--json")[1])
    check("④ subagent_stop 递减", st.get("counters", {}).get("subagents_active") == 0,
          f"got={st.get('counters',{}).get('subagents_active')}")

    # 返工口径（2026-10-06 修）：只读调查不计，写命令才计
    for _ in range(3):
        run(rn, "observe", event=json.dumps({"kind": "command", "cmd": "Get-Content b.py",
                                             "path": "b.py", "rework_kind": "read"}))
    st = jload(run(rn, "status", "--json")[1])
    check("只读调查不计返工（rework_kind=read 被忽略）",
          st.get("counters", {}).get("rework") == 1,
          f"got={st.get('counters',{}).get('rework')}")
    for _ in range(3):
        run(rn, "observe", event=json.dumps({"kind": "command", "cmd": "Set-Content c.py",
                                             "path": "c.py", "rework_kind": "edit"}))
    st = jload(run(rn, "status", "--json")[1])
    check("写命令仍计返工（rework_kind=edit）",
          st.get("counters", {}).get("rework") == 2,
          f"got={st.get('counters',{}).get('rework')}")

    banner("16. ③calibrate：没样本就不编，有样本才给建议")
    rcal = fresh()
    code, out, _ = run(rcal, "calibrate")
    check("无样本时拒绝编造", code == 2 and jload(out).get("ok") is False, out[:120])
    for _ in range(3):
        run(rcal, "observe", event=json.dumps({"kind": "turn"}))
    code, out, _ = run(rcal, "calibrate")
    j = jload(out)
    check("有样本时给出 proposed", code == 0 and j.get("ok") is True
          and "turns_watch" in (j.get("proposed") or {}), out[:160])

    banner("17. ⑤handoff：交接单可生成且含人填部分")
    rh = fresh()
    code, out, _ = run(rh, "handoff")
    j = jload(out)
    hp = Path(j.get("path") or "")
    ok = code == 0 and hp.exists() and hp.stat().st_size > 0
    check("handoff 写出文件", ok, out[:160])
    check("含人填部分标题", ok and "人需要填写" in hp.read_text(encoding="utf-8"))

    banner("18. 修复回归：parse_failure 可见 + started_ts 同步 + 会话溯源")
    rfix = fresh()
    sf = rfix / "state" / "session.json"
    d = json.loads(sf.read_text(encoding="utf-8"))
    d["started_ts"] = "2000-01-01T00:00:00+00:00"
    sf.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    anchor = d["started_epoch"]
    run(rfix, "observe", event=json.dumps({"kind": "session_start",
                                           "session_id": "codex-thread-abc",
                                           "source": "resume"}))
    s = json.loads(sf.read_text(encoding="utf-8"))
    check("session_start 不重置计数窗口锚点（时长与计数同口径，2026-10-06 修）",
          s.get("started_ts") == "2000-01-01T00:00:00+00:00"
          and abs(s.get("started_epoch", 0) - anchor) < 1e-6,
          f"ts={s.get('started_ts')} epoch={s.get('started_epoch')}")
    check("session_start 登记线程接入且不清计数",
          bool(s.get("last_session_start_ts")) and s.get("sessions_seen") == 1,
          f"seen={s.get('sessions_seen')} last={s.get('last_session_start_ts')}")
    check("记录官方 SessionStart.source",
          s.get("last_session_source") == "resume", f"got={s.get('last_session_source')}")
    check("codex 真实会话 id 已溯源", s.get("codex_session_id") == "codex-thread-abc",
          f"got={s.get('codex_session_id')}")

    code, out, _ = run(rfix, "observe", event=json.dumps({"kind": "parse_failure",
                                                          "source": "post"}))
    j = jload(out)
    st = jload(run(rfix, "status", "--json")[1])
    check("parse_failure 计入计数（盲区不再静默）",
          (st.get("counters", {}) or {}).get("parse_failures") == 1,
          f"got={(st.get('counters',{}) or {}).get('parse_failures')}")
    check("单次盲区不进分数（watch=2，避免误杀）但状态可见",
          j.get("score", 0) == 0 and st.get("counters", {}).get("parse_failures") == 1,
          str(j)[:160])
    code, out, _ = run(rfix, "observe", event=json.dumps({"kind": "parse_failure",
                                                          "source": "post"}))
    j = jload(out)
    check("累计盲区进入分数并写进 reasons",
          j.get("score", 0) >= 1 and any("parse_failures" in r for r in j.get("reasons", [])),
          str(j)[:160])
    for _ in range(15):
        run(rfix, "observe", event=json.dumps({"kind": "parse_failure", "source": "post"}))
    st2 = jload(run(rfix, "status", "--json")[1])
    check("盲区信号只提醒、永不单独 BLOCK（15 次后仍非 BLOCKED）",
          st2.get("level") != "BLOCKED", f"level={st2.get('level')} score={st2.get('score')}")
    check("status 暴露 codex 会话关联",
          st.get("codex_session") == "codex-thread-abc", f"got={st.get('codex_session')}")

    code, out, _ = run(rfix, "debt", "add", "--text", "测试用欠账", "--severity", "critical",
                        "--deliverable", "demo")
    st3 = jload(run(rfix, "status", "--json")[1])
    check("critical 欠账计分（+3）", (st3.get("score") or 0) >= (j.get("score") or 0) + 3,
          f"before={j.get('score')} after={st3.get('score')}")
    code, out, _ = run(rfix, "doctor", "--json")
    dj = jload(out)
    check("doctor 自检可运行并输出结构化结果",
          code in (0, 2) and isinstance(dj.get("checks"), list) and len(dj.get("checks") or []) > 0,
          out[:160])

    banner("9. 瞬时失败抗性（2026-10-06 欠账 #2）")
    # 9a. 事件文本混入孤立代理项( lone surrogate )时，jsonl 落盘曾抛
    #     UnicodeEncodeError，被 main() 兜成 score=999 / deny —— 一次工具调用被硬拒。
    r9 = fresh()
    lone_ev = json.dumps({"kind": "command",
                          "cmd": "echo " + chr(0xDCAD) + " x " + chr(0xDCAE)})
    code, out, _e = run(r9, "observe", event=lone_ev)
    j9 = jload(out)
    check("孤立代理项事件不再崩成 999 硬拒",
          j9.get("score") != 999 and j9.get("decision") != "deny",
          f"code={code} out={out[:160]}")
    ev9 = r9 / "state" / "events.jsonl"
    check("该事件仍被落盘（不静默丢失）",
          ev9.exists() and "echo" in ev9.read_text(encoding="utf-8", errors="replace"), "")

    # 9b. rules.json 被瞬时写坏（撕裂）时，读重试必须扛住，不能硬拒。
    r9b = fresh()
    rf9 = r9b / "rules" / "rules.json"
    good9 = rf9.read_bytes()
    stop9 = {"v": False}

    def _torn9():
        while not stop9["v"]:
            try:
                rf9.write_bytes(b"{")
                time.sleep(0.004)
                rf9.write_bytes(good9)
            except Exception:
                pass
            time.sleep(0.004)

    th9 = threading.Thread(target=_torn9, daemon=True)
    th9.start()
    denies9 = 0
    for _ in range(40):
        _c, _o, _e2 = run(r9b, "observe", event='{"kind": "turn"}')
        if jload(_o).get("decision") == "deny":
            denies9 += 1
    stop9["v"] = True
    th9.join(timeout=3)
    check("rules.json 撕裂写期间 40 次 observe 无一硬拒", denies9 == 0, f"denies={denies9}")

    banner("19. repeat_command 只读豁免（2026-10-06 第十四轮修）")
    rp = fresh()
    for _ in range(4):
        run(rp, "observe", event=json.dumps({"kind": "command", "cmd": "Get-Content a.py",
                                             "readonly": True}))
    stp = jload(run(rp, "status", "--json")[1])
    check("只读命令重复不计 repeat_command",
          "repeat_commands=" not in " ".join(stp.get("reasons") or []), str(stp.get("reasons")))
    check("只读重复已单独可见（read_commands）",
          (stp.get("counters", {}) or {}).get("read_commands") == 1, str(stp.get("counters")))
    for _ in range(4):
        run(rp, "observe", event=json.dumps({"kind": "command", "cmd": "Set-Content a.py -Value x"}))
    stp2 = jload(run(rp, "status", "--json")[1])
    check("写命令重复仍计 repeat_command",
          any("repeat_commands=" in r for r in (stp2.get("reasons") or [])), str(stp2.get("reasons")))
    rq = fresh()
    for _ in range(4):
        run(rq, "observe", event=json.dumps({"kind": "command", "cmd": "echo hi"}))
    stq = jload(run(rq, "status", "--json")[1])
    check("缺 readonly 的旧载荷保持旧行为（仍计分）",
          any("repeat_commands=" in r for r in (stq.get("reasons") or [])), str(stq.get("reasons")))

    banner("20. 外部维护模式：限时放行、自动过期、不可从 Codex 内启停")
    rm = fresh()
    seed_thresholds(rm, turns_watch=1, turns_degraded=2, turns_blocked=3)
    turns(rm, 3)
    code, out, _ = run(rm, "observe", event=json.dumps({"kind": "turn"}))
    check("维护前 BLOCKED 拒绝", code == 2 and jload(out).get("decision") == "deny", out[:160])
    code, out, _ = run_as_codex(rm, "maintenance", "on", "--by", "human", "--reason", "测试", "--minutes", "1")
    check("Codex 内不能启动维护模式", code == 2 and jload(out).get("ok") is False and not (rm / "state" / "maintenance.json").exists(), out[:160])
    code, out, _ = run_external(rm, "maintenance", "on", "--by", "human", "--reason", "测试", "--minutes", "1")
    check("外部可启动限时维护模式", code == 0 and jload(out).get("ok") is True, out[:160])
    st = jload(run(rm, "status", "--json")[1])
    check("状态保留底层 BLOCKED，但维护模式放行", st.get("level") == "BLOCKED" and st.get("maintenance_active") is True, str(st)[:180])
    code, out, _ = run(rm, "observe", event=json.dumps({"kind": "turn"}))
    check("维护模式下 Hook 决策为 allow", code == 0 and jload(out).get("decision") == "allow" and jload(out).get("maintenance_active") is True, out[:180])
    mf = rm / "state" / "maintenance.json"
    md = json.loads(mf.read_text(encoding="utf-8"))
    md["until"] = "2000-01-01T00:00:00+00:00"
    mf.write_text(json.dumps(md, ensure_ascii=False, indent=2), encoding="utf-8")
    code, out, _ = run(rm, "observe", event=json.dumps({"kind": "turn"}))
    check("维护窗口过期后自动恢复拒绝", code == 2 and jload(out).get("decision") == "deny", out[:180])
    code, out, _ = run_external(rm, "maintenance", "off", "--by", "human", "--reason", "结束测试")
    check("外部可关闭维护模式", code == 0 and jload(out).get("maintenance", {}).get("active") is False, out[:160])

    print()
    print("=" * 68)
    if FAILURES:
        print(f"结果: {len(FAILURES)} 项未通过")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("结果: 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
