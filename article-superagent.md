# 我不看好 Super Agent？我把它拆了，还照着造了一个

> 上个月看到一条短视频，博主对 Super Agent 的判断是"设计过复杂、违背现实逻辑、不看好"。我把 DeepSeek 团队开源的原型（dsh-superagent-prototype，22k★，约 1.24 万行 TypeScript）逐文件拆了一遍，然后照着它的架构，用纯 Python 标准库造了一个自己的版本，接真网关跑出了真实的成本数据。这篇文章是拆解结论 + 自研过程 + 真跑数字的完整记录。

## 一、先说拆解：宣传稿的五个卖点，只有一个真的兑现了

我把原型源码 clone 下来，从 admission 到 outbox 到 store 逐文件读，每个结论都落到具体的 file:line。结果做成一张"兑现度表"：

| 宣传卖点 | 源码现实 | 证据 |
|---|---|---|
| 两级过滤（规则门 + 模型研判） | ✅ **兑现，且严谨** | `policy.ts:197` 显式直通 / `admission.ts:9-13` 三格路由 / `assessment.ts:21-34` 研判 schema 硬校验 |
| Watchdog 盯着 worker 跑偏 | ❌ **全库 0 命中** | `grep -r watchdog` 无结果；只有 `retry-health.ts` 被动重试 + `effects.ts:151-160` 三次失败标 error |
| Router 订阅 Thread、感知冲突 | ❌ 订阅者根本不是 Router | `model.ts:164` 订阅者是 ticket；Router 只被动收 relay notice（`notices.ts:102,121`） |
| 上下文与执行环境解耦 | ❌ 三者绑死单机单进程 | SQLite WAL 是唯一真相源，无远程 KV、无会话外置 |
| 长期记忆 | ❌ 是个测试桩 | `memory.ts:1` 是测试用内存仓库；`intake.ts:37` 的 correlated 字段只写不读 |

顺带两个隐藏发现：

1. **没有冷却/静默时段机制**——一个高产出 Router 理论上可以无限打扰用户。
2. **研判不是独立模型调用**——`settings.eval.model` 字段存在，但没有任何调用面。所谓"模型研判"发生在主 Session 里，跟用户共享同一个上下文窗口。

所以那句"设计过复杂"只对了一半：它的可靠性底座（先存后做的 outbox 三角：`outbox.ts:27-32` 先收幂等回执才入队、5s→300s 指数退避、SQLite `synchronous=FULL`）写得相当扎实；但**宣传稿里最有想象力的四件事，一行代码都没有**。

## 二、造一个：把没做的四件事补上

光拆不造是纸上谈兵。我用纯 Python 标准库（零第三方依赖）实现了同样的架构骨架，然后把原型缺的四块补齐，这就是 [superagent-build](https://github.com/aidless/superagent-build)（PyPI 0.2.0 在架）：

**1. 文件级治理（写前拦截，不是事后扯皮）**
原型里两个 worker 改同一个文件，冲突要到 PR 层的 merge conflict 才暴露。我的版本在引擎里挂了 `pre_write` 治理点：worker 申报写作用域（claim），写前校验（check），未声明的写直接 BLOCK 且**不落盘**，判决进 append-only 的 ruling 日志。BLOCK 是确定性的，不依赖模型自觉——挑逗性输入试过，真的拦得住。

**2. Watchdog 复审环（承诺对齐，不是只看退出码）**
worker 交付报告后，规则层先查：报告里申报的 touched_paths 是否越出 mandate 边界、是否命中 forbid 清单、declares_extra_work 是否撒谎；再过一层 LLM judge 做语义审查（判"答非所问"）。第一次 deviation 不终止票，违规理由拼成修复指令重投，给一次自纠机会；复审用尽才终止票并落判决。

**3. 主动开口阈值（把"什么时候打扰用户"变成可校准的参数）**
每个候选事件带效用 u(e)、时间衰减 exp(-λt)、置信度 p，三选一动作 Speak/Wait/Drop。开口条件是 `p ≥ p*`，其中解析解 `p* = (c_false + c_speak) / (u_eff + c_false)`——把误报成本、沉默成本、事件效用折成一个随时间变化的临界确证度。参数不拍脑袋：真跑攒 observe 事件，校准器在网格里选"精度达标且样本量够"的参数组，**带样本量门（n_speak≥3）防小样本刷分**——一个开口恰好对了就自称 precision 1.0 的候选组会被直接挡掉。

**4. 成本账（双臂对比，不是"感觉更快"）**
成本模型 `Cost = T×p + N_int×V_int`：token 单价乘 token 量，加介入次数乘每次介入的价值。盈亏平衡点 `p* = ΔN×V/ΔT`——治理层多花的 token 税，换少打扰你几次，这笔账能不能算平，是能算出来的。

## 三、真跑数字：8 runs、真实 usage、双臂对比

架构跑通了不够，接了 Magpie 网关（deepseek-v4.1-flash）真跑：

**双臂 sweep（8 runs，全真实 usage 计量）**：single 臂 = 裸 worker 直调；dual 臂 = 完整治理链（LLM 研判准入 + watchdog 复审 + 治理 + 开口判定）。结果：dual 臂每 run 多花 **1654 tokens**（研判+复审的税），换来**少 4 次人工介入**，折出盈亏平衡价 **p\* = 0.0121 元/token**——只要你的模型单价低于这个数，治理层的税就值得交。四个 cost_delta 全负，verdict = **DUAL_WINS**。

**真校准（12 个真任务：6 合规 + 3 挑逗 + 3 闲聊，独立线程）**：14 次调用全部成功，真模型在研判层幻觉过一次规则名，被 fail-closed 拦下（`hallucinated_rule`）——三铁律在真实模型上生效过，不是只在测试里。12 个 observe 事件喂进校准器，出参数 (c_false=0.5, c_speak=0.2, λ=0.05)。

**也要诚实**：这批事件里 confidence 全部同质（0.533）——因为开口事件都绑在 worker 报告之后，而 watchdog 全部放行，证据向量一模一样。所以当前校准是"无冲突数据上的方向性验证"，要出真正有区分度的校准，得等 wait/drop 侧事件和坏开口样本积累起来。这是下一阶段的事。

## 四、回头看那条短视频

博主的"不看好"错押了论据：复杂度不是问题，复杂度**有没有人买单**才是问题——原型的答案是用两级准入替你把关，这个设计成立。但博主有一个判断我完全同意：**消费级还早**。主动性边界、权限粒度、成本三关，这版原型一个都没过（我的版本过了成本这关的"算账"，但样本还小）。

真正值钱的结论是：2026 年 9 到 10 月，Meta、OpenAI、Google、Anthropic、DeepSeek 五家在六周内集体转向多代理编排，**瓶颈已经从模型能力转移到人的注意力管理**。谁把"少打扰、可审计、账算得平"这三件事做成产品能力，谁就赢下一程。

---

**仓库**：[aidless/superagent-build](https://github.com/aidless/superagent-build)（自研内核，203 项检查，纯标准库）· [aidless/superagent-dig](https://github.com/aidless/superagent-dig)（拆解报告五件套，全部带 file:line 举证）· [PyPI superagent-build 0.2.0](https://pypi.org/project/superagent-build/0.2.0/)
