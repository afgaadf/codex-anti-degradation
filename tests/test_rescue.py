# -*- coding: utf-8 -*-
"""紧急恢复（rescue）的客观验证 —— 覆盖"搞砸了也收得了场"的坏状态。

每个场景用独立临时副本，互不污染。
"""
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
FAILURES = []


def check(name, cond, detail=""):
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  :: " + str(detail)[:160]) if (detail and not cond) else ""))
    if not cond:
        FAILURES.append(name)


def run(root, *args, event=None, env=None):
    r = subprocess.run([PY, str(root / "supervisor.py"), *args],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       input=event, cwd=str(root), env=env)
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def jload(text):
    try:
        return json.loads(text)
    except Exception:
        return {}


def copy_root():
    d = tempfile.mkdtemp(prefix="rescue-test-")
    root = Path(d) / "anti"
    shutil.copytree(SRC, root, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "backup"))
    st = root / "state"
    st.mkdir(parents=True, exist_ok=True)
    for n in ("session.json", "intervention.json", "maintenance.json"):
        fp = st / n
        if fp.exists():
            fp.unlink()
    run(root, "init")
    return root


def state(root, name):
    return root / "state" / name


def main():
    # ---- 1) 正常状态下 rescue 幂等 ----
    rm = copy_root()
    c1, o1, _ = run(rm, "rescue", "--json")
    c2, o2, _ = run(rm, "rescue", "--json")
    check("正常状态下 rescue 成功且幂等", c1 == 0 and c2 == 0
          and jload(o1).get("ok") is True and jload(o2).get("ok") is True, o1[:160])

    # ---- 2) session.json 损坏 ----
    rm = copy_root()
    state(rm, "session.json").write_text("{ 这不是 JSON", encoding="utf-8")
    code, out, _ = run(rm, "rescue")
    rcode, rout, _ = run(rm, "status", "--json")
    check("session.json 损坏 → rescue 能救回", code == 0 and rcode in (0, 3) and "NORMAL" in rout.upper(), out[:160])
    archived = list((rm / "state").glob("_rescue_*/*.json"))
    check("损坏文件被归档（不是直接丢）", len(archived) >= 1, [str(x) for x in archived])

    # ---- 3) BLOCKED + intervention 损坏 ----
    rm = copy_root()
    st = state(rm, "session.json")
    s = json.loads(st.read_text(encoding="utf-8"))
    s.update({"turns": 999, "context_chars": 10 ** 9, "images_in_context": 99,
              "unverified_claims": 50, "claims_without_evidence": 50, "tool_errors": 50,
              "unregistered_failures": 50, "rework": 50, "compactions": 50})
    st.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")
    c0, o0, _ = run(rm, "observe", event=json.dumps({"kind": "turn"}))
    blocked_before = (jload(o0).get("level") == "BLOCKED")
    state(rm, "intervention.json").write_text("]]坏掉的[", encoding="utf-8")
    code, out, _ = run(rm, "rescue")
    c1, o1, _ = run(rm, "observe", event=json.dumps({"kind": "turn"}))
    check("极端会话先被判 BLOCKED（前置条件成立）", blocked_before, o0[:160])
    check("BLOCKED + 干预文件损坏 → rescue 后不再拒绝", code == 0 and jload(o1).get("decision") == "allow", out[:160])

    # ---- 4) maintenance.json 损坏 ----
    rm = copy_root()
    state(rm, "maintenance.json").write_text("不是 json", encoding="utf-8")
    code, out, _ = run(rm, "rescue")
    m = jload(run(rm, "maintenance", "status", "--json")[1]).get("maintenance") or {}
    check("maintenance.json 损坏 → rescue 后状态可读且未开启", code == 0 and m.get("active") is False, out[:160])

    # ---- 5) 在 Codex 环境里也能用（紧急通道不受外部限制） ----
    rm = copy_root()
    env = os.environ.copy()
    env["CODEX_SESSION_ID"] = "sim"
    env["CODEX_SHELL"] = "1"
    code, out, _ = run(rm, "rescue", env=env)
    check("紧急恢复在 Codex 环境内依然可用", code == 0 and "恢复可用" in out, out[:160])
    # 对照：维护模式 on 在同样环境里必须被拒
    code2, out2, _ = run(rm, "maintenance", "on", "--by", "x", "--reason", "y", "--minutes", "1", env=env)
    check("对照：维护模式 on 在 Codex 内被拒（限制仍生效）", code2 == 2, out2[:160])

    # ---- 6) rescue 写了审计 ----
    rm = copy_root()
    run(rm, "rescue", "--by", "审计测试")
    audit = rm / "state" / "audit.jsonl"
    has = audit.exists() and "rescue" in audit.read_text(encoding="utf-8", errors="replace")
    check("rescue 写入审计（actor=rescue）", has)

    print()
    print("=" * 68)
    if FAILURES:
        print("结果: %d 项未通过" % len(FAILURES))
        for f in FAILURES:
            print("  - " + f)
        return 1
    print("结果: 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())