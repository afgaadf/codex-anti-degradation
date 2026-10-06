# -*- coding: utf-8 -*-
"""会话级 hook 端到端验证：上下文上报 + 压缩信号 + 介入注入。"""
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
os.environ.setdefault('SUPERVISOR_CORRECTIONS_PATH', str(Path(tempfile.gettempdir()) / 'no-supervisor-corrections.json'))
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


def find_shell():
    p = Path(r"C:\Users\taich\AppData\Local\Microsoft\WindowsApps\pwsh.exe")
    if p.exists():
        return str(p)
    return "pwsh"


SHELL = find_shell()


def sup(root, *args):
    r = subprocess.run([PY, str(root / "supervisor.py"), *args],
                       capture_output=True, text=True, encoding="utf-8", cwd=str(root))
    return r.returncode, (r.stdout or "").strip()


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


def call_hook(hook, payload):
    r = subprocess.run([SHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(hook)],
                       capture_output=True, text=True, encoding="utf-8",
                       input=payload, timeout=90)
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def jload(raw):
    try:
        return json.loads(raw)
    except Exception:
        return {}


def fresh():
    d = Path(tempfile.mkdtemp(prefix="sess_test_"))
    dst = d / "sup"
    shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns("state", "work", "tests", "logs", "__pycache__", ".git", "backup"))
    (dst / "state").mkdir(exist_ok=True)
    (dst / "checks").mkdir(exist_ok=True)
    (dst / "hook_config.json").write_text(json.dumps({
        "supervisor_home": str(dst), "python": PY, "shell": SHELL}), encoding="utf-8")
    sup(dst, "init")
    # 断言写死了"300KB=DEGRADED / 1 次压缩=DEGRADED / 2 次=BLOCKED"，
    # 必须钉住出厂阈值，否则会跟着被人调高过的生产 rules.json 一起漂移。
    seed_thresholds(dst, context_chars_watch=120000, context_chars_degraded=250000,
                    context_chars_blocked=400000, compactions_watch=1, compactions_blocked=2)
    return dst, dst / "hook_session.ps1"


def banner(t):
    print()
    print("=" * 68)
    print(t)
    print("=" * 68)


def mk_transcript(d, size_bytes):
    f = d / f"transcript_{size_bytes}.jsonl"
    f.write_bytes(b'{"role":"user","content":"' + b"x" * max(0, size_bytes - 40) + b'"}\n')
    return str(f)


def payload(d, event, transcript=None, prompt="hello", source=None):
    p = {"hook_event_name": event, "session_id": "testsess", "cwd": str(d),
         "turn_id": "t1", "model": "test", "permission_mode": "default", "prompt": prompt}
    if transcript:
        p["transcript_path"] = transcript
    if source:
        p["source"] = source
    return json.dumps(p, ensure_ascii=False)


def main():
    banner("1. SessionStart 必须完全透明")
    root, hook = fresh()
    code, out, err = call_hook(hook, payload(root, "SessionStart", source="startup"))
    check("退出码 0", code == 0, f"code={code} err={err[:120]}")
    check("输出 {}", out.strip() == "{}", out[:150])
    st0 = jload(sup(root, "status", "--json")[1])
    check("官方 source 已透传并记录",
          st0.get("counters", {}).get("last_session_source") == "startup",
          str(st0.get("counters", {})))

    banner("2. UserPromptSubmit 正常放行，并计数 turn")
    tr = mk_transcript(root, 500)
    code, out, _ = call_hook(hook, payload(root, "UserPromptSubmit", tr))
    check("输出 {}", out.strip() == "{}", out[:150])
    st = jload(sup(root, "status", "--json")[1])
    check("turn 被计数", (st.get("counters", {}).get("turns") or 0) >= 1,
          f"turns={st.get('counters',{}).get('turns')}")

    banner("3. 从 transcript_path 上报上下文体积（这是补上的关键能力）")
    tr_big = mk_transcript(root, 300000)
    call_hook(hook, payload(root, "UserPromptSubmit", tr_big))
    st = jload(sup(root, "status", "--json")[1])
    ctx = st.get("counters", {}).get("context_chars") or 0
    check("context_chars 已上报且接近文件大小", 250000 < ctx <= 320000, f"ctx={ctx}")
    check("体积把等级推到 DEGRADED", st.get("level") == "DEGRADED",
          f"level={st.get('level')} score={st.get('score')}")

    banner("4. DEGRADED 时 UserPromptSubmit 注入 additionalContext")
    code, out, _ = call_hook(hook, payload(root, "UserPromptSubmit", tr_big))
    j = jload(out)
    hso = j.get("hookSpecificOutput", {})
    ctxmsg = hso.get("additionalContext") or ""
    check("注入了 additionalContext", "SUPERVISOR" in ctxmsg, out[:200])
    check("注明事件名", hso.get("hookEventName") == "UserPromptSubmit", str(hso.get("hookEventName")))
    check("包含当前体积", "KB" in ctxmsg, ctxmsg[:160])

    banner("5. PreCompact 是强降智信号")
    r5, hook5 = fresh()
    tr5 = mk_transcript(r5, 500)
    before = jload(sup(r5, "status", "--json")[1])
    call_hook(hook5, payload(r5, "PreCompact", tr5))
    after = jload(sup(r5, "status", "--json")[1])
    check("compactions 计数 +1",
          (after.get("counters", {}).get("compactions") or 0) == (before.get("counters", {}).get("compactions") or 0) + 1,
          f"before={before.get('counters',{}).get('compactions')} after={after.get('counters',{}).get('compactions')}")
    check("1 次压缩即 DEGRADED", after.get("level") == "DEGRADED",
          f"level={after.get('level')} score={after.get('score')}")

    banner("6. 2 次压缩 -> BLOCKED")
    call_hook(hook5, payload(r5, "PreCompact", tr5))
    after2 = jload(sup(r5, "status", "--json")[1])
    check("2 次压缩 -> BLOCKED", after2.get("level") == "BLOCKED",
          f"level={after2.get('level')} score={after2.get('score')}")

    banner("7. 缺 transcript_path 不能崩")
    r7, hook7 = fresh()
    code, out, err = call_hook(hook7, payload(r7, "UserPromptSubmit", None))
    check("无 transcript 仍正常", code == 0 and out.strip() == "{}", f"code={code} out={out[:120]}")

    banner("8. transcript 路径不存在不能崩")
    code, out, err = call_hook(hook7, payload(r7, "UserPromptSubmit", "C:/nope/none.jsonl"))
    check("坏路径仍正常", code == 0 and out.strip() == "{}", f"code={code} out={out[:120]}")

    banner("9. 垃圾输入 / 空输入不能崩")
    code, out, _ = call_hook(hook7, "not json")
    check("垃圾输入 -> {}", code == 0 and out.strip() == "{}", f"code={code} out={out[:120]}")
    code, out, _ = call_hook(hook7, "")
    check("空输入 -> {}", code == 0 and out.strip() == "{}", f"code={code} out={out[:120]}")

    banner("10. supervisor 不可达时不能砖掉提交")
    d = Path(tempfile.mkdtemp(prefix="sess_test_"))
    shutil.copy2(SRC / "hook_session.ps1", d / "hook_session.ps1")
    (d / "hook_config.json").write_text(json.dumps({
        "supervisor_home": str(d / "gone"), "python": PY}), encoding="utf-8")
    code, out, _ = call_hook(d / "hook_session.ps1", payload(d, "UserPromptSubmit", tr))
    check("不可达时放行", code == 0 and out.strip() == "{}", f"code={code} out={out[:120]}")
    check("已记录日志", (d / "logs" / "session.log").exists())

    banner("11. Stop 事件：宿主侧\"本轮结束\"证据（2026-10-06 接线）")
    r11, hook11 = fresh()
    stop1 = json.dumps({
        "hook_event_name": "Stop", "session_id": "testsess", "cwd": str(r11),
        "turn_id": "t9", "model": "test", "permission_mode": "default",
        "stop_hook_active": False, "last_assistant_message": "done: deliverable X",
        "transcript_path": mk_transcript(r11, 400)}, ensure_ascii=False)
    code, out, err = call_hook(hook11, stop1)
    check("Stop 退出码 0 且透明", code == 0 and out.strip() == "{}",
          f"code={code} out={out[:120]} err={err[:120]}")
    c11 = jload(sup(r11, "status", "--json")[1]).get("counters", {}) or {}
    check("记录了宿主侧 turn_end", c11.get("last_turn_end_ts") is not None, str(c11))
    check("记录了末条消息存在与长度",
          c11.get("last_stop_had_message") is True and (c11.get("last_stop_msg_len") or 0) > 0, str(c11))
    check("stop_hook_active=False 已记录", c11.get("last_stop_hook_active") is False, str(c11))
    stop2 = json.dumps({
        "hook_event_name": "Stop", "session_id": "testsess", "cwd": str(r11),
        "turn_id": "t10", "model": "test", "permission_mode": "default",
        "stop_hook_active": True}, ensure_ascii=False)
    call_hook(hook11, stop2)
    c11b = jload(sup(r11, "status", "--json")[1]).get("counters", {}) or {}
    check("无末条消息时 has_message=False", c11b.get("last_stop_had_message") is False, str(c11b))
    check("stop_hook_active=True 已记录", c11b.get("last_stop_hook_active") is True, str(c11b))

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
