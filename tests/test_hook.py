# -*- coding: utf-8 -*-
"""hook 端到端验证：喂真实 PreToolUse payload，检查它到底放行还是拦截。"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

SRC = Path(__file__).resolve().parent.parent
PY = sys.executable
PS = "powershell.exe"
FAILURES = []


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    line = f"[{mark}] {name}"
    if detail and not cond:
        line += f"  :: {detail}"
    print(line)
    if not cond:
        FAILURES.append(name)


def sup(root, *args, event=None):
    r = subprocess.run([PY, str(root / "supervisor.py"), *args],
                       capture_output=True, text=True, encoding="utf-8",
                       input=event, cwd=str(root))
    return r.returncode, (r.stdout or "").strip()


def call_hook(hook, payload):
    r = subprocess.run([PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(hook)],
                       capture_output=True, text=True, encoding="utf-8",
                       input=payload, timeout=90)
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def sup_external(root, *args, event=None):
    env = os.environ.copy()
    for k in list(env):
        if k.startswith("CODEX_") or k == "OPENAI_API_KEY":
            env.pop(k, None)
    r = subprocess.run([PY, str(root / "supervisor.py"), *args],
                       capture_output=True, text=True, encoding="utf-8",
                       input=event, cwd=str(root), env=env)
    return r.returncode, (r.stdout or "").strip()


def jload(raw):
    try:
        return json.loads(raw)
    except Exception:
        return {}


def unlock(p):
    subprocess.run(["attrib", "-R", str(p)], capture_output=True)


def seed_thresholds(root, **kw):
    """测试自带受控阈值，避免和生产标定耦合。"""
    rf = root / "rules" / "rules.json"
    unlock(rf)
    d = json.loads(rf.read_text(encoding="utf-8"))
    d["thresholds"].update(kw)
    rf.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    code, out = sup(root, "rules", "approve", "--by", "test", "--reason", "测试受控阈值")
    assert code == 0, f"seed_thresholds failed: {out}"


def fresh_with_hook():
    d = Path(tempfile.mkdtemp(prefix="hook_test_"))
    dst = d / "sup"
    shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns("state", "work", "tests", "logs", "ledger", "__pycache__"))
    (dst / "state").mkdir(exist_ok=True)
    (dst / "checks").mkdir(exist_ok=True)
    (dst / "hook_config.json").write_text(json.dumps({
        "supervisor_home": str(dst),
        "python": PY,
    }, ensure_ascii=False), encoding="utf-8")
    sup(dst, "init")
    return dst, dst / "hook_supervisor.ps1"


def banner(t):
    print()
    print("=" * 68)
    print(t)
    print("=" * 68)


PAYLOAD_EXEC = json.dumps({"tool_name": "exec_command",
                           "tool_input": {"command": "echo hello"}}, ensure_ascii=False)
PAYLOAD_STATUS = json.dumps({"tool_name": "exec_command",
                             "tool_input": {"command": 'python supervisor.py status'}}, ensure_ascii=False)
PAYLOAD_IMAGE = json.dumps({"tool_name": "view_image",
                            "tool_input": {"path": "C:/tmp/x.png"}}, ensure_ascii=False)


def main():
    banner("1. NORMAL 状态：hook 必须完全透明（输出 {}）")
    root, hook = fresh_with_hook()
    code, out, err = call_hook(hook, PAYLOAD_EXEC)
    check("退出码 0", code == 0, f"code={code} err={err[:120]}")
    check("输出为空对象 {}（不干扰正常调用）", out.strip() == "{}", out[:160])

    banner("2. DEGRADED 状态：放行但注入介入指令")
    seed_thresholds(root, turns_watch=10, turns_degraded=20, turns_blocked=30)
    for _ in range(20):
        sup(root, "observe", event=json.dumps({"kind": "turn"}))
    st = jload(sup(root, "status", "--json")[1])
    check("已进入 DEGRADED", st.get("level") == "DEGRADED", f"level={st.get('level')}")
    code, out, err = call_hook(hook, PAYLOAD_EXEC)
    j = jload(out)
    hso = j.get("hookSpecificOutput", {})
    check("hook 放行", hso.get("permissionDecision") == "allow", out[:200])
    check("介入指令已注入 reason", "SUPERVISOR" in (hso.get("permissionDecisionReason") or ""),
          (hso.get("permissionDecisionReason") or "")[:120])

    banner("3. BLOCKED 状态：hook 必须真正拦下工具调用")
    for _ in range(10):
        sup(root, "observe", event=json.dumps({"kind": "turn"}))
    st = jload(sup(root, "status", "--json")[1])
    check("已进入 BLOCKED", st.get("level") == "BLOCKED", f"level={st.get('level')}")
    code, out, err = call_hook(hook, PAYLOAD_EXEC)
    j = jload(out)
    hso = j.get("hookSpecificOutput", {})
    check("hook 拒绝", hso.get("permissionDecision") == "deny",
          f"got={hso.get('permissionDecision')} raw={out[:200]}")
    check("拒绝理由含 SUPERVISOR", "SUPERVISOR" in (hso.get("permissionDecisionReason") or ""))

    banner("3b. 外部维护模式：BLOCKED 下临时放行但仍保留底层等级")
    code, out = sup_external(root, "maintenance", "on", "--by", "test-human",
                             "--reason", "验证 hook 放行", "--minutes", "1")
    check("外部可开启维护模式", code == 0 and jload(out).get("ok") is True, out[:160])
    code, out, err = call_hook(hook, PAYLOAD_EXEC)
    check("维护模式下写命令被放行", out.strip() == "{}", out[:200])
    st = jload(sup(root, "status", "--json")[1])
    check("底层仍是 BLOCKED，维护状态可见",
          st.get("level") == "BLOCKED" and st.get("maintenance_active") is True, str(st)[:180])
    code, out = sup_external(root, "maintenance", "off", "--by", "test-human",
                             "--reason", "验证结束")
    check("外部可关闭维护模式", code == 0 and jload(out).get("maintenance", {}).get("active") is False, out[:160])

    banner("4. BLOCKED 下的只读白名单：不能把人锁死")
    code, out, err = call_hook(hook, PAYLOAD_STATUS)
    check("status 命令仍放行（避免无法查看状态）", out.strip() == "{}", out[:200])

    banner("5. view_image 事件被计入监督状态")
    root5, hook5 = fresh_with_hook()
    for _ in range(6):
        call_hook(hook5, PAYLOAD_IMAGE)
    st = jload(sup(root5, "status", "--json")[1])
    check("images 计数 >= 3", (st.get("counters", {}).get("images") or 0) >= 3,
          f"images={st.get('counters',{}).get('images')}")

    banner("6. supervisor 不可达时不能砖掉 Codex（放行 + 记日志）")
    d = Path(tempfile.mkdtemp(prefix="hook_test_"))
    (d / "exec").mkdir()
    shutil.copy2(SRC / "hook_supervisor.ps1", d / "hook_supervisor.ps1")
    (d / "hook_config.json").write_text(json.dumps({
        "supervisor_home": str(d / "does-not-exist"), "python": PY}), encoding="utf-8")
    code, out, err = call_hook(d / "hook_supervisor.ps1", PAYLOAD_EXEC)
    check("不可达时放行", code == 0 and out.strip() == "{}", f"code={code} out={out[:120]}")
    lg = d / "logs" / "hook.log"
    check("已记录日志", lg.exists() and lg.stat().st_size > 0)

    banner("7a. PostToolUse hook：工具报错被采集，成功调用不采集")
    root7, _ = fresh_with_hook()
    post = root7 / "hook_post.ps1"
    before = (jload(sup(root7, "status", "--json")[1]).get("counters", {}) or {}).get("tool_errors") or 0
    code, out, err = call_hook(post, json.dumps({
        "hook_event_name": "PostToolUse", "tool_name": "exec_command",
        "tool_input": {"command": "exit 1"},
        "tool_response": {"exit_code": 1, "stderr": "boom"}}, ensure_ascii=False))
    check("失败调用仍输出 {}（不干预）", code == 0 and out.strip() == "{}", out[:140])
    call_hook(post, json.dumps({
        "hook_event_name": "PostToolUse", "tool_name": "exec_command",
        "tool_input": {"command": "echo hi"},
        "tool_response": {"exit_code": 0}}, ensure_ascii=False))
    after = (jload(sup(root7, "status", "--json")[1]).get("counters", {}) or {}).get("tool_errors") or 0
    check("只有失败被计入 tool_errors", after == before + 1, f"before={before} after={after}")

    banner("7b. ④ Subagent 事件被采集")
    root7b, _ = fresh_with_hook()
    sess = root7b / "hook_session.ps1"
    call_hook(sess, json.dumps({"hook_event_name": "SubagentStart", "agent_id": "a1",
                                "agent_type": "worker"}, ensure_ascii=False))
    st = jload(sup(root7b, "status", "--json")[1])
    check("subagents_active = 1", (st.get("counters", {}) or {}).get("subagents_active") == 1,
          f"got={(st.get('counters',{}) or {}).get('subagents_active')}")
    call_hook(sess, json.dumps({"hook_event_name": "SubagentStop", "agent_id": "a1",
                                "agent_type": "worker"}, ensure_ascii=False))
    st = jload(sup(root7b, "status", "--json")[1])
    check("subagents_active 回到 0", (st.get("counters", {}) or {}).get("subagents_active") == 0,
          f"got={(st.get('counters',{}) or {}).get('subagents_active')}")

    banner("7. 垃圾输入不能崩，且必须留下证据（不能静默）")
    root7c, hook7c = fresh_with_hook()
    code, out, err = call_hook(hook7c, "not json at all")
    check("垃圾输入 -> {} 且 exit 0", code == 0 and out.strip() == "{}", f"code={code} out={out[:120]}")
    dumps = sorted((root7c / "logs").glob("stdin-fail-supervisor-*.txt"))
    check("解析失败的原始 stdin 已落盘",
          len(dumps) == 1 and dumps[0].read_text(encoding="utf-8").strip() == "not json at all",
          f"dumps={[x.name for x in dumps]}")
    st = jload(sup(root7c, "status", "--json")[1])
    check("解析失败计入 parse_failures",
          (st.get("counters", {}) or {}).get("parse_failures") == 1,
          f"got={(st.get('counters',{}) or {}).get('parse_failures')}")
    check("单次盲区不计分但可见（watch=2）", (st.get("score") or 0) == 0
          and st.get("level") != "BLOCKED", f"score={st.get('score')} level={st.get('level')}")

    banner("7c. PostToolUse hook 同样不能静默失败")
    code, out, err = call_hook(root7c / "hook_post.ps1", "{broken")
    check("post 垃圾输入 -> {} 且 exit 0", code == 0 and out.strip() == "{}", f"code={code} out={out[:120]}")
    pdumps = sorted((root7c / "logs").glob("stdin-fail-post-*.txt"))
    check("post 解析失败的原始 stdin 已落盘", len(pdumps) == 1, f"dumps={[x.name for x in pdumps]}")
    st = jload(sup(root7c, "status", "--json")[1])
    check("post 解析失败同样计数",
          (st.get("counters", {}) or {}).get("parse_failures") == 2,
          f"got={(st.get('counters',{}) or {}).get('parse_failures')}")
    check("累计 2 次盲区才进分数，且不 BLOCK",
          (st.get("score") or 0) >= 1 and st.get("level") != "BLOCKED",
          f"score={st.get('score')} level={st.get('level')}")

    banner("8. 空 stdin 不能崩")
    code, out, err = call_hook(hook, "")
    check("空输入 -> {} 且 exit 0", code == 0 and out.strip() == "{}", f"code={code} out={out[:120]}")

    banner("9. 白名单必须认本机宿主的工具名 Bash（P0 回归）")
    root9, hook9 = fresh_with_hook()
    sf = root9 / "state" / "session.json"
    d = json.loads(sf.read_text(encoding="utf-8"))
    d["tool_errors"] = 999
    d["unregistered_failures"] = 999
    sf.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    code, out, err = call_hook(hook9, json.dumps({"tool_name": "Bash", "tool_input": {
        "command": "python supervisor.py status"}, "session_id": "cx-1"}, ensure_ascii=False))
    check("BLOCKED 下 Bash 只读命令仍放行（逃生口有效）", code == 0 and out.strip() == "{}",
          f"out={out[:160]}")
    code, out, err = call_hook(hook9, json.dumps({"tool_name": "Bash", "tool_input": {
        "command": "rm -rf /tmp/whatever"}, "session_id": "cx-1"}, ensure_ascii=False))
    check("BLOCKED 下 Bash 写命令被拒", jload(out).get("hookSpecificOutput", {}).get(
        "permissionDecision") == "deny", f"out={out[:160]}")
    evs = [json.loads(x) for x in (root9 / "state" / "events.jsonl").read_text(
        encoding="utf-8").splitlines() if x.strip()]
    check("Bash 工具名也采集 command 信号", any(e.get("kind") == "command" for e in evs))
    check("事件带宿主 session_id（可溯源）", any(e.get("payload", {}).get("session_id") == "cx-1" for e in evs))

    banner("10. 非 ASCII 载荷必须原样解析（hook stdin 编码回归，2026-10-06）")
    root10, hook10 = fresh_with_hook()
    cn_cmd = "echo 中文注释"
    code, out, err = call_hook(hook10, json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": cn_cmd}, "session_id": "cn-1"},
        ensure_ascii=False))
    check("含中文的 PreToolUse 载荷 -> {} 且 exit 0", code == 0 and out.strip() == "{}",
          f"code={code} out={out[:120]} err={err[:120]}")
    d10 = list((root10 / "logs").glob("stdin-fail-*.txt"))
    check("含中文的载荷不再被误判为解析失败（无 stdin-fail 落盘）", not d10, [x.name for x in d10])
    ev10 = root10 / "state" / "events.jsonl"
    cmds10 = []
    if ev10.exists():
        cmds10 = [json.loads(x)["payload"].get("cmd") for x in ev10.read_text(
            encoding="utf-8").splitlines() if x.strip() and json.loads(x).get("kind") == "command"]
    check("中文命令逐字节送达 supervisor（host->hook->python 全链路 UTF-8）",
          bool(cmds10) and cmds10[-1] == cn_cmd, cmds10[-1:])

    banner("11. 解析失败落盘的必须是原始字节（取证可信）")
    root11, hook11 = fresh_with_hook()
    raw11 = '{"tool_name":"Bash","tool_input":{"command":"echo 中'.encode("utf-8")[:-1]  # 截断的 UTF-8
    subprocess.run([PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(hook11)],
                   input=raw11, capture_output=True, timeout=90)
    d11 = sorted((root11 / "logs").glob("stdin-fail-supervisor-*.txt"))
    check("截断载荷会落盘一份取证文件", len(d11) == 1, [x.name for x in d11])
    check("落盘内容与送入字节逐字节一致",
          bool(d11) and d11[0].read_bytes() == raw11,
          (d11[0].read_bytes()[:60] if d11 else b""))

    banner("12. run_hook.cmd 启动器（hooks.json 现在用的写法）")
    root12, _ = fresh_with_hook()
    lch = root12 / "run_hook.cmd"
    check("run_hook.cmd 已随安装目录分发", lch.exists(), str(lch))
    if lch.exists():
        cases = (
            ("supervisor", json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hi"},
                                       "session_id": "L1"}, ensure_ascii=False)),
            ("post", json.dumps({"hook_event_name": "PostToolUse", "tool_name": "exec_command",
                                 "tool_input": {"command": "exit 1"},
                                 "tool_response": {"exit_code": 1}}, ensure_ascii=False)),
            ("session", json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "hi"},
                                   ensure_ascii=False)),
        )
        for mode, payload in cases:
            rr = subprocess.run(["cmd.exe", "/d", "/c", str(lch), mode], input=payload,
                                capture_output=True, text=True, encoding="utf-8", timeout=90)
            check("run_hook.cmd " + mode + " -> 合法 JSON 且 exit 0",
                  rr.returncode == 0 and (rr.stdout or "").strip().startswith("{"),
                  f"code={rr.returncode} out={(rr.stdout or '')[:120]} err={(rr.stderr or '')[:120]}")

    banner("13. 只读白名单不能被拼接命令绕过（2026-10-06 修洞）")
    root13, hook13 = fresh_with_hook()
    sf13 = root13 / "state" / "session.json"
    d13 = json.loads(sf13.read_text(encoding="utf-8"))
    d13["tool_errors"] = 999
    d13["unregistered_failures"] = 999
    sf13.write_text(json.dumps(d13, ensure_ascii=False), encoding="utf-8")

    def _dec13(cmd):
        _c, o, _e = call_hook(hook13, json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": cmd}, "session_id": "ro-1"},
            ensure_ascii=False))
        if o.strip() == "{}":
            return "allow"
        return jload(o).get("hookSpecificOutput", {}).get("permissionDecision") or "?"

    check("BLOCKED 下纯只读命令仍放行（逃生口有效）",
          _dec13("python supervisor.py status") == "allow",
          _dec13("python supervisor.py status"))
    check("BLOCKED 下 cd <dir> ; 只读命令 仍放行",
          _dec13("cd /tmp; python supervisor.py status") == "allow",
          _dec13("cd /tmp; python supervisor.py status"))
    check("前缀拼接 rm ... ; 只读命令 被拒（原越权洞）",
          _dec13("rm -rf /tmp/x; python supervisor.py status") == "deny",
          _dec13("rm -rf /tmp/x; python supervisor.py status"))
    check("后缀拼接 只读命令 ; rm ... 被拒",
          _dec13("python supervisor.py status; rm -rf /tmp/x") == "deny",
          _dec13("python supervisor.py status; rm -rf /tmp/x"))
    check("非只读子命令仍被拒",
          _dec13("python supervisor.py resume --by x --reason y") == "deny",
          _dec13("python supervisor.py resume --by x --reason y"))

    banner("14. 返工信号只认写入，不认只读调查（2026-10-06 修）")
    root14, hook14 = fresh_with_hook()
    ro14 = json.dumps({"tool_name": "Bash", "tool_input": {
        "command": "Get-Content C:\\temp\\probe.py"}, "session_id": "rw-1"}, ensure_ascii=False)
    for _ in range(5):
        call_hook(hook14, ro14)
    st = jload(sup(root14, "status", "--json")[1])
    c = st.get("counters", {}) or {}
    check("反复只读同一文件不计返工", (c.get("rework") or 0) == 0, f"rework={c.get('rework')}")
    check("只读重复不出现在 reasons 的返工项",
          "rework=" not in " ".join(st.get("reasons") or []), str(st.get("reasons")))
    wr14 = json.dumps({"tool_name": "Bash", "tool_input": {
        "command": "Set-Content C:\\temp\\probe.py -Value x"}, "session_id": "rw-1"}, ensure_ascii=False)
    for _ in range(3):
        call_hook(hook14, wr14)
    st = jload(sup(root14, "status", "--json")[1])
    c = st.get("counters", {}) or {}
    check("重复写同一文件才计返工", (c.get("rework") or 0) == 1, f"rework={c.get('rework')}")

    def touches14():
        return (json.loads((root14 / "state" / "session.json").read_text(
            encoding="utf-8")).get("touches") or {})

    # 回归（2026-10-06 复查）：带前置 cd 的写重定向，目标必须是被写文件，不能是 cd 的目录
    call_hook(hook14, json.dumps({"tool_name": "Bash", "tool_input": {
        "command": "cd C:\\temp\\proj; Get-Content C:\\temp\\proj\\a.py > C:\\temp\\proj\\log1.txt"},
        "session_id": "rw-2"}, ensure_ascii=False))
    tp = touches14()
    check("写目标落在被写文件而非 cd 目录",
          "C:\\temp\\proj" not in tp and any(k.endswith("log1.txt") for k in tp), str(tp))

    # 回归（2026-10-06 复查）：(>=N) 这类比较不能被当成写重定向
    call_hook(hook14, json.dumps({"tool_name": "Bash", "tool_input": {
        "command": "cd C:\\temp\\proj; python -c \"print(2>=3)\""},
        "session_id": "rw-2"}, ensure_ascii=False))
    check("(>=N) 比较不产生写目标", "C:\\temp\\proj" not in touches14(), str(touches14()))

    # 回归（2026-10-06 复查）：内联 here-string 正文里的关键词不是真实写命令
    hs_read = ("$h = @'\n" + "refs @'...'@ and Set-Content -LiteralPath C:\\fake\\x.ps1\n"
               + "'@\n" + "python C:\\tools\\run.py")
    call_hook(hook14, json.dumps({"tool_name": "Bash", "tool_input": {"command": hs_read},
                                  "session_id": "rw-3"}, ensure_ascii=False))
    check("here-string 正文里的写关键词不算写盘",
          "C:\\fake\\x.ps1" not in touches14(), str(touches14()))

    # 回归（2026-10-06 复查）：here-string + 真实写命令，目标取真实路径
    hs_write = ("$h = @'\n" + "refs @'...'@ and (>=3)\n" + "'@\n"
                + "Set-Content -LiteralPath C:\\temp\\real.ps1 -Value $h")
    call_hook(hook14, json.dumps({"tool_name": "Bash", "tool_input": {"command": hs_write},
                                  "session_id": "rw-3"}, ensure_ascii=False))
    check("here-string + 真实写命令：目标取真实路径",
          any(k.endswith("real.ps1") for k in touches14()), str(touches14()))

    # 回归（2026-10-06 第三轮复查）：这些只读命令曾被误判成写盘
    ro_cases = [
        "python -c \"print(1 > 0)\"",
        "python -c \"print('a > b')\"",
        "python -c \"import sys; sys.stdout.write('x')\"",
        "Select-String -Path a.py -Pattern \">\"",
        "git stash list",
        "sc query",
    ]
    before = set(touches14())
    for cmd in ro_cases:
        call_hook(hook14, json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd},
                                      "session_id": "rw-4"}, ensure_ascii=False))
    added = set(touches14()) - before
    check("只读命令不再被误判成写盘（比较/引号/stdout/stash/sc）", not added, f"added={added}")

    banner("15. view_image 守卫必须按 UTF-8 读 stdin（中文路径回归）")
    guard = Path.home() / ".codex" / "tools" / "view_image_guard.ps1"
    if not guard.exists():
        print("[SKIP] 未安装 view_image_guard.ps1")
    else:
        gdir = Path(tempfile.mkdtemp(prefix="guard_test_"))
        gimg = gdir / "中文大图_测试.png"
        gimg.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * (600 * 1024))  # >400KB 足以触发
        glog = Path.home() / ".codex" / "cache" / "imgpreview" / "hook.log"
        before = glog.stat().st_size if glog.exists() else 0
        payload = json.dumps({"tool_name": "view_image", "tool_input": {"path": str(gimg)}},
                             ensure_ascii=False)
        rr = subprocess.run([PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(guard)],
                            input=payload.encode("utf-8"), capture_output=True, timeout=90)
        newlog = ""
        if glog.exists():
            newlog = glog.read_bytes()[before:].decode("utf-8", "replace")
        check("中文路径能被守卫解析（不再 no usable path）",
              "no usable path" not in newlog, newlog.strip()[:200])
        # 只要日志里出现 preview failed / downscaled / guard error 三者之一，
        # 就证明它已经越过"解析路径"这一步（路径不存在时只会记 no usable path）。
        check("路径已解析并进入预览分支（证明中文路径确实被认出）",
              ("preview failed" in newlog) or ("downscaled" in newlog) or ("guard error" in newlog),
              newlog.strip()[:200])

    banner("16. hook stdout 必须是 UTF-8（cp936 控制台回归，2026-10-06）")
    root16, _ = fresh_with_hook()
    sf16 = root16 / "state" / "session.json"
    d16 = json.loads(sf16.read_text(encoding="utf-8"))
    d16["tool_errors"] = 999
    d16["unregistered_failures"] = 999
    sf16.write_text(json.dumps(d16, ensure_ascii=False), encoding="utf-8")
    # 用 chcp 936 + powershell.exe 模拟"控制台输出码页是 cp936"的宿主
    wrap = root16 / "cp936.cmd"
    wrap.write_text("@echo off\nchcp 936 >nul\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File \"%~1\"\n",
                    encoding="ascii")
    payload16 = json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "hi",
                            "session_id": "cn-936"}, ensure_ascii=False)
    rr = subprocess.run(["cmd.exe", "/d", "/c", str(wrap), str(root16 / "hook_session.ps1")],
                        input=payload16.encode("utf-8"), capture_output=True, timeout=90)
    raw16 = rr.stdout or b""
    try:
        txt16 = raw16.decode("utf-8")
        utf8_ok = True
    except Exception:
        txt16 = raw16.decode("gbk", "replace")
        utf8_ok = False
    check("cp936 控制台下 hook stdout 仍是 UTF-8（不被编成 GBK）", utf8_ok, txt16[:140])
    check("注入文本中文完好（含「降智」）", "降智" in txt16, txt16[:140])

    banner("17. tool_response 形状：字符串=不可判定（必须可见，不能静默）")
    root17, _ = fresh_with_hook()
    post17 = root17 / "hook_post.ps1"
    call_hook(post17, json.dumps({
        "hook_event_name": "PostToolUse", "tool_name": "Bash",
        "tool_input": {"command": "exit 7"},
        "tool_response": "some output\n",          # 本机宿主的真实形状：字符串
        "session_id": "shape-str"}, ensure_ascii=False))
    c17 = jload(sup(root17, "status", "--json")[1]).get("counters", {}) or {}
    check("字符串 tool_response 记为不可判定（可见）",
          c17.get("tool_response_shape") == "string"
          and c17.get("tool_response_judgeable") is False, str(c17))
    check("不可判定不得被当成 tool_error", (c17.get("tool_errors") or 0) == 0, str(c17))
    call_hook(post17, json.dumps({
        "hook_event_name": "PostToolUse", "tool_name": "exec_command",
        "tool_input": {"command": "exit 1"},
        "tool_response": {"exit_code": 1},           # 对象形状仍按原逻辑判定
        "session_id": "shape-obj"}, ensure_ascii=False))
    c17b = jload(sup(root17, "status", "--json")[1]).get("counters", {}) or {}
    check("对象 tool_response 仍能判失败", (c17b.get("tool_errors") or 0) == 1, str(c17b))

    banner("18. 只读命令重复不计 repeat_command（2026-10-06 第十四轮修）")
    root18, hook18 = fresh_with_hook()
    ro18 = json.dumps({"tool_name": "Bash", "tool_input": {
        "command": "Get-Content C:\\temp\\probe.py"}, "session_id": "rep-1"}, ensure_ascii=False)
    for _ in range(5):
        call_hook(hook18, ro18)
    st18 = jload(sup(root18, "status", "--json")[1])
    c18 = st18.get("counters", {}) or {}
    check("只读命令重复不计 repeat_command",
          "repeat_commands=" not in " ".join(st18.get("reasons") or []), str(st18.get("reasons")))
    check("只读重复已单独可见（read_commands）", (c18.get("read_commands") or 0) == 1, str(c18))
    wr18 = json.dumps({"tool_name": "Bash", "tool_input": {
        "command": "Set-Content C:\\temp\\probe.py -Value x"}, "session_id": "rep-1"}, ensure_ascii=False)
    for _ in range(5):
        call_hook(hook18, wr18)
    check("写命令重复仍计 repeat_command",
          any("repeat_commands=" in r for r in (jload(sup(root18, "status", "--json")[1]).get("reasons") or [])),
          "see session")
    root18c, hook18c = fresh_with_hook()
    mixed18 = json.dumps({"tool_name": "Bash", "tool_input": {
        "command": "Get-Content a.py; Remove-Item b.py"}, "session_id": "rep-2"}, ensure_ascii=False)
    for _ in range(5):
        call_hook(hook18c, mixed18)
    st18c = jload(sup(root18c, "status", "--json")[1])
    check("只读+写拼接命令不被豁免（仍计 repeat_command）",
          any("repeat_commands=" in r for r in (st18c.get("reasons") or [])), str(st18c.get("reasons")))

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
