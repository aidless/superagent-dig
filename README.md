# superagent-dig — dsh-superagent-prototype 源码拆解报告

> 对 [deepseek-ai/dsh-superagent-prototype](https://github.com/deepseek-ai/dsh-superagent-prototype)（22k★，~12.4k 行 TS）的逐文件拆解：宣传稿五卖点兑现度表、可靠性底座分析、多代理成本盈亏模型、通用治理工具、主动开口阈值建模。
> 全部结论带实测 `file:line` 引用。自研实现见姊妹仓库 [aidless/superagent-build](https://github.com/aidless/superagent-build)。

## 五份交付

| # | 文件 | 内容 |
|---|---|---|
| ① | [01-prototype-dissection.md](01-prototype-dissection.md) | 总报告：真实架构图、最扎实三块（两级准入/outbox 三角/Router 纪律）、**五卖点兑现度表**（两级过滤✅，Watchdog/订阅冲突/上下文解耦/长期记忆/多 worker 协作 ❌）、隐藏差距 |
| ② | [02-cost-experiment/](02-cost-experiment/DESIGN.md) | 多代理 vs 单循环成本实验设计 + framework.py：Cost=T×p+N_int×V_int、盈亏平衡 p\*/C\*、配对 bootstrap、三档判决（含可复算 selftest） |
| ③ | [03-governance-tool/gov.py](03-governance-tool/gov.py) | 通用文件治理 CLI：claim/check/release/conflicts/ruling，写前校验未声明即 BLOCK，append-only 判决流（RED→GREEN selftest 内嵌） |
| ④ | [04-context-decoupling.md](04-context-decoupling.md) | 上下文解耦两条路线对比：记录外置（真相源）vs KV 虚拟化（加速层），正交非竞争，生产栈=A 真相 + B 加速 + A→B 失效下发 |
| ⑤ | [05-proactivity-modeling.md](05-proactivity-modeling.md) | 主动开口阈值建模：u(e)/τ 衰减/三动作 Speak-Wait-Drop、临界确证度 p\*(t)、三条可证伪预测 |

## 核心结论

原型宣传的五卖点里，**只有两级过滤真的兑现**；Watchdog、文件级冲突治理、上下文解耦、长期记忆四件事在 12.4k 行源码里全库 0 命中——这正是 [superagent-build](https://github.com/aidless/superagent-build) 要自造的四个洞。

## 许可

MIT

## 06 生态学习（2026-11-10）

- [06-ecosystem-learning-NOTES.md](06-ecosystem-learning-NOTES.md)：四个同赛道开源项目源码级拆解（microsoft/agent-governance-toolkit、OrchestratorInc/agent-orchestrator、ruvnet/APx、microsoft/agent-framework），四项目零重叠印证赛道竞争面 = 治理与监督的确定性；综合出 superagent-build 六条升级方向（escalate 档/预算进治理/派发租约/信用门/复审熔断/symlink 加固）。
