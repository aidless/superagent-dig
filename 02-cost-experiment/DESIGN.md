# 多代理 vs 单循环：成本盈亏平衡实验设计

## 1. 要回答的问题

"Super-agent 用 token 换人的注意力"——这句话目前全行业都是定性拍脑袋。
本实验把兑换率量出来：

> **在什么任务复杂度区间，双层架构（Router + Worker）的总成本低于单 Agent Loop？**

总成本定义为（钱与注意力折成同一币种：人民币/秒）：

```
Cost(方案) = TotalTokens × PricePerToken + HumanInterventions × ValuePerIntervention
```

其中 `ValuePerIntervention` = 被打断的分钟数 × 人的小时价 / 60。
这就是盈亏平衡的两种读法：

- **Breakeven token price** `p* = ΔN_int × V_int / ΔT`：token 单价低于 p* 时双层划算。
- **Breakeven attention value** `C* = ΔT × p / ΔN_int`：人的注意力价值高于 C* 时双层划算。

## 2. 实验变量

| 变量 | 取值 | 说明 |
|---|---|---|
| 架构（自变量） | `single` / `dual` | single = 一个会话顺序吃完全部任务；dual = Router 派发 + 每任务独立 Worker 会话 |
| 任务批次 | 8/16/32 个真实 repo 任务 | 从真实仓库 issue/重构清单里取，**不用人造玩具任务**（人造任务的协调成本不真实） |
| 任务复杂度 | 按"跨文件数 × 依赖任务数"分档 L1/L2/L3 | 盈亏平衡曲线以复杂度为 x 轴 |
| seeds | 每档 ≥3 次重复 | 温度/采样随机性，配对比较 |
| 模型 | 通过 OpenAI 兼容端点（本机 Magpie 网关 127.0.0.1:3425 或云端通道） | 框架与模型解耦 |

## 3. 测什么（每 run 的原始记录）

1. **token 四项**：prompt / completion / cached / reasoning（逐调用累加，从响应 usage 字段取，不估算）。
2. **墙钟**：run 起止，另记每个任务的独立耗时（并行度收益要单独归因）。
3. **人工介入次数 N_int**：预注册判定标准（这是防作弊的关键）——
   - 算一次：请求人批准写操作 / 请求缺失信息 / 自报"无法继续"；
   - 不算：纯进度汇报（无决策）。
   - 判定器必须落盘（`interventions.jsonl`），事后可复核。
4. **返工次数 R**：任务产出未过验收（测试/断言）后被重做的次数。
5. **协调开销**（仅 dual）：Router 每轮 prompt token 单独记账——这是双层的纯税。

## 4. 判决规则（预注册，跑之前写死）

- 主指标：配对差 `ΔCost = Cost(dual) − Cost(single)`，按任务档分层，配对 bootstrap 95% CI。
- 次指标：ΔN_int、Δ墙钟、返工率。
- 三档判决：
  - **DUAL_WINS**：ΔCost < 0 且 CI 上界 < 0；
  - **SINGLE_WINS**：ΔCost > 0 且 CI 下界 > 0；
  - **INCONCLUSIVE**：CI 跨零（样本不够，报 MDE 而不是硬下结论）。
- 禁止换指标、禁止跑完再挑有利分档。

## 5. 框架怎么接真实 harness（待扩展项）

`framework.py` 的 `Executor` 抽象有三个实现位：

1. `EchoExecutor`（已实现）：确定性回声后端，用于框架自检和管线调试，零成本；
2. `OpenAICompatExecutor`（已实现）：直打 OpenAI 兼容端点，记录 usage；
3. **真实 harness 适配器（未实现，后续工作）**：
   - single 臂：驱动一个单会话 agent（codex/claude CLI 无头模式）顺序执行；
   - dual 臂：驱动 dsh 会话（Lead 派发子代理），用 dsh 的 session 事件记录取真实 token 数。
   - 关键纪律：两臂除架构外**其他一切对齐**（同一模型、同一 prompt 模板、同一验收标准）。

## 6. 执行纪律（对应工作区 AGENTS.md）

- 实验一律上云跑（AGENTS.md 第 17 条）：真实 sweep 用 AMD 实例 / SeetaCloud / Kaggle，
  本机只允许框架 smoke（<1 分钟）；
- 断言走 RED→GREEN：`framework.py --selftest` 必须先注入"作弊 run"（干预判定漏记、
  token 用估算值）证明门会红，再用合规 run 证明放行；
- 每个数字落盘 `results/<run_id>/metrics.json`，可复算；
- 报告里的数必须能从落盘证据重算，重算不一致 = 阻塞（铁律 1）。

## 7. 预期产出

- `results/` 下的逐 run 原始记录 + 聚合表；
- 盈亏平衡曲线（x=任务复杂度档，y=p* 和 C*）；
- 一页结论：给出"任务复杂度超过哪一档、人的注意力价值超过哪个数，双层才值得上"的可引用答案。
