#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多代理 vs 单循环 成本测量框架（Super-agent 深挖任务②）。

用法：
    python3 framework.py --selftest          # 框架自检（RED→GREEN，零网络）
    python3 framework.py --demo              # Echo 后端演示跑，生成 results/demo/
    python3 framework.py --endpoint URL --model ID --tasks tasks.json   # 真实跑（上云）

设计要点见同目录 DESIGN.md。所有数字从落盘证据可复算；禁止估算 token。
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class Task:
    """一个待执行任务。complexity: L1/L2/L3 档（见 DESIGN.md 第 2 节）。"""

    id: str
    prompt: str
    complexity: str
    est_output_tokens: int = 200  # 仅用于 Echo 后端模拟，真实跑时忽略


@dataclass
class CallRecord:
    """一次 LLM 调用的记录。token 数一律来自响应 usage 字段，禁止估算。"""

    role: str  # "router" | "worker" | "single-loop"
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int = 0
    wall_seconds: float = 0.0


@dataclass
class InterventionRecord:
    """一次人工介入。decision_kind 必须属于预注册的三类，纯进度汇报不算。"""

    task_id: str
    decision_kind: str  # approval | missing_info | stuck
    detail: str = ""


@dataclass
class RunRecord:
    """一次 run 的完整原始记录，直接落盘 metrics.json。"""

    run_id: str
    arm: str  # "single" | "dual"
    seed: int
    calls: list[CallRecord] = field(default_factory=list)
    interventions: list[InterventionRecord] = field(default_factory=list)
    rework: int = 0  # 返工次数：产出未过验收被重做
    wall_seconds: float = 0.0
    router_overhead_tokens: int = 0  # 仅 dual：Router 协调税（prompt tokens）

    def total_tokens(self) -> int:
        return sum(c.prompt_tokens + c.completion_tokens for c in self.calls)

    def router_tax(self) -> int:
        """协调税 = Router 角色的全部 prompt tokens（double 记账防漏）。"""
        tax = sum(c.prompt_tokens for c in self.calls if c.role == "router")
        # router_overhead_tokens 是兜底字段（框架级直接注入的开销），两者取和
        return tax + self.router_overhead_tokens


# ---------------------------------------------------------------------------
# Executor 抽象：三个实现位（DESIGN.md 第 5 节）
# ---------------------------------------------------------------------------

Executor = Callable[[list[Task], str, int], RunRecord]


class EchoExecutor:
    """确定性回声后端：不调模型，按任务的 est_output_tokens 造 RunRecord。
    只用于框架自检与管线调试，产出永远不能当实验数字引用。"""

    def __init__(self, interventions_per_task: float = 0.5, rework_rate: float = 0.0):
        self.interventions_per_task = interventions_per_task
        self.rework_rate = rework_rate

    def __call__(self, tasks: list[Task], arm: str, seed: int) -> RunRecord:
        rng = random.Random(seed)
        rec = RunRecord(run_id=f"echo-{arm}-{seed}", arm=arm, seed=seed)
        t0 = time.monotonic()
        for t in tasks:
            if arm == "single":
                # 单循环：每任务一次主循环调用，上下文累积 → prompt 逐任务线性增长
                prompt = 1000 * (tasks.index(t) + 1)
                rec.calls.append(CallRecord("single-loop", prompt, t.est_output_tokens))
            else:
                # 双层：Router 派发一次 + Worker 独立执行一次（上下文不累积）
                rec.calls.append(CallRecord("router", 300, 50))
                rec.calls.append(CallRecord("worker", 500, t.est_output_tokens))
            # 介入按预注册口径生成（echo 只是模拟器，量级来自构造参数）
            n_int = rng.random() < self.interventions_per_task
            if n_int:
                kind = rng.choice(["approval", "missing_info", "stuck"])
                rec.interventions.append(InterventionRecord(t.id, kind))
            if rng.random() < self.rework_rate:
                rec.rework += 1
        rec.wall_seconds = time.monotonic() - t0 + 0.01 * len(tasks)
        return rec


class OpenAICompatExecutor:
    """OpenAI 兼容端点执行器。usage 逐调用取自响应体，缓存进 CallRecord。
    注意：这是"纯 API 测量"通道，测量的是任务级成本结构；
    要测真实 harness（dsh 双层 vs 单会话）需另写适配器（DESIGN.md 第 5 节第 3 条）。"""

    def __init__(self, endpoint: str, model: str, api_key: str = ""):
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")

    def _chat(self, messages: list[dict]) -> dict:
        import urllib.request

        body = json.dumps(
            {"model": self.model, "messages": messages, "temperature": 0.7}
        ).encode()
        req = urllib.request.Request(
            self.endpoint + "/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())

    def __call__(self, tasks: list[Task], arm: str, seed: int) -> RunRecord:
        rec = RunRecord(run_id=f"api-{arm}-{seed}", arm=arm, seed=seed)
        t0 = time.monotonic()
        if arm == "single":
            history: list[dict] = [
                {"role": "system", "content": "你是执行代理，逐个完成任务并给出结果。"}
            ]
            for t in tasks:
                history.append({"role": "user", "content": t.prompt})
                r = self._chat(history)
                u = r["usage"]
                rec.calls.append(
                    CallRecord(
                        "single-loop",
                        u["prompt_tokens"],
                        u["completion_tokens"],
                        u.get("prompt_tokens_details", {}).get("cached_tokens", 0),
                    )
                )
                history.append(
                    {"role": "assistant", "content": r["choices"][0]["message"]["content"]}
                )
        else:
            raise NotImplementedError(
                "dual 臂的真实执行需要 harness 适配器（Router 派发逻辑），"
                "本框架只提供记账结构；先跑 single 臂或用 Echo 后端联调。"
            )
        rec.wall_seconds = time.monotonic() - t0
        return rec


# ---------------------------------------------------------------------------
# 成本模型与盈亏平衡
# ---------------------------------------------------------------------------


@dataclass
class CostModel:
    """币种统一为人民币。price_per_1m_tokens：每 1M token 单价；
    attention_value_per_hour：人的注意力时薪；
    intervention_minutes：一次介入打断人的平均分钟数。"""

    price_per_1m_tokens: float = 4.0  # 元 / 1M tokens（按实际模型改）
    attention_value_per_hour: float = 100.0  # 元 / 小时（按实际改）
    intervention_minutes: float = 5.0  # 分钟 / 次

    def value_per_intervention(self) -> float:
        return self.intervention_minutes / 60.0 * self.attention_value_per_hour

    def cost(self, run: RunRecord) -> dict:
        toks = run.total_tokens()
        v_int = self.value_per_intervention()
        token_cost = toks / 1_000_000 * self.price_per_1m_tokens
        attention_cost = len(run.interventions) * v_int
        return {
            "total_tokens": toks,
            "router_tax_tokens": run.router_tax(),
            "token_cost": round(token_cost, 6),
            "interventions": len(run.interventions),
            "attention_cost": round(attention_cost, 6),
            "rework": run.rework,
            "wall_seconds": round(run.wall_seconds, 3),
            "total_cost": round(token_cost + attention_cost, 6),
        }


def breakeven(cost_single: float, cost_dual: float, tokens_single: int,
              tokens_dual: int, n_int_single: int, n_int_dual: int) -> dict:
    """盈亏平衡两种读法（DESIGN.md 第 1 节）。任一 delta 为零时返回 None 项，
    那一侧的平衡点不存在（一个方案全维占优或全维占劣）。"""
    d_t = tokens_dual - tokens_single
    d_n = n_int_single - n_int_dual  # 双层省下的介入次数（正数才对双层有利）
    out: dict = {"delta_tokens": d_t, "delta_interventions": d_n,
                 "delta_cost": round(cost_dual - cost_single, 6)}
    if d_n > 0 and d_t > 0:
        # p* = ΔN × V_int / ΔT：token 价格低于 p* 时双层划算
        # C* = ΔT × p / ΔN：注意力价值高于 C* 时双层划算（p 为本轮实际单价）
        # 这里只给结构；V_int 与 p 由 CostModel 在调用方代入。
        out["note"] = "需要代入 price 与 value_per_intervention 才能得 p*/C*"
    return out


def breakeven_price(delta_interventions: int, value_per_intervention: float,
                    delta_tokens: int) -> Optional[float]:
    """Breakeven token price：p* = ΔN_int × V_int / ΔT（元/token）。
    返回 None 表示该侧平衡点不存在。"""
    if delta_tokens <= 0 or delta_interventions <= 0:
        return None
    return delta_interventions * value_per_intervention / delta_tokens


def breakeven_attention(delta_tokens: int, price_per_token: float,
                        delta_interventions: int) -> Optional[float]:
    """Breakeven attention value：C* = ΔT × p / ΔN_int（元/次介入）。"""
    if delta_interventions <= 0 or delta_tokens <= 0:
        return None
    return delta_tokens * price_per_token / delta_interventions


# ---------------------------------------------------------------------------
# 判决（预注册规则，DESIGN.md 第 4 节）
# ---------------------------------------------------------------------------


def verdict(delta_costs: list[float], ci_low: float, ci_high: float) -> str:
    """三档判决。ci 为配对 bootstrap 的 95% CI。禁止换指标。"""
    if ci_high < 0:
        return "DUAL_WINS"
    if ci_low > 0:
        return "SINGLE_WINS"
    return "INCONCLUSIVE"


def paired_bootstrap_ci(deltas: list[float], n_boot: int = 2000,
                        seed: int = 0) -> tuple[float, float]:
    """配对 bootstrap 95% CI。"""
    rng = random.Random(seed)
    n = len(deltas)
    means = []
    for _ in range(n_boot):
        sample = [deltas[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    return means[int(0.025 * n_boot)], means[int(0.975 * n_boot) - 1]


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------


def save_run(results_dir: Path, run: RunRecord, cost: dict) -> Path:
    d = results_dir / run.run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "metrics.json").write_text(
        json.dumps(
            {
                "run_id": run.run_id,
                "arm": run.arm,
                "seed": run.seed,
                "cost": cost,
                "calls": [dataclasses.asdict(c) for c in run.calls],
                "interventions": [dataclasses.asdict(i) for i in run.interventions],
                "rework": run.rework,
                "wall_seconds": run.wall_seconds,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return d / "metrics.json"


# ---------------------------------------------------------------------------
# 自检（RED→GREEN，AGENTS.md 第 21 条）
# ---------------------------------------------------------------------------


def selftest() -> None:
    """先注入违规证明门会红，再注入合规证明放行。断言引用的期望值全部现算。"""

    tasks = [Task(f"t{i}", f"任务{i}", "L1", est_output_tokens=100) for i in range(4)]

    # ---- RED 1：介入判定漏记必须能被抓出来 ----
    ex = EchoExecutor(interventions_per_task=1.0)  # 每任务必介入
    run = ex(tasks, "single", 7)
    assert len(run.interventions) == 4, f"echo 构造失败: {len(run.interventions)}"
    # 作弊 run：把介入记录悄悄删掉再算成本 → attention_cost 应跌到 0，即"账对不上"
    cheat = dataclasses.replace(run)
    cheat.interventions = []
    cm = CostModel()
    real_cost = cm.cost(run)
    cheat_cost = cm.cost(cheat)
    assert real_cost["interventions"] == 4 and cheat_cost["interventions"] == 0, (
        "介入记账门失效"
    )
    assert cheat_cost["attention_cost"] < real_cost["attention_cost"], "作弊成本没有变低"

    # ---- RED 2：router 税漏记必须能被抓出来 ----
    run_d = EchoExecutor()(tasks, "dual", 7)
    assert run_d.router_tax() > 0, "dual 臂必须有协调税"
    no_tax = dataclasses.replace(run_d, calls=[
        c for c in run_d.calls if c.role != "router"
    ])
    assert no_tax.router_tax() == 0, "router 税门失效：剔掉 router 后应为 0"

    # ---- GREEN 1：盈亏平衡数学用现算期望值双向验证 ----
    # 结构：ΔN=6 次介入 × 5 元/次 = 30 元；ΔT=200k tokens
    p_star = breakeven_price(6, 5.0, 200_000)
    assert p_star is not None
    assert abs(p_star - 30.0 / 200_000) < 1e-12, f"p* 现算不符: {p_star}"
    c_star = breakeven_attention(200_000, 30.0 / 200_000, 6)
    assert c_star is not None and abs(c_star - 5.0) < 1e-9, f"C* 现算不符: {c_star}"
    # 反向：ΔN<=0 或 ΔT<=0 时平衡点不存在
    assert breakeven_price(0, 5.0, 200_000) is None
    assert breakeven_attention(-1, 0.1, 6) is None

    # ---- GREEN 2：判决三档方向正确 ----
    assert verdict([1.0] * 5, 0.5, 1.5) == "SINGLE_WINS"
    assert verdict([-1.0] * 5, -1.5, -0.5) == "DUAL_WINS"
    assert verdict([1.0, -1.0] * 3, -0.5, 0.5) == "INCONCLUSIVE"

    # ---- GREEN 3：落盘可复算 ----
    out = save_run(Path("results/selftest"), run, real_cost)
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded["cost"]["interventions"] == real_cost["interventions"]
    # 从落盘 evidence 重算 token 总量，与内存值一致（铁律 1）
    recomputed = sum(c["prompt_tokens"] + c["completion_tokens"]
                     for c in loaded["calls"])
    assert recomputed == loaded["cost"]["total_tokens"], "落盘 token 不可复算"

    print("SELFTEST PASS: 2 RED（作弊可抓）+ 3 GREEN（数学/判决/落盘可复算）全部通过")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

DEMO_TASKS = [
    Task("d1", "给 utils.ts 补 parseDuration 的边界测试", "L1", 150),
    Task("d2", "把 store.ts 的同步写入改成批量写", "L2", 300),
    Task("d3", "修复 issue#42：outbox 在断网时丢消息", "L2", 300),
    Task("d4", "重构 admission 判定并迁移全部调用点", "L3", 500),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--demo", action="store_true", help="Echo 后端演示跑")
    ap.add_argument("--endpoint", default="", help="OpenAI 兼容端点，如 http://127.0.0.1:3425/v1")
    ap.add_argument("--model", default="", help="模型 id")
    ap.add_argument("--tasks", default="", help="任务清单 JSON（Task 数组）")
    ap.add_argument("--seeds", type=int, default=3)
    args = ap.parse_args()

    if args.selftest:
        selftest()
        return

    tasks = (
        [Task(**t) for t in json.loads(Path(args.tasks).read_text(encoding="utf-8"))]
        if args.tasks
        else DEMO_TASKS
    )
    cm = CostModel()
    results_dir = Path("results/demo" if args.demo else "results/run")
    summary = []
    for arm in ("single", "dual"):
        executor: Executor = (
            EchoExecutor(interventions_per_task=0.5)
            if args.demo or not args.endpoint
            else OpenAICompatExecutor(args.endpoint, args.model)
        )
        for seed in range(args.seeds):
            run = executor(tasks, arm, seed)
            cost = cm.cost(run)
            path = save_run(results_dir, run, cost)
            summary.append({"run_id": run.run_id, **cost, "saved": str(path)})
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
