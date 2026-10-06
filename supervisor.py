#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""anti-degradation supervisor

设计原则（硬性）：
  1. 生产权与监督权分离 —— 本进程独占 state/ rules/ checks/ ledger/ 的写权限。
     生产者（Codex agent）只能 observe / gate / check / debt add，
     不能改规则、不能自证通过、不能解除阻断。
  2. 失败方向正确 —— 任何异常都朝 BLOCK 走，绝不朝"放行"走。
  3. 规则不可自改 —— rules.json 哈希锁定；被静默修改 => 立即 BLOCKED（硬阻断）。
  4. 分数可恢复 —— 问题被真正解决（检查通过）后，对应扣分必须清除。
     只会往上涨的监督器，最后一定会误杀正常的长任务。
  5. 阻断必须留痕 —— 解除只能靠人；强行绕过写入 break_glass 永久审计。
  6. 维护模式只可外部限时开启 —— 不常驻、不做页面内设置；过期自动恢复阻断，启停全程审计。

退出码：0=ALLOW, 2=DENY, 3=ERROR(保守阻断)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
RULES_DIR = ROOT / "rules"
RULES_FILE = RULES_DIR / "rules.json"
RULES_HASH = RULES_DIR / "rules.sha256"
APPROVALS = RULES_DIR / "approvals"
CHECKS_DIR = ROOT / "checks"
LEDGER_DIR = ROOT / "ledger"
LEDGER = LEDGER_DIR / "debt.md"

EVENTS = STATE / "events.jsonl"
SESSION = STATE / "session.json"
AUDIT = STATE / "audit.jsonl"
INTERVENTION = STATE / "intervention.json"
MAINTENANCE = STATE / "maintenance.json"
CHECK_RECORDS = STATE / "check_records.json"
CHECK_RUNS = STATE / "check_runs"
BREAK_GLASS = STATE / "break_glass.jsonl"
SAMPLES = STATE / "samples.jsonl"
CALIBRATION = STATE / "calibration.json"
HANDOFF_DIR = STATE / "handoffs"

DEFAULT_RULES = {
    "version": 1,
    "note": "监督阈值。改动必须由人通过 rules approve 批准，否则监督层硬阻断。",
    "thresholds": {
        "turns_watch": 30, "turns_degraded": 60, "turns_blocked": 100,
        "context_chars_watch": 120000, "context_chars_degraded": 250000,
        "context_chars_blocked": 400000,
        "images_watch": 3, "images_degraded": 6,
        "elapsed_min_watch": 30, "elapsed_min_degraded": 60,
        "repeat_command_limit": 3,
        "compactions_watch": 1, "compactions_blocked": 2,
        "tool_errors_watch": 5, "tool_errors_degraded": 10, "tool_errors_blocked": 20,
        "rework_limit": 3, "rework_watch": 2, "rework_degraded": 4, "rework_blocked": 8,
        "unfinished_watch": 2, "unfinished_degraded": 4,
        "subagents_watch": 3, "subagents_degraded": 6,
        # 盲区信号只做"可见+提醒"：拦生产者无法恢复可观测性，故永不单独 BLOCK
        "parse_failures_watch": 2, "parse_failures_degraded": 10, "parse_failures_blocked": None,
    },
    # 分档系数：watch=x1, degraded=x3, blocked=x6
    "tier_multiplier": {"watch": 1, "degraded": 3, "blocked": 6},
    "weights": {
        "turns": 3, "context_chars": 3, "images": 3, "elapsed": 3,
        "unverified_claim": 5, "claim_without_evidence": 2,
        "unregistered_failure": 4, "repeat_command": 3, "critical_debt": 3,
        "compaction": 8,
        "tool_error": 3, "rework": 3, "unfinished": 3, "subagents": 3,
        "parse_failure": 1,
    },
    "levels": {"watch": 3, "degraded": 8, "blocked": 14},
    # 这些条件不看分数，直接硬阻断
    "hard_block_on": ["rules_integrity"],
}


# ---------------------------------------------------------------- utilities

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except Exception:
        return default


def read_json_retry(path: Path, default=None, attempts: int = 5, base_delay: float = 0.03):
    """读 JSON，抗“瞬时失败”：文件正被原子替换 / 被杀软或占用方短暂锁住。

    2026-10-06：rules approve 会重写 rules.json 与 rules.sha256；并发的 observe
    若恰好落在替换窗口里，会把一次正常批准误判成“规则缺失/被篡改”而硬阻断。
    这里做退避重试，只有全部尝试都失败才返回 default。
    """
    attempts = max(1, attempts)
    for i in range(attempts):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            if i < attempts - 1:
                time.sleep(base_delay * (i + 1))
    return default


def _json_text(obj, indent=None) -> str:
    """json.dumps 的稳健封装（落盘永不因字符串内容而失败）。

    若字符串里混入孤立代理项（lone surrogate，例如上游按错编码解码留下的
    高代理/低代理字符），json.dumps(ensure_ascii=False) 的结果再按 UTF-8 写盘
    会抛 UnicodeEncodeError —— 那会让一次 observe 直接崩成 score=999 硬拒绝
    （2026-10-06 实际发生过一次）。这里把不可编码字符替换掉，保证日志能落盘。
    """
    txt = json.dumps(obj, ensure_ascii=False, indent=indent)
    return txt.encode("utf-8", "replace").decode("utf-8")


def atomic_write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(_json_text(obj, indent=2))
    os.replace(tmp, path)


def append_jsonl(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(_json_text(obj) + "\n")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def audit(event_type: str, **fields) -> None:
    rec = {"ts": now_iso(), "kind": event_type}
    rec.update(fields)
    append_jsonl(AUDIT, rec)


# ---------------------------------------------------------------- rules

def ensure_layout() -> None:
    for d in (STATE, RULES_DIR, APPROVALS, CHECKS_DIR, LEDGER_DIR, CHECK_RUNS):
        d.mkdir(parents=True, exist_ok=True)
    if not RULES_FILE.exists():
        atomic_write_json(RULES_FILE, DEFAULT_RULES)
    if not LEDGER.exists():
        LEDGER.write_text("# 欠账登记\n\n结项条件：open 项清零。\n\n"
                          "| ID | 欠账内容 | 严重度 | 交付物 | 状态 |\n|---|---|---|---|---|\n",
                          encoding="utf-8")


def load_rules():
    ensure_layout()
    rules = read_json_retry(RULES_FILE)
    if not isinstance(rules, dict):
        return None, "rules.json 缺失或损坏"
    return rules, None


def rules_integrity():
    if not RULES_FILE.exists():
        return False, "rules.json 不存在"
    actual = sha256_file(RULES_FILE)
    if not RULES_HASH.exists():
        return False, "rules.sha256 缺失（规则未冻结）"
    try:
        recorded = RULES_HASH.read_text(encoding="utf-8").strip()
    except Exception:
        recorded = ""
    # 抗瞬时失败：freeze_rules 重写 rules.sha256 的瞬间可能被读到中间态，
    # 短暂退避重读，避免把一次正常批准误判成“静默篡改”而硬阻断。
    for _ in range(4):
        if actual == recorded:
            break
        time.sleep(0.05)
        try:
            recorded = RULES_HASH.read_text(encoding="utf-8").strip()
        except Exception:
            continue
    if actual == recorded:
        return True, "规则完整"
    for f in APPROVALS.glob("*.json"):
        rec = read_json(f, {}) or {}
        if rec.get("new_hash") == actual and rec.get("approved_by"):
            return True, "规则已变更并获批准"
    return False, "规则哈希不匹配且无批准（疑似静默修改）"


def freeze_rules() -> str:
    ensure_layout()
    digest = sha256_file(RULES_FILE)
    # 原子写 hash：非原子写会让并发 observe 读到空/半截的 hash，
    # 从而误判“规则被静默篡改”并硬阻断一次工具调用。
    tmp = RULES_HASH.with_suffix(RULES_HASH.suffix + ".tmp")
    tmp.write_text(digest, encoding="utf-8")
    os.replace(tmp, RULES_HASH)
    try:
        subprocess.run(["attrib", "+R", str(RULES_FILE)], capture_output=True)
    except Exception:
        pass
    return digest


# ---------------------------------------------------------------- state

def new_session(sid=None) -> dict:
    return {
        "session_id": sid or uuid.uuid4().hex[:12],
        "started_ts": now_iso(),
        "started_epoch": time.time(),
        "turns": 0,
        "context_chars": 0,
        "images_in_context": 0,
        "commands": {},
        "read_commands": {},
        "claim_state": {},
        "compactions": 0,
        "unregistered_failures": 0,
        "tool_errors": 0,
        "parse_failures": 0,
        "codex_session_id": None,
        "touches": {},
        "subagents_active": 0,
        "subagents_started": 0,
        "deliverables": {},
        "sessions_seen": 0,
        "last_session_start_ts": None,
        "last_session_source": None,
        "tool_response_shape": None,
        "tool_response_judgeable": None,
        "last_turn_end_ts": None,
        "last_stop_hook_active": None,
        "last_stop_had_message": None,
        "last_stop_msg_len": 0,
        "last_updated": now_iso(),
    }


def load_session() -> dict:
    ensure_layout()
    s = read_json(SESSION)
    if not isinstance(s, dict):
        s = new_session()
        atomic_write_json(SESSION, s)
    s.setdefault("claim_state", {})
    s.setdefault("commands", {})
    s.setdefault("read_commands", {})
    s.setdefault("parse_failures", 0)
    s.setdefault("codex_session_id", None)
    # 旧状态文件缺这两个字段时补齐（2026-10-06 新增：线程接入登记）
    s.setdefault("sessions_seen", 0)
    s.setdefault("last_session_start_ts", None)
    s.setdefault("last_session_source", None)
    s.setdefault("tool_response_shape", None)
    s.setdefault("tool_response_judgeable", None)
    s.setdefault("last_turn_end_ts", None)
    s.setdefault("last_stop_hook_active", None)
    s.setdefault("last_stop_had_message", None)
    s.setdefault("last_stop_msg_len", 0)
    return s


def save_session(s: dict) -> None:
    s["last_updated"] = now_iso()
    atomic_write_json(SESSION, s)


def claim_totals(s: dict):
    unverified = 0
    no_evidence = 0
    for rec in (s.get("claim_state") or {}).values():
        unverified += rec.get("unverified", 0)
        no_evidence += rec.get("no_evidence", 0)
    return unverified, no_evidence


def rework_count(s: dict, limit: int = 3) -> int:
    """同一条路径被反复触碰 >= limit 次，算作一次返工。"""
    return sum(1 for _p, n in (s.get("touches") or {}).items() if n >= limit)


def unfinished_count(s: dict) -> int:
    """声明完成却拿不出证据的交付物个数（漏项）。"""
    return sum(1 for rec in (s.get("claim_state") or {}).values()
               if rec.get("unverified", 0) > 0)


def sample_metrics(s: dict) -> dict:
    """一次会话快照，供 calibrate 做分布统计。"""
    return {
        "turns": s.get("turns", 0),
        "context_chars": s.get("context_chars", 0),
        "images": s.get("images_in_context", 0),
        "compactions": s.get("compactions", 0),
        "tool_errors": s.get("tool_errors", 0),
        "rework": rework_count(s),
        "unfinished": unfinished_count(s),
        "subagents_active": s.get("subagents_active", 0),
        "parse_failures": s.get("parse_failures", 0),
        "elapsed_min": round((time.time() - s.get("started_epoch", time.time())) / 60.0, 2),
    }


def percentile(values, pct):
    """最近秩法（nearest-rank）。values 为空返回 None。"""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    import math
    k = max(1, math.ceil(pct / 100.0 * len(vals)))
    return vals[min(k, len(vals)) - 1]


def write_handoff(s: dict, level: str = "") -> str:
    """把人机交接单写成文件（降智介入时自动产出）。"""
    ensure_layout()
    HANDOFF_DIR.mkdir(parents=True, exist_ok=True)
    sid = s.get("session_id", "unknown")
    path = HANDOFF_DIR / f"handoff-{sid}.md"
    unverified, no_evidence = claim_totals(s)
    debt_items = [d for d in parse_debt() if d.get("status") == "open"]
    lines = []
    lines.append(f"# 交接单 (session {sid})")
    lines.append("")
    lines.append(f"- 生成时间: {now_iso()}")
    lines.append(f"- 触发等级: {level or '(手工生成)'}")
    lines.append(f"- 会话时长: {round((time.time() - s.get('started_epoch', time.time())) / 60.0, 1)} 分钟")
    lines.append(f"- 轮数: {s.get('turns', 0)}   上下文: {s.get('context_chars', 0)} 字符")
    lines.append("")
    lines.append("## 机器可读计数")
    lines.append("")
    for k, v in sample_metrics(s).items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("## 未决交付（声明完成但无通过检查）")
    lines.append("")
    if unverified or no_evidence:
        for name, rec in (s.get("claim_state") or {}).items():
            lines.append(f"- {name}: unverified={rec.get('unverified', 0)} no_evidence={rec.get('no_evidence', 0)}")
    else:
        lines.append("- 无")
    lines.append("")
    lines.append("## 未决欠账")
    lines.append("")
    if debt_items:
        for d in debt_items:
            lines.append(f"- [{d.get('severity')}] {d.get('text')}")
    else:
        lines.append("- 无")
    lines.append("")
    lines.append("## 人需要填写的部分（监督层无法代劳）")
    lines.append("")
    lines.append("- 目标：")
    lines.append("- 已完成：")
    lines.append("- 未完成：")
    lines.append("- 下一步：")
    lines.append("- 卡点：")
    lines.append("- 是否建议换线程继续：")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    audit("handoff_written", level=level, path=str(path))
    return str(path)


# ---------------------------------------------------------------- scoring

def score_session(s: dict, rules: dict):
    th = rules.get("thresholds", {})
    w = rules.get("weights", {})
    lv = rules.get("levels", {})
    tm = rules.get("tier_multiplier", {"watch": 1, "degraded": 3, "blocked": 6})

    score = 0
    reasons = []

    def tier(value, watch, degraded, blocked, weight, label):
        nonlocal score
        if blocked is not None and value >= blocked:
            pts = weight * tm.get("blocked", 6)
            score += pts
            reasons.append(f"{label}={value} >= {blocked} (+{pts})")
        elif degraded is not None and value >= degraded:
            pts = weight * tm.get("degraded", 3)
            score += pts
            reasons.append(f"{label}={value} >= {degraded} (+{pts})")
        elif value >= watch:
            pts = weight * tm.get("watch", 1)
            score += pts
            reasons.append(f"{label}={value} >= {watch} (+{pts})")

    tier(s.get("turns", 0), th.get("turns_watch", 30), th.get("turns_degraded", 60),
         th.get("turns_blocked", 100), w.get("turns", 3), "turns")
    tier(s.get("context_chars", 0), th.get("context_chars_watch", 120000),
         th.get("context_chars_degraded", 250000), th.get("context_chars_blocked", 400000),
         w.get("context_chars", 3), "context_chars")
    tier(s.get("images_in_context", 0), th.get("images_watch", 3),
         th.get("images_degraded", 6), None, w.get("images", 3), "images")
    elapsed_min = (time.time() - s.get("started_epoch", time.time())) / 60.0
    tier(elapsed_min, th.get("elapsed_min_watch", 30), th.get("elapsed_min_degraded", 60),
         None, w.get("elapsed", 3), "elapsed_min")

    unverified, no_evidence = claim_totals(s)
    if unverified:
        pts = w.get("unverified_claim", 5) * unverified
        score += pts
        reasons.append(f"unverified_claims={unverified} (+{pts})")
    if no_evidence:
        pts = w.get("claim_without_evidence", 2) * no_evidence
        score += pts
        reasons.append(f"claims_without_evidence={no_evidence} (+{pts})")

    fails = s.get("unregistered_failures", 0)
    if fails:
        pts = w.get("unregistered_failure", 4) * fails
        score += pts
        reasons.append(f"unregistered_failures={fails} (+{pts})")

    # 压缩发生过 => 上下文曾经被填满，是最硬的降智信号之一。
    # 走 tier()，让 rules.json 的 compactions_watch/blocked 真正生效（原来是死配置）。
    tier(s.get("compactions", 0), th.get("compactions_watch", 1), None,
         th.get("compactions_blocked", 2), w.get("compaction", 8), "compactions")

    # 工具报错总数（区别于 unregistered_failures：这里统计所有报错）
    tier(s.get("tool_errors", 0), th.get("tool_errors_watch", 5),
         th.get("tool_errors_degraded", 10), th.get("tool_errors_blocked", 20),
         w.get("tool_error", 3), "tool_errors")

    # hook stdin 解析失败 = 监督层根本没看见这条事件。
    # 这种盲区以前完全静默（等于自己不知道有个洞），必须计分。
    tier(s.get("parse_failures", 0), th.get("parse_failures_watch", 2),
         th.get("parse_failures_degraded", 10), th.get("parse_failures_blocked"),
         w.get("parse_failure", 1), "parse_failures")

    # 返工：同一条路径被反复触碰
    tier(rework_count(s, th.get("rework_limit", 3)), th.get("rework_watch", 2),
         th.get("rework_degraded", 4), th.get("rework_blocked", 8),
         w.get("rework", 3), "rework")

    # 漏项：声明完成却拿不出证据的交付物
    tier(unfinished_count(s), th.get("unfinished_watch", 2),
         th.get("unfinished_degraded", 4), None, w.get("unfinished", 3), "unfinished")

    # 子代理并发（原来是监控盲区）
    tier(s.get("subagents_active", 0), th.get("subagents_watch", 3),
         th.get("subagents_degraded", 6), None, w.get("subagents", 3), "subagents_active")

    limit = th.get("repeat_command_limit", 3)
    repeats = sum(1 for c, n in (s.get("commands") or {}).items() if n >= limit)
    if repeats:
        pts = w.get("repeat_command", 3) * repeats
        score += pts
        reasons.append(f"repeat_commands={repeats} (+{pts})")

    debt = count_open_debt(critical_only=True)
    if debt:
        pts = w.get("critical_debt", 3) * debt
        score += pts
        reasons.append(f"critical_debt={debt} (+{pts})")

    ok, detail = rules_integrity()
    hard = [] if ok else ["rules_integrity"]

    if score >= lv.get("blocked", 14):
        level = "BLOCKED"
    elif score >= lv.get("degraded", 8):
        level = "DEGRADED"
    elif score >= lv.get("watch", 4):
        level = "WATCH"
    else:
        level = "NORMAL"

    if hard:
        level = "BLOCKED"
        reasons.append(f"HARD BLOCK: {detail}")

    return score, level, reasons, ok, detail


# ---------------------------------------------------------------- ledger

def parse_debt() -> list:
    if not LEDGER.exists():
        return []
    items = []
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("|---") or "严重度" in line:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 5 or not cells[0].isdigit():
            continue
        items.append({"id": cells[0], "text": cells[1], "severity": cells[2].lower(),
                      "deliverable": cells[3], "status": cells[4].lower()})
    return items


def count_open_debt(critical_only=False) -> int:
    n = 0
    for it in parse_debt():
        if it.get("status") != "open":
            continue
        if critical_only and it.get("severity") != "critical":
            continue
        n += 1
    return n


def rewrite_ledger(items) -> None:
    header = ("# 欠账登记\n\n结项条件：open 项清零。\n\n"
              "| ID | 欠账内容 | 严重度 | 交付物 | 状态 |\n|---|---|---|---|---|\n")
    body = "".join(
        f"| {i['id']} | {i['text']} | {i['severity']} | {i['deliverable']} | {i['status']} |\n"
        for i in items
    )
    LEDGER.write_text(header + body, encoding="utf-8")


def add_debt(text, severity, deliverable) -> str:
    ensure_layout()
    items = parse_debt()
    next_id = max([int(i["id"]) for i in items] or [0]) + 1
    items.append({"id": str(next_id), "text": text or "-", "severity": severity,
                  "deliverable": deliverable or "-", "status": "open"})
    rewrite_ledger(items)
    audit("debt_add", id=next_id, text=text, severity=severity, deliverable=deliverable)
    return str(next_id)


def clear_debt(debt_id, evidence) -> bool:
    items = parse_debt()
    hit = False
    for it in items:
        if it["id"] == str(debt_id) and it["status"] == "open":
            it["status"] = "closed"
            hit = True
    if hit:
        rewrite_ledger(items)
        audit("debt_clear", id=debt_id, evidence=evidence)
    return hit


# ---------------------------------------------------------------- checks

def run_check(name: str, deliverable: str | None = None):
    """仅由监督层执行。返回 (passed, record_or_detail)。"""
    ensure_layout()
    spec = read_json(CHECKS_DIR / f"{name}.json")
    if not isinstance(spec, dict):
        return False, f"检查定义不存在: checks/{name}.json"

    steps = spec.get("steps") or []
    if not steps:
        return False, "检查定义没有任何步骤（禁止空检查通过）"

    target = deliverable or spec.get("deliverable") or name
    results = []
    passed = True
    for idx, step in enumerate(steps):
        cmd = step.get("cmd")
        if not cmd:
            results.append({"step": idx, "ok": False, "error": "missing cmd"})
            passed = False
            break
        expect = step.get("expect_exit", 0)
        t0 = time.time()
        try:
            proc = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                                  timeout=step.get("timeout", 600))
            code = proc.returncode
            ok = (code == expect)
            out = (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired:
            code, ok, out = -1, False, "TIMEOUT"
        except Exception as exc:
            code, ok, out = -1, False, f"EXEC_ERROR: {exc}"
        results.append({"step": idx, "cmd": cmd, "exit": code, "ok": ok,
                        "seconds": round(time.time() - t0, 2), "output_tail": out[-2000:]})
        if not ok:
            passed = False
            if not step.get("continue_on_fail"):
                break

    record = {"name": name, "deliverable": target, "passed": passed,
              "ts": now_iso(), "results": results}
    CHECK_RUNS.mkdir(parents=True, exist_ok=True)
    append_jsonl(CHECK_RUNS / "history.jsonl", record)

    recs = read_json(CHECK_RECORDS, {}) or {}
    if passed:
        recs[target] = {"passed": True, "ts": record["ts"], "check": name}
    else:
        recs.pop(target, None)
    atomic_write_json(CHECK_RECORDS, recs)

    # 检查通过 => 该交付物的未验证声明视为已解决，扣分清除
    s = load_session()
    if passed and target in (s.get("claim_state") or {}):
        s["claim_state"].pop(target, None)
        save_session(s)

    audit("check_run", name=name, deliverable=target, passed=passed)
    return passed, record


def has_passing_check(deliverable: str) -> bool:
    recs = read_json(CHECK_RECORDS, {}) or {}
    return bool((recs.get(deliverable) or {}).get("passed"))


# ---------------------------------------------------------------- intervention

def write_intervention(level, score, reasons):
    prev = read_intervention()
    obj = {
        "ts": now_iso(),
        "level": level,
        "score": score,
        "reasons": reasons,
        "active": level in ("DEGRADED", "BLOCKED"),
    }
    # blocked_at 是"最近一次进入阻断"的时刻，resume 时不得覆盖
    if level == "BLOCKED":
        obj["blocked_at"] = obj["ts"]
    elif prev.get("blocked_at"):
        obj["blocked_at"] = prev["blocked_at"]

    if level == "DEGRADED":
        obj["directive"] = (
            "SUPERVISOR 已介入（DEGRADED）。逐条执行并回读：\n"
            "1) 停止新增任务，不要扩大范围。\n"
            "2) 打开自动生成的交接单（见下方路径），补全「人需要填写的部分」。\n"
            "3) 读回交接单，确认无遗漏。\n"
            "4) 未重新锚定前，禁止推进任何实质步骤。\n"
            "5) 若原因是上下文被压缩过：不要相信自己对早期内容的记忆，回到文件重新读。\n"
            "6) 若分数主要来自上下文/轮数：结论应是「换新线程继续」，而不是在本会话硬撑。"
        )
    elif level == "BLOCKED":
        obj["directive"] = (
            "SUPERVISOR 已阻断（BLOCKED）。继续需要人在场：\n"
            "1) 立即写交接单并停止写入型操作。\n"
            "2) 由人执行 resume --by <人> --reason <理由> 才能恢复。\n"
            "3) 强行绕过会写入 break_glass 永久审计。"
        )
    else:
        obj["directive"] = None
        obj["active"] = False
        obj.pop("blocked_at", None)
    # ⑤ 进入介入态时自动产出人机交接单（只在等级变化那一刻写，避免刷屏）
    if level in ("DEGRADED", "BLOCKED") and prev.get("level") != level:
        try:
            hp = write_handoff(load_session(), level=level)
            obj["handoff_path"] = hp
            obj["directive"] = (obj.get("directive") or "") + "\n交接单已生成: " + hp
        except Exception as exc:
            audit("handoff_failed", error=repr(exc))

    atomic_write_json(INTERVENTION, obj)
    return obj


def read_intervention():
    return read_json(INTERVENTION, {}) or {}


# ---------------------------------------------------------------- maintenance (external only)
def _inside_codex():
    """Producer-side detection: maintenance must be toggled from an external shell."""
    return bool(os.environ.get("CODEX_SESSION_ID") or os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SHELL"))


def _parse_iso(ts):
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _friendly_ts(ts):
    dt = _parse_iso(ts)
    return dt.astimezone().isoformat(timespec="minutes") if dt else str(ts or "未设置")


def maintenance_state():
    """Read the external, time-limited maintenance switch; never starts a process."""
    obj = dict(read_json(MAINTENANCE, {}) or {})
    now = datetime.now(timezone.utc)
    until = _parse_iso(obj.get("until"))
    active = bool(obj.get("active"))
    if active and until is not None:
        active = until > now
        obj["remaining_min"] = round(max(0.0, (until - now).total_seconds()) / 60.0, 1)
    else:
        obj["remaining_min"] = 0
    if active and until is None:
        active = False
    obj["active"] = active
    return obj


def write_maintenance(active, by="", reason="", minutes=0):
    now = datetime.now(timezone.utc)
    obj = {"ts": now_iso(), "active": bool(active), "by": str(by or ""), "reason": str(reason or "")}
    if active:
        minutes = int(minutes)
        obj["minutes"] = minutes
        obj["until"] = (now + timedelta(minutes=minutes)).isoformat(timespec="seconds")
    atomic_write_json(MAINTENANCE, obj)
    return obj


def maintenance_text(m):
    if not m or not m.get("active"):
        return ""
    return ("[维护模式] 开发/维护临时放行，至 %s；由 %s 启动。原因：%s。"
            " 仅外部命令可启停，不常驻。" % (_friendly_ts(m.get("until")),
                                         m.get("by") or "未署名",
                                         m.get("reason") or "未说明"))


# ---------------------------------------------------------------- commands

def cmd_init(args):
    ensure_layout()
    digest = freeze_rules()
    s = new_session()
    atomic_write_json(SESSION, s)
    atomic_write_json(INTERVENTION, {"ts": now_iso(), "level": "NORMAL", "active": False, "directive": None})
    audit("init", session=s["session_id"])
    print(json.dumps({"ok": True, "session": s["session_id"], "rules_hash": digest}, ensure_ascii=False))
    return 0


def cmd_observe(args):
    ensure_layout()
    try:
        raw = args.event if args.event else sys.stdin.read()
        raw = str(raw).lstrip("\ufeff")
        ev = json.loads(raw) if raw.strip() else {}
    except Exception as exc:
        audit("observe_parse_error", error=str(exc))
        print(json.dumps({"decision": "allow", "level": "NORMAL", "score": 0,
                          "note": "事件无法解析，监督状态未变"}, ensure_ascii=False))
        return 0

    rules, err = load_rules()
    if rules is None:
        audit("rules_missing", error=err)
        print(json.dumps({"decision": "deny", "level": "BLOCKED", "score": 999,
                          "reason": f"监督层异常：{err}"}, ensure_ascii=False))
        return 2

    s = load_session()
    kind = ev.get("kind", "unknown")
    _cx = (ev.get("session_id") or "").strip()
    if _cx:
        s["codex_session_id"] = _cx

    if kind == "turn":
        s["turns"] = s.get("turns", 0) + 1
    elif kind == "context":
        s["context_chars"] = ev.get("chars", s.get("context_chars", 0))
    elif kind == "image":
        s["images_in_context"] = s.get("images_in_context", 0) + 1
    elif kind == "command":
        cmd = (ev.get("cmd") or "").strip()
        if cmd:
            # repeat_command 只统计"会改盘"的命令（与 rework 同口径）。
            # hook 对纯只读调查命令附 readonly=true（2026-10-06 第十四轮修）：
            # 反复跑同一条只读命令属正常调查，不计入 repeat_command；
            # 但仍落进 read_commands 单独记账，保持"可见"（不静默）。
            if ev.get("readonly") is True:
                rcmds = s.setdefault("read_commands", {})
                rcmds[cmd] = rcmds.get(cmd, 0) + 1
            else:
                cmds = s.setdefault("commands", {})
                cmds[cmd] = cmds.get(cmd, 0) + 1
        # rework: 只统计"对同一路径的重复写入/修改"。
        # hook 只对会改盘的命令附 path（rework_kind='edit'）；只读调查命令不附 path，
        # 或显式标 rework_kind='read'。反复读同一文件属于正常调查，不算返工。
        # 2026-10-06 修：旧口径把 Get-Content/Get-ChildItem/Select-String 也算返工，
        # 本会话因此被误判两次 DEGRADED、一次 BLOCKED。
        tgt = (ev.get("path") or "").strip()
        if tgt and (ev.get("rework_kind") or "edit") != "read":
            touches = s.setdefault("touches", {})
            touches[tgt] = touches.get(tgt, 0) + 1
    elif kind == "compact":
        s["compactions"] = s.get("compactions", 0) + 1
    elif kind == "session_start":
        # 计数是跨会话留存的（恢复模型 = 换线程 + resume，见 README 六·六）。
        # 所以这里**不重置**计数窗口锚点 started_epoch/started_ts：
        # elapsed_min 必须与各计数同口径，否则换线程后会出现
        # 「计数继续累积、时长却归零」两个窗口打架 —— 生产侧只要新开线程
        # 就能把时长信号清零，等于给监督层留了个逃生门。
        # 只登记本线程接入，供溯源与审计；窗口锚点仅在 new_session() 重置。
        s["sessions_seen"] = s.get("sessions_seen", 0) + 1
        s["last_session_start_ts"] = now_iso()
        # 官方 source ∈ startup/resume/clear/compact/fork（input schema required）
        if ev.get("source"):
            s["last_session_source"] = str(ev.get("source"))
    elif kind == "tool_error":
        # 统计所有工具报错；未登记为欠账的，另记一份
        s["tool_errors"] = s.get("tool_errors", 0) + 1
        if not ev.get("debt_registered"):
            s["unregistered_failures"] = s.get("unregistered_failures", 0) + 1
    elif kind == "rework":
        path = (ev.get("path") or "").strip()
        if path:
            touches = s.setdefault("touches", {})
            touches[path] = touches.get(path, 0) + 1
    elif kind == "subagent_start":
        s["subagents_active"] = s.get("subagents_active", 0) + 1
        s["subagents_started"] = s.get("subagents_started", 0) + 1
    elif kind == "subagent_stop":
        s["subagents_active"] = max(0, s.get("subagents_active", 0) - 1)
    elif kind == "parse_failure":
        # 盲区计数：原始 stdin 已由 hook 落盘到 logs/stdin-fail-*.txt
        s["parse_failures"] = s.get("parse_failures", 0) + 1
        s["last_parse_failure"] = {"ts": now_iso(), "source": ev.get("source"),
                                   "hint": ev.get("hint")}
    elif kind == "completion_claim":
        target = ev.get("deliverable") or "(unnamed)"
        if has_passing_check(target):
            s.setdefault("deliverables", {})[target] = {"verified": True, "ts": now_iso()}
        else:
            cs = s.setdefault("claim_state", {}).setdefault(target, {"unverified": 0, "no_evidence": 0})
            cs["unverified"] += 1
            if not ev.get("evidence"):
                cs["no_evidence"] += 1
    elif kind == "tool_response_shape":
        # 宿主把 tool_response 发成非结构化值（本机是字符串）时，tool_errors 无法判定。
        # 记下"不可观测"，避免把"看不见"当成"没有错误"（2026-10-06 线上查证后补）。
        s["tool_response_shape"] = str(ev.get("shape") or "unknown")
        s["tool_response_judgeable"] = bool(ev.get("judgeable"))
    elif kind == "turn_end":
        # 宿主侧"本轮结束"证据（2026-10-06 接线官方 Stop 事件）。
        # 只记录，不改计分：把"本轮是否结束 / 是否有末条消息"从生产者自报
        # 升级为宿主侧信号，供后续判断"声明完成却无证据"使用。
        s["last_turn_end_ts"] = now_iso()
        s["last_stop_hook_active"] = bool(ev.get("stop_hook_active"))
        s["last_stop_had_message"] = bool(ev.get("has_message"))
        s["last_stop_msg_len"] = int(ev.get("msg_len") or 0)

    save_session(s)
    append_jsonl(EVENTS, {"ts": now_iso(), "kind": kind, "payload": ev})
    append_jsonl(SAMPLES, {"ts": now_iso(), "sid": s.get("session_id"),
                           "metrics": sample_metrics(s)})

    score, level, reasons, ok, detail = score_session(s, rules)
    if level in ("DEGRADED", "BLOCKED"):
        write_intervention(level, score, reasons)
    else:
        prev = read_intervention()
        if prev.get("active"):
            write_intervention(level, score, reasons)

    maint = maintenance_state()
    if maint.get("active"):
        decision = "allow"
    else:
        decision = "allow" if level != "BLOCKED" else "deny"
    inter = read_intervention()
    directive = inter.get("directive") if level in ("DEGRADED", "BLOCKED") else None
    if maint.get("active"):
        directive = maintenance_text(maint) + (("\n" + directive) if directive else "")
    out = {
        "decision": decision,
        "level": level,
        "score": score,
        "reasons": reasons,
        "integrity_ok": ok,
        "integrity_detail": detail,
        "directive": directive,
        "session": s.get("session_id"),
        "maintenance": maint if maint.get("active") else None,
        "maintenance_active": bool(maint.get("active")),
    }
    audit("observe", event=kind, level=level, score=score, decision=decision,
          maintenance=bool(maint.get("active")))
    print(json.dumps(out, ensure_ascii=False))
    return 0 if decision == "allow" else 2


def cmd_status(args):
    rules, err = load_rules()
    if rules is None:
        print(json.dumps({"ok": False, "error": err}, ensure_ascii=False))
        return 3
    s = load_session()
    score, level, reasons, ok, detail = score_session(s, rules)
    inter = read_intervention()
    maint = maintenance_state()
    unverified, no_evidence = claim_totals(s)
    out = {
        "session": s.get("session_id"),
        "codex_session": s.get("codex_session_id"),
        "level": level,
        "score": score,
        "reasons": reasons,
        "rules_integrity": detail,
        "intervention_active": level in ("DEGRADED", "BLOCKED"),
        "maintenance": maint if maint.get("active") else None,
        "maintenance_active": bool(maint.get("active")),
        "open_debt": count_open_debt(),
        "open_critical_debt": count_open_debt(critical_only=True),
        "counters": {
            "turns": s.get("turns"), "context_chars": s.get("context_chars"),
            "images": s.get("images_in_context"),
            "unverified_claims": unverified,
            "claims_without_evidence": no_evidence,
            "compactions": s.get("compactions"),
            "unregistered_failures": s.get("unregistered_failures"),
            "tool_errors": s.get("tool_errors"),
            "rework": rework_count(s),
            "unfinished": unfinished_count(s),
            "subagents_active": s.get("subagents_active"),
            "subagents_started": s.get("subagents_started"),
            "parse_failures": s.get("parse_failures"),
            "sessions_seen": s.get("sessions_seen", 0),
            "last_session_source": s.get("last_session_source"),
            "tool_response_shape": s.get("tool_response_shape"),
            "tool_response_judgeable": s.get("tool_response_judgeable"),
            "read_commands": len(s.get("read_commands") or {}),
            "last_turn_end_ts": s.get("last_turn_end_ts"),
            "last_stop_hook_active": s.get("last_stop_hook_active"),
            "last_stop_had_message": s.get("last_stop_had_message"),
            "last_stop_msg_len": s.get("last_stop_msg_len"),
        },
        "elapsed_min": round((time.time() - s.get("started_epoch", time.time())) / 60.0, 1),
        "directive": inter.get("directive"),
        "blocked_at": inter.get("blocked_at"),
    }
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        if maint.get("active"):
            print("MAINT   : ON  " + maintenance_text(maint))
        else:
            print("MAINT   : OFF")
        print(f"LEVEL   : {level}  (score={score})")
        print(f"SESSION : {out['session']}   codex={out.get('codex_session')}   elapsed={out['elapsed_min']}min")
        print(f"RULES   : {detail}")
        print(f"DEBT    : open={out['open_debt']} critical={out['open_critical_debt']}")
        print(f"COUNTERS: {out['counters']}")
        if reasons:
            print("REASONS :")
            for r in reasons:
                print(f"  - {r}")
        if out["directive"]:
            print("\n" + out["directive"])
    return 0 if maint.get("active") else (2 if level == "BLOCKED" else 0)


def cmd_gate(args):
    deliverable = args.deliverable
    s = load_session()
    if has_passing_check(deliverable):
        s.get("claim_state", {}).pop(deliverable, None)
        save_session(s)
        audit("gate", deliverable=deliverable, result="allow")
        print(json.dumps({"decision": "allow", "deliverable": deliverable,
                          "note": "存在监督层执行的通过检查记录"}, ensure_ascii=False))
        return 0

    cs = s.setdefault("claim_state", {}).setdefault(deliverable, {"unverified": 0, "no_evidence": 0})
    cs["unverified"] += 1
    if not args.evidence:
        cs["no_evidence"] += 1
    save_session(s)
    audit("gate", deliverable=deliverable, result="deny")
    print(json.dumps({
        "decision": "deny",
        "deliverable": deliverable,
        "reason": "没有监督层执行的通过检查记录 —— 生产者无权自证完成",
        "required": f"supervisor.py check --name <check> --deliverable {deliverable}",
    }, ensure_ascii=False))
    return 2


def cmd_check(args):
    passed, record = run_check(args.name, args.deliverable)
    if isinstance(record, str):
        print(json.dumps({"passed": False, "error": record}, ensure_ascii=False))
        return 2
    print(json.dumps({"passed": passed, "record": record}, ensure_ascii=False, indent=2))
    return 0 if passed else 2


def cmd_debt(args):
    if args.action == "add":
        if not args.text:
            print(json.dumps({"ok": False, "error": "需要 --text"}, ensure_ascii=False))
            return 2
        did = add_debt(args.text, args.severity, args.deliverable)
        s = load_session()
        if s.get("unregistered_failures", 0) > 0:
            s["unregistered_failures"] -= 1
            save_session(s)
        print(json.dumps({"added": did}, ensure_ascii=False))
        return 0
    if args.action == "list":
        items = parse_debt()
        if args.json:
            print(json.dumps(items, ensure_ascii=False, indent=2))
        else:
            if not items:
                print("（空）")
            for it in items:
                print(f"[{it['id']}] {it['severity']:8s} {it['status']:6s} {it['text']}")
        return 0
    if args.action == "clear":
        if not args.id:
            print(json.dumps({"ok": False, "error": "需要 --id"}, ensure_ascii=False))
            return 2
        ok = clear_debt(args.id, args.evidence)
        print(json.dumps({"cleared": ok, "id": args.id}, ensure_ascii=False))
        return 0 if ok else 2
    return 3


def _set_readonly(path, readonly: bool) -> None:
    try:
        subprocess.run(["attrib", "+R" if readonly else "-R", str(path)], capture_output=True)
    except Exception:
        pass


def _read_samples() -> list:
    if not SAMPLES.exists():
        return []
    out = []
    for line in SAMPLES.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def cmd_calibrate(args):
    """③ 依据历史会话样本建议阈值。只建议，不自动改 —— 改规则仍须 rules approve。"""
    rules, err = load_rules()
    if rules is None:
        print(json.dumps({"ok": False, "error": err}, ensure_ascii=False))
        return 3
    samples = _read_samples()
    if not samples:
        print(json.dumps({"ok": False, "detail": "没有历史样本（state/samples.jsonl 为空），无法标定"},
                         ensure_ascii=False))
        return 2

    def dist(metric):
        return [(r.get("metrics") or {}).get(metric) for r in samples]

    metrics = ["turns", "context_chars", "images", "compactions", "tool_errors",
               "rework", "unfinished", "subagents_active", "elapsed_min"]
    observed = {m: {str(p): percentile(dist(m), p) for p in (50, 90, 95, 99)} for m in metrics}

    mapping = [("turns", "turns_watch"), ("context_chars", "context_chars_watch"),
               ("images", "images_watch"), ("compactions", "compactions_watch"),
               ("tool_errors", "tool_errors_watch"), ("rework", "rework_watch"),
               ("unfinished", "unfinished_watch"), ("subagents_active", "subagents_watch"),
               ("elapsed_min", "elapsed_min_watch")]
    proposed = {}
    for metric, key in mapping:
        v = percentile(dist(metric), args.pct)
        if v is not None:
            proposed[key] = v

    out = {
        "ok": True,
        "samples": len(samples),
        "percentile": args.pct,
        "observed": observed,
        "current": rules.get("thresholds", {}),
        "proposed": proposed,
        "note": "watch 建议取历史分布的该分位数：低于它属正常，超过就该看一眼。",
    }
    ensure_layout()
    CALIBRATION.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.apply:
        _set_readonly(RULES_FILE, False)
        rules.setdefault("thresholds", {}).update(proposed)
        RULES_FILE.write_text(json.dumps(rules, ensure_ascii=False, indent=2), encoding="utf-8")
        _set_readonly(RULES_FILE, True)
        out["applied"] = True
        out["next"] = 'python supervisor.py rules approve --by <人> --reason "阈值自动标定"'
        out["warning"] = "已写入 rules.json 但尚未批准 —— 不执行 rules approve 会因哈希不匹配被硬阻断。"
        audit("calibrate_apply", samples=len(samples), proposed=proposed)

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_handoff(args):
    """⑤ 人工生成人机交接单。"""
    s = load_session()
    path = write_handoff(s, level=args.level or "manual")
    print(json.dumps({"ok": True, "path": path, "session": s.get("session_id")},
                     ensure_ascii=False))
    return 0


def cmd_rules(args):
    if args.action == "verify":
        ok, detail = rules_integrity()
        print(json.dumps({"ok": ok, "detail": detail}, ensure_ascii=False))
        return 0 if ok else 2
    if args.action == "freeze":
        print(json.dumps({"frozen": freeze_rules()}, ensure_ascii=False))
        return 0
    if args.action == "approve":
        if not args.reason or not args.by:
            print(json.dumps({"ok": False, "error": "批准必须带 --reason 和 --by"}, ensure_ascii=False))
            return 2
        digest = sha256_file(RULES_FILE)
        rec = {"ts": now_iso(), "new_hash": digest, "reason": args.reason, "approved_by": args.by}
        APPROVALS.mkdir(parents=True, exist_ok=True)
        atomic_write_json(APPROVALS / f"rules-{digest[:12]}.json", rec)
        # 原子写 hash：非原子写会被并发 observe 读到中间态（与 freeze_rules 同款）
        _tmp = RULES_HASH.with_suffix(RULES_HASH.suffix + ".tmp")
        _tmp.write_text(digest, encoding="utf-8")
        os.replace(_tmp, RULES_HASH)
        audit("rules_approve", **rec)
        print(json.dumps({"approved": True, "hash": digest}, ensure_ascii=False))
        return 0
    return 3


def cmd_maintenance(args):
    """External-only, time-limited development/maintenance bypass.

    This is deliberately not a resident mode: status reads a JSON switch; on/off
    are one-shot external commands. Active maintenance allows the hook to proceed
    while preserving the underlying score/level and writing audit records.
    """
    if args.action == "status":
        m = maintenance_state()
        if args.json:
            print(json.dumps({"ok": True, "maintenance": m}, ensure_ascii=False, indent=2))
        elif m.get("active"):
            print("维护模式：已开启")
            print("开启人：%s" % (m.get("by") or "未署名"))
            print("原因：%s" % (m.get("reason") or "未说明"))
            print("到期时间：%s" % _friendly_ts(m.get("until")))
            print("剩余时间：约 %.0f 分钟" % (m.get("remaining_min") or 0))
        else:
            print("维护模式：未开启")
        return 0
    if _inside_codex():
        rec = {"action": args.action, "by": args.by or "unknown"}
        audit("maintenance_rejected", **rec)
        print(json.dumps({"ok": False,
                          "error": "维护模式只能在 Codex 之外启停（检测到 CODEX_SESSION_ID/THREAD_ID/SHELL）"},
                         ensure_ascii=False))
        return 2
    if not args.by or not args.reason:
        print(json.dumps({"ok": False, "error": "维护模式启停必须带 --by 与 --reason"}, ensure_ascii=False))
        return 2
    if args.action == "on":
        minutes = int(args.minutes or 60)
        if minutes < 1 or minutes > 480:
            print(json.dumps({"ok": False, "error": "--minutes 必须在 1..480 分钟"}, ensure_ascii=False))
            return 2
        m = write_maintenance(True, args.by, args.reason, minutes)
        audit("maintenance_on", by=args.by, reason=args.reason,
              minutes=minutes, until=m.get("until"))
        if getattr(args, "human", False):
            print("维护模式：已开启")
            print("开启人：%s" % args.by)
            print("原因：%s" % args.reason)
            print("有效时间：%d 分钟" % minutes)
            print("到期时间：%s" % _friendly_ts(m.get("until")))
        else:
            print(json.dumps({"ok": True, "maintenance": m}, ensure_ascii=False))
        return 0
    if args.action == "off":
        m = write_maintenance(False, args.by, args.reason)
        audit("maintenance_off", by=args.by, reason=args.reason)
        if getattr(args, "human", False):
            print("维护模式：已关闭")
            print("操作人：%s" % args.by)
            print("原因：%s" % args.reason)
        else:
            print(json.dumps({"ok": True, "maintenance": m}, ensure_ascii=False))
        return 0
    return 2


def cmd_resume(args):
    """由人解除阻断 —— 记录批准，并重置会话计数（重新锚定）。"""
    if not args.by or not args.reason:
        print(json.dumps({"ok": False, "reason": "恢复必须由人执行，需要 --by 与 --reason"}, ensure_ascii=False))
        return 2
    inter = read_intervention()
    rec = {"ts": now_iso(), "approved_by": args.by, "reason": args.reason,
           "nonce": uuid.uuid4().hex[:8], "cleared_blocked_at": inter.get("blocked_at")}
    APPROVALS.mkdir(parents=True, exist_ok=True)
    atomic_write_json(APPROVALS / f"resume-{rec['nonce']}.json", rec)
    s = new_session()
    atomic_write_json(SESSION, s)
    atomic_write_json(INTERVENTION, {"ts": now_iso(), "level": "NORMAL", "active": False,
                                     "directive": None, "resumed_by": args.by,
                                     "resumed_at": now_iso()})
    if maintenance_state().get("active"):
        write_maintenance(False, args.by, "resume")
        audit("maintenance_off", by=args.by, reason="resume")
    audit("resume", **rec)
    print(json.dumps({"ok": True, "resumed_by": args.by, "new_session": s["session_id"]},
                     ensure_ascii=False))
    return 0


def cmd_break_glass(args):
    rec = {"ts": now_iso(), "reason": args.reason or "(未说明)", "actor": "producer-agent",
           "session": load_session().get("session_id")}
    append_jsonl(BREAK_GLASS, rec)
    audit("break_glass", **rec)
    atomic_write_json(INTERVENTION, {"ts": now_iso(), "level": "NORMAL", "active": False,
                                     "directive": None, "break_glass": True})
    print(json.dumps({"ok": True, "warning": "已记录到 break_glass 永久审计", "record": rec},
                     ensure_ascii=False))
    return 0


def cmd_reset(args):
    s = new_session()
    atomic_write_json(SESSION, s)
    atomic_write_json(INTERVENTION, {"ts": now_iso(), "level": "NORMAL", "active": False, "directive": None})
    audit("reset", session=s["session_id"])
    print(json.dumps({"ok": True, "session": s["session_id"]}, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------- cli

def cmd_doctor(args):
    """自检：规则完整性 + hooks.json 接线 + 解释器路径 + 脚本是否在位。

    目的：解释器或接线失效时 hook 是"静默不执行"的 —— 这个命令把它变成可见失败。
    """
    import re as _re
    checks = []

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})

    ok, detail = rules_integrity()
    add("rules_integrity", ok, detail)

    hooks_path = Path.home() / ".codex" / "hooks.json"
    conf = read_json(hooks_path)
    if not isinstance(conf, dict):
        add("hooks_json", False, "读不到或不是对象: " + str(hooks_path))
    else:
        add("hooks_json", True, str(hooks_path))
        entries = []
        for _evt, groups in (conf.get("hooks") or {}).items():
            for g in (groups or []):
                for hh in ((g or {}).get("hooks") or []):
                    entries.append(str((hh or {}).get("command") or ""))
        # 接线判定：认可 run_hook.cmd 启动器写法，也兼容早期直接调用 hook_*.ps1
        wired = (
            ("supervisor", "run_hook.cmd supervisor", "hook_supervisor.ps1"),
            ("post", "run_hook.cmd post", "hook_post.ps1"),
            ("session", "run_hook.cmd session", "hook_session.ps1"),
        )
        for name, needle, legacy in wired:
            add("wired:" + name, any(needle in e or legacy in e for e in entries))
        seen = set()
        for e in entries:
            m = _re.match(r'"?([A-Za-z]:[^"]*?[.]exe)', e)
            if m:
                seen.add(m.group(1))
        for exe in sorted(seen):
            add("interpreter:" + Path(exe).name, Path(exe).exists(), exe)

        # view_image 守卫：hooks.json 的 PreToolUse(view_image) 指向
        # tools/view_image_guard.ps1 —— 它不在 ROOT 下，最容易被漏检。
        # 而它失效是"静默放行大图"（大图进上下文 -> 413），正是 doctor 要消灭的失败模式。
        # 2026-10-06 补检（当时它确实已因 cp936 误读而静默失效）。
        guard_entry = ""
        for e in entries:
            if "view_image_guard.ps1" in e:
                guard_entry = e
                break
        add("wired:view_image_guard", bool(guard_entry), guard_entry)
        m2 = _re.search(r'-File\s+"?([^"\s]+\.ps1)', guard_entry)
        gp = Path(m2.group(1)) if m2 else (Path.home() / ".codex" / "tools" / "view_image_guard.ps1")
        add("file:view_image_guard.ps1", gp.exists(), str(gp))
        iprev = gp.parent / "imgpreview.ps1"
        add("file:imgpreview.ps1", iprev.exists(), str(iprev))

    for script in ("supervisor.py", "run_hook.cmd", "hook_supervisor.ps1", "hook_post.ps1", "hook_session.ps1"):
        f = ROOT / script
        add("file:" + script, f.exists(), str(f))

    failed = [c["name"] for c in checks if not c["ok"]]
    out = {"ok": not failed, "failed": failed, "checks": checks}
    if getattr(args, "json", False):
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        for c in checks:
            print(("OK   " if c["ok"] else "FAIL ") + c["name"] + (("  " + c["detail"]) if c["detail"] else ""))
        print("DOCTOR: " + ("全部通过" if not failed else str(len(failed)) + " 项失败 -> " + str(failed)))
    return 0 if not failed else 2


def build_parser():
    p = argparse.ArgumentParser(prog="supervisor", description="anti-degradation supervisor")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(func=cmd_init)

    sp = sub.add_parser("observe", help="记录事件并返回决策（hook 调用）")
    sp.add_argument("--event", default=None)
    sp.set_defaults(func=cmd_observe)

    sp = sub.add_parser("status")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("gate")
    sp.add_argument("--deliverable", required=True)
    sp.add_argument("--claim", default=None)
    sp.add_argument("--evidence", default=None)
    sp.set_defaults(func=cmd_gate)

    sp = sub.add_parser("check")
    sp.add_argument("--name", required=True)
    sp.add_argument("--deliverable", default=None)
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("debt")
    sp.add_argument("action", choices=["add", "list", "clear"])
    sp.add_argument("--text", default=None)
    sp.add_argument("--severity", default="normal", choices=["normal", "critical"])
    sp.add_argument("--deliverable", default=None)
    sp.add_argument("--id", default=None)
    sp.add_argument("--evidence", default=None)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_debt)

    sp = sub.add_parser("rules")
    sp.add_argument("action", choices=["verify", "freeze", "approve"])
    sp.add_argument("--reason", default=None)
    sp.add_argument("--by", default=None)
    sp.set_defaults(func=cmd_rules)

    sp = sub.add_parser("maintenance", help="外部开发/维护临时放行（限时，不常驻）")
    sp.add_argument("action", choices=["on", "off", "status"])
    sp.add_argument("--minutes", type=int, default=60)
    sp.add_argument("--by", default=None)
    sp.add_argument("--reason", default=None)
    sp.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    sp.add_argument("--human", action="store_true", help="输出中文说明")
    sp.set_defaults(func=cmd_maintenance)

    sp = sub.add_parser("resume")
    sp.add_argument("--by", default=None)
    sp.add_argument("--reason", default=None)
    sp.set_defaults(func=cmd_resume)

    sp = sub.add_parser("break-glass")
    sp.add_argument("--reason", default=None)
    sp.set_defaults(func=cmd_break_glass)

    sp = sub.add_parser("calibrate", help="③ 依据历史样本建议阈值（改规则仍须 rules approve）")
    sp.add_argument("--pct", type=float, default=95.0)
    sp.add_argument("--apply", action="store_true")
    sp.set_defaults(func=cmd_calibrate)

    sp = sub.add_parser("handoff", help="⑤ 生成人机交接单")
    sp.add_argument("--level", default=None)
    sp.set_defaults(func=cmd_handoff)

    sub.add_parser("reset").set_defaults(func=cmd_reset)

    sp = sub.add_parser("doctor", help="自检：hook 接线 / 解释器路径 / 规则完整性")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_doctor)
    return p


def _force_utf8():
    # stdout/stderr + stdin 全部钉死 UTF-8。
    # stdin 同样重要：hook 侧已改为显式写 UTF-8 字节（见 hook_*.ps1 的 Write-Utf8Stdin），
    # 这里若还按控制台代码页(cp936)读，中文就会变成 observe_parse_error。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    try:
        sys.stdin.reconfigure(encoding="utf-8")
    except Exception:
        pass


def main(argv=None):
    _force_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:
        audit("supervisor_error", error=repr(exc))
        print(json.dumps({"decision": "deny", "level": "BLOCKED", "score": 999,
                          "reason": f"监督层异常，保守阻断: {exc!r}"}, ensure_ascii=False))
        return 3


if __name__ == "__main__":
    sys.exit(main())
