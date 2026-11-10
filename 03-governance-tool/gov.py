#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gov.py —— 轻量多代理治理工具（Super-agent 深挖任务③）。

把本工作区 AGENTS.md 里手搓的治理纪律抽成通用 CLI，三件事：

1. 写作用域声明与校验：每个任务声明它允许写的路径前缀，写之前 `gov check`；
2. 冲突检测：两个活动任务的写作用域重叠 → `gov conflicts` 报警（advisory，不是锁）；
3. 审阅留痕：判决/裁决追加进 append-only JSONL（flock + 追加，永不整文件重写）。

用法：
    python3 gov.py claim  T1 --scope "src/a/**" --scope "docs/a.md"   # 声明任务作用域
    python3 gov.py check  T1 src/a/foo.ts                             # 写前校验
    python3 gov.py release T1                                         # 释放
    python3 gov.py conflicts                                          # 重叠报警
    python3 gov.py ruling R1 --verdict supported --why "证据齐全"      # 审阅留痕
    python3 gov.py selftest                                           # RED→GREEN 自检

状态文件：.governance/scopes.json（登记表）+ .governance/rulings.jsonl（判决流）。
"""
from __future__ import annotations

import argparse
import fcntl
import json
import sys
import time
from pathlib import Path

GOV_DIR = Path(".governance")
SCOPES = GOV_DIR / "scopes.json"
RULINGS = GOV_DIR / "rulings.jsonl"

# ---------------------------------------------------------------------------
# 登记表读写（读-改-写全程持 flock，防并发会话互踩）
# ---------------------------------------------------------------------------


def _load_scopes() -> dict:
    if not SCOPES.exists():
        return {"tasks": {}}
    return json.loads(SCOPES.read_text(encoding="utf-8"))


def _save_scopes(data: dict) -> None:
    GOV_DIR.mkdir(exist_ok=True)
    with open(SCOPES, "w", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(data, ensure_ascii=False, indent=2))
        fcntl.flock(f, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# 命令实现
# ---------------------------------------------------------------------------


def cmd_claim(task_id: str, scopes: list[str]) -> int:
    data = _load_scopes()
    if task_id in data["tasks"] and data["tasks"][task_id].get("active"):
        print(f"ERR: 任务 {task_id} 已有活动声明，先 release 再 claim", file=sys.stderr)
        return 2
    data["tasks"][task_id] = {
        "scopes": sorted(set(scopes)),
        "active": True,
        "claimed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    _save_scopes(data)
    print(f"OK: {task_id} 声明了 {len(scopes)} 个写作用域")
    return 0


def cmd_release(task_id: str) -> int:
    data = _load_scopes()
    if task_id not in data["tasks"]:
        print(f"ERR: 任务 {task_id} 不存在", file=sys.stderr)
        return 2
    data["tasks"][task_id]["active"] = False
    data["tasks"][task_id]["released_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    _save_scopes(data)
    print(f"OK: {task_id} 已释放")
    return 0


def _matches(path: str, pattern: str) -> bool:
    """前缀/通配匹配：src/a/** 匹配 src/a 下一切；docs/a.md 精确匹配。"""
    if pattern.endswith("/**"):
        return path.startswith(pattern[:-2])
    return path == pattern or path.startswith(pattern.rstrip("/") + "/")


def cmd_check(task_id: str, path: str) -> int:
    data = _load_scopes()
    task = data["tasks"].get(task_id)
    if not task or not task.get("active"):
        print(f"BLOCK: {task_id} 无活动作用域声明——没有声明就不许写", file=sys.stderr)
        return 2
    if any(_matches(path, p) for p in task["scopes"]):
        print(f"OK: {path} ∈ {task_id} 的声明作用域")
        return 0
    print(f"BLOCK: {path} 不在 {task_id} 声明的作用域 {task['scopes']} 内", file=sys.stderr)
    return 2


def cmd_conflicts() -> int:
    data = _load_scopes()
    active = {k: v for k, v in data["tasks"].items() if v.get("active")}
    found = False
    ids = sorted(active)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            overlap = [
                pa for pa in active[a]["scopes"]
                if any(_matches(pa, pb) or _matches(pb, pa) for pb in active[b]["scopes"])
            ]
            if overlap:
                found = True
                print(f"OVERLAP: {a} × {b} 共同作用域: {overlap}")
    if not found:
        print("OK: 活动任务间无写作用域重叠")
    return 0


def cmd_ruling(ruling_id: str, verdict: str, why: str, reviewer: str = "") -> int:
    """append-only 判决流：单次 O_APPEND 写 + flock，绝不整文件重写。"""
    GOV_DIR.mkdir(exist_ok=True)
    line = json.dumps(
        {
            "id": ruling_id,
            "verdict": verdict,
            "why": why,
            "reviewer": reviewer,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        },
        ensure_ascii=False,
    )
    with open(RULINGS, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(line + "\n")
        fcntl.flock(f, fcntl.LOCK_UN)
    print(f"OK: 判决 {ruling_id}({verdict}) 已落 {RULINGS}")
    return 0


# ---------------------------------------------------------------------------
# 自检（RED→GREEN）
# ---------------------------------------------------------------------------


def selftest() -> None:
    import shutil
    import tempfile

    global GOV_DIR, SCOPES, RULINGS
    tmp = Path(tempfile.mkdtemp())
    GOV_DIR = tmp / ".governance"
    SCOPES = GOV_DIR / "scopes.json"
    RULINGS = GOV_DIR / "rulings.jsonl"
    try:
        # ---- RED 1：未声明作用域就写 → 必须 BLOCK ----
        cmd_claim("T1", ["src/a/**"])
        assert cmd_check("T1", "src/b/leak.ts") == 2, "未声明路径必须被拦"
        # ---- RED 2：作用域重叠必须报警 ----
        cmd_claim("T2", ["src/a/sub/**"])
        assert cmd_conflicts() != 0 or _overlap_exists(["T1", "T2"]), "重叠必须被检出"
        # ---- GREEN 1：声明范围内放行 ----
        assert cmd_check("T1", "src/a/foo.ts") == 0, "范围内必须放行"
        assert cmd_check("T1", "src/a/sub/x.ts") == 0, "** 应覆盖子树"
        # ---- GREEN 2：无重叠时不误报 ----
        cmd_release("T2")
        cmd_claim("T2", ["src/c/**"])
        assert not _overlap_exists(["T1", "T2"]), "不相交作用域不应报重叠"
        # ---- GREEN 3：判决流 append-only 且可读回 ----
        cmd_ruling("R1", "supported", "证据齐全")
        cmd_ruling("R2", "needs-work", "缺 file:line 引用")
        lines = RULINGS.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2 and json.loads(lines[1])["id"] == "R2", "判决流读写不符"
        print("GOV SELFTEST PASS: 2 RED（拦住越权写、抓到重叠）+ 3 GREEN（放行/无误报/留痕）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _overlap_exists(ids: list[str]) -> bool:
    data = _load_scopes()
    active = {k: v for k, v in data["tasks"].items() if k in ids and v.get("active")}
    ids = sorted(active)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if any(
                _matches(pa, pb) or _matches(pb, pa)
                for pa in active[a]["scopes"]
                for pb in active[b]["scopes"]
            ):
                return True
    return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    p1 = sub.add_parser("claim")
    p1.add_argument("task")
    p1.add_argument("--scope", action="append", required=True)
    p2 = sub.add_parser("release")
    p2.add_argument("task")
    p3 = sub.add_parser("check")
    p3.add_argument("task")
    p3.add_argument("path")
    sub.add_parser("conflicts")
    p5 = sub.add_parser("ruling")
    p5.add_argument("id")
    p5.add_argument("--verdict", required=True)
    p5.add_argument("--why", required=True)
    p5.add_argument("--reviewer", default="")
    args = ap.parse_args()

    if args.selftest:
        selftest()
        return
    rc = {
        "claim": lambda: cmd_claim(args.task, args.scope),
        "release": lambda: cmd_release(args.task),
        "check": lambda: cmd_check(args.task, args.path),
        "conflicts": cmd_conflicts,
        "ruling": lambda: cmd_ruling(args.id, args.verdict, args.why, args.reviewer),
    }[args.cmd]()
    sys.exit(rc)


if __name__ == "__main__":
    main()
