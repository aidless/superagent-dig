# 生态学习笔记：四个同赛道开源项目源码级拆解

> 2026-11-10。目标：找跟 superagent-build 同赛道的近期开源项目，读源码到 file:line，把可借鉴机制摘出来，并对照我们自己的四件自研（治理/watchdog/开口阈值/成本账）找缺口。四个对象：microsoft/agent-governance-toolkit、OrchestratorInc/agent-orchestrator、ruvnet/APx、microsoft/agent-framework。

---

## 1. microsoft/agent-governance-toolkit（6.5k★，Rust 核心 + 多语言适配器）

**定位**：不是 agent 框架，是"agent 循环里的确定性决策引擎"。核心是一份 RFC2119 级别的规范（AGT = Agent Control Specification，钉在上游 ACS spec 0.4.0-alpha.4），运行时无状态、确定性、fail-closed 是三条硬不变量。

### 最值得抄的三个机制

**① 快照 + 干预点 + 判决三件套（spec §1/§4/§13）**
- Host 为每个**干预点**组装完整 JSON 快照（snapshot），干预点固定枚举 8 个：`agent_startup / input / pre_model_call / post_model_call / pre_tool_call / post_tool_call / output / agent_shutdown`。
- 运行时无状态：判决只由 manifest + snapshot + mode + dispatcher 结果决定，两次相同输入必须同判决（`spec/SPECIFICATION.md` §1.1 Invariants）。
- 判决空间闭合且归一化：策略可以表达 `warn/escalate`，引擎把 `warn → allow + warnings[]`、`escalate → deny + approval 块`（"可解除的 deny"，§13.1）。**这一招直接解决了我们 superagent-build 里"治理只 ALLOW|BLOCK 二值、没有升级人工审批通道"的缺口。**
- 还有 `transform` 判决：允许动作但**改写** policy target（比如脱敏后放行）——我们目前只有拒绝，没有"改写后放行"这个档位。
- 每次评估产出两个 SHA-256 身份：`input_identity`（评估前）与 `enforced_identity`（transform 后），escalation 审批绑定在 enforced_identity 上，"审批人同意的是真正要执行的那个动作"。

**② Rego/Cedar 双轨策略库（policy/lib/*.rego + policy/cedar-lib/*.cedar）**
- 同一套语义两份实现，每份策略带自己的测试文件（`budgets.rego` 98 行 + `budgets_test.rego` 98 行）。策略不是代码里的 if-else，是数据 + 可独立测试的规则集。
- 预算策略（`policy/lib/budgets.rego`）直接就是成本治理：四个计数器 `tool_call_count / token_count / elapsed_seconds / cost_usd`，任一超限 deny，**计数器缺失 fail-safe 放行、畸形计数器 fail-closed**——细节级诚实。
- 我们的 gov.py 是路径前缀树，AGT 是声明式策略语言。可借鉴点：把治理规则从代码抽成数据文件 + 每规则带测试，规则变更不动引擎。

**③ fail-closed 作为 ADR 而非习惯（docs/adr/0013）**
- 明文写"没有配置开关可以切到 fail-open"，理由：fail-open 的静默错误不可检测，fail-closed 的误伤会立刻暴露并驱动修复。
- 我们 superagent-build 的 fail-closed 是散在各处的实现习惯；AGT 把它写成不可协商的架构决策。**教训：关键决策要有 ADR 编号，防止未来某次"顺手改"悄悄引入 fail-open 路径。**

**④ manifest extends 的 SSRF 防护（spec §2.2，值得单独记）**
- 策略清单支持 `extends` 引 URL，防护写到病态级别：拒绝 loopback/RFC1918/云元数据段（169.254.169.254）、拒绝 `127.1`/`2130706433`/`0x7f000001` 等非规范字面量（用"fetcher 实际会连的解析结果"做判据）、IPv4-mapped IPv6 查内嵌 v4 地址、重定向逐跳复检。这种"用解析器同款逻辑算真实地址"的写法是我们写任何 URL 校验时的模板。

**与我们对照**：我们的 pre_write 治理 ≈ AGT 的 `pre_tool_call` 单点；缺 `escalate`（人工审批通道）、缺 `transform`（改写放行）、缺预算类策略（token/cost 上限直接 deny）。**AGT 证明"成本上限进治理判决"是一条被微软实践过的路**——这正好把我们的成本账（sweep_bridge）和治理（gov.py）缝起来。

---

## 2. OrchestratorInc/agent-orchestrator（14.4k★，Go，桌面端多 agent 监督台）

**定位**："Plan, run and supervise fleets of agents from task to merged PR"。backend 33.7 万行 Go，最有价值的是它把"监督"拆成了几个我此前没细分过的角色。

### 最值得抄的三个机制

**① sessionguard：写 pane 前重读状态的单一闸门（backend/internal/sessionguard/guard.go）**
- 不变量："每个对活跃会话 pane 的写入，必须在写入前一刻重读会话状态，拒绝会把消息落进只有用户才能操作的界面的写"。因为 runtime 会在粘贴后自动补 Enter，写进一个正卡在权限确认对话框的会话 = **替用户做了决定**，不可恢复。
- 抑制原因枚举到病态细致：`SuppressedUnknown`（读失败=未知=fail-closed，**故意设为 iota 零值，漏赋值永远不会伪装成成功发送**）、`SuppressedExited`（agent 进程退出后 pane 可能是交互 shell，写入会当 shell 命令执行）、`SuppressedStartupPending`（TUI 首信号前写入会吃掉 MCP 审批对话框）、`SuppressedComposerBusy`（composer 里有用户没发完的草稿，尾部 Enter 会把拼接文本发出去）。
- `InputLease`（先取租约→写→归还）消掉 check-then-write 竞态；会话变更"先关准入，再等已发租约排空"。
- **对照我们的缺口**：我们 Scheduler.tick 派发 deliver 时不检查票的当前状态，理论上存在"用户正跟 worker 同一票对话时调度器又投了 idle_timeout 消息"的同类竞态。**SessionGuard 的"写前重读 + 租约 + 零值 fail-closed"三件套是我们 I6 调度器下一个版本该搬的。**

**② autoreview：把"什么时候复审"变成带退避的状态机（backend/internal/autoreview/coordinator.go）**
- 周期 sweep（默认 1 分钟），每个 session 过 `sessionGate`：AutoReviewEnabled + KindWorker + 未终止 + ActivityIdle + 距上次活动 ≥ idleThreshold（默认 1 分钟）——**"worker 停手了才复审"，跟我们的 idle_timeout 同源但用途相反：他们用 idle 触发复审，我们用 idle 回收工位**。
- 同一 PR head 的失败自动复审重试上限 3 次（`autoReviewFailedRetryLimit=3`），超限 reason=`failed_same_sha_retry_limit`——复审风暴的熔断。每个跳过都有 reason 字符串（`no_pr / draft_pr / merged_pr / review_running / already_approved / changes_requested_same_sha ...`），可观测性是"每个否决都有名字"。
- 对照我们：watchdog 复审环有 max_repairs=1，但没有"同 head 失败退避"概念；AGT/AO 的 reason 命名法值得全库统一。

**③ reactions + observe：外部事件的幂等反应层（backend/internal/lifecycle/reactions.go + observe/observer.go）**
- SCM/Tracker 轮询观察 → `ApplyTrackerFacts` 反应：issue 终态→终止会话、新 bot 评论→一次性 nudge，nudge 用 `sendOnce + dedup 签名`（签名 = 评论 ID 串），重启后从持久化签名恢复去重。
- `cannotNudge` 入口守卫 + sessionguard JIT 复检双层防御；merge-conflict nudge 有意留窄豁免（needs-input 状态也要能通知），豁免的注释写明为什么。
- `StartTriggeredPollLoop`：tick + 触发通道双源，触发到达时 mid-poll 也会在结束后补一轮——避免"知道刚变了但还要等满一个 tick"。
- **对照**：我们的 observe_log 是纯数据记录，没有"反应"层。如果做"watchdog deviation 自动通知 + 只通知一次"这类闭环，AO 的 dedup 签名模式是现成模板。

**其他**：pricing/（模型价格目录同步 + 长上下文分段定价 `above272k` 行 + 用户自有价格覆盖）——商用 agent 产品里"成本显示"的完整数据结构样本。

---

## 3. ruvnet/APx（10★ 但方向极准：Agentic Power 产出率基准）

**定位**："agent 系统的马力"。`AP = 接受产出的参考人类工时 / 完整墙钟小时`，`APx = log2(AP)`。476 行 TS 核心完成全部验证/评分/比较，是四个项目里"每行都是设计决策"密度最高的。

### 最值得抄的四个机制

**① 分母纪律（CONTRACT.md）**
- 墙钟必须分解为五个不重叠段：`executionHours / retryHours / reviewHours / blockingWaitHours / otherHours`，和必须等于 wallHours；**所有等待、重试、复审、返工时间都留在分母里**。"检查算术一致性，不检查观察者有没有漏报"——只验能验的，不装全能。
- `agentRuntimeHours`（并行 worker 运行时之和）永远不能替代墙钟做分母——并行度不能伪装成效率。
- 人力单独记账：`humanReviewHours ≥ wallBreakdown.reviewHours`（并行评审可以让人时高于墙钟评审时间，永不能低于它）。

**② 信用门（evaluate() 的排除链，core.mjs:171）**
- 排序确定（slotId 字典序），五连排除：未接受 → 质量低于冻结阈值 → safety 非 pass → 缺 deliverableId/artifactHash → 缺 verified 判定或测试回执。
- **身份冲突即整 run 报废**：同一 deliverableId 映射到两个 artifactHash = 拒绝整个 run（alias chain 也逃不掉）；同 hash 多 ID 或同 ID 多次只记一次信用。
- safety fail 无论是否 accepted 都进 violations → 官方 AP/APx 置 null（INELIGIBLE），诊断值照给但标注 "diagnostic only"。**违规阻断的不是加分，是"官方分数"本身**——这个设计对我们 K2 校准直接可用：watchdog DEVIATION 事件的 run 应该是 INELIGIBLE 而不是低分。
- `certified: false` 永久硬编码；synthetic/pilot 标签由数据来源自动主导。

**③ 冻结清单 + 预登记（freezeManifest/validateManifest）**
- suite/baseline/evaluator/environment 四组件全部 SHA-256 冻结，taskSetHash 钉任务槽集合；measured baseline 要求 protocol 带预登记时间戳 `predeclaredAt`（`/^\d{4}-\d{2}-\d{2}T/` 强校验），评审人≠参与者（`reviewerId === participantId` 即 fail）。
- **对照任务②框架**：我们的 DESIGN.md 说"禁止换指标"，但没有机器可查的冻结。把任务配置做成 APx 式冻结清单（哈希+预登记时间戳），跑之前钉死，是实验纪律从文档落到数据的一步。

**④ 提升门（compareCampaigns，core.mjs:226）**
- ≥5 对独立 campaign 才做配对 bootstrap（2000 次、可复现种子 20260907、成对全 campaign 重采样），95% 区间下界严格 > 0 才谈 promotion。
- **人类工时不许倒退**：候选侧总 humanReviewHours > 父侧 = 直接记 regression——"速度提升不许藏在某 campaign 增加的人类辅助里"。逐任务验收/质量/安全三重非回归。
- 五对是最小实现门不是统计充分性保证，区间显式声明条件于冻结基线——诚实口径的写法模板。

---

## 4. microsoft/agent-framework（14.6k★，python/packages/core 的 _workflows + _harness）

**定位**：大厂编排框架。9.2k 行 `_harness/` + 17.3k 行 `_workflows/`，看点不在算法在工程形态。

### 最值得抄的三个机制

**① CheckpointStorage 协议 + 图签名（_workflows/_checkpoint.py）**
- checkpoint 绑定 `workflow_name + graph_signature_hash`（图定义哈希）而非实例 ID，**同一图定义的不同实例可共享恢复点**；图变了旧 checkpoint 语义上失效。
- Protocol 五方法（save/load/list_checkpoints/delete/get_latest），内存实现与持久实现同签名。
- 对照我们 I1 状态回灌：我们把票/线程整卡回灌进内存，但没有"引擎代码版本变了，旧状态还兼容吗"的概念。图签名哈希是给 SQLite 里的 kv 状态加版本戳的现成方案。

**② _harness 的工具审批中间件（_harness/_tool_approval.py，793 行）**
- 文件工具默认 `approval_mode="always_require"`：模型每次提议写/删/替换都转成 `function_approval_request`，宿主不回 `function_approval_response` 工具就不执行——**审批是执行流的硬前置，不是建议**。
- 审批规则粒度到 `tool` / `tool_with_arguments`（参数级精确匹配，None=工具级规则；无参调用的参数规则存 `{}` 防止变成通配）。
- 对照我们：pre_write 是路径级，AF 给了参数级模板；且"默认全审、显式降级"的方向与我们的 fail-closed 一致，但我们的降级（whitelist 内免审）没有规则对象化。

**③ AgentFileStore 的符号链接防御（_harness/_file_access.py:1340 附近）**
- 路径解析全程拒绝 symlink/reparse point：探测 + 打开两段，POSIX 侧 open 带 `O_NOFOLLOW` 让内核在"探测后、打开前叶子被换成 symlink"时拒绝（TOCTOU 收口到内核层）；Windows 侧诚实声明 best-effort 覆盖不了对抗式父目录交换。
- 类 docstring 明说：这是单租户/协作租户设计，**不是对抗共享根目录敌对进程的沙箱**——能力边界的诚实声明写在类型上。
- **对照我们**：gov.py 是前缀树路径匹配，对 symlink 逃逸（`allowed/../../etc` 或 allowed 内的 symlink 指出去）目前没有防护。realpath 解析 + O_NOFOLLOW 是补丁方向。

**④ BackgroundAgentsProvider（_harness/_background_agents.py）**
- 父 agent 把工作委派给后台子 agent：每任务独立 session 并发跑、可等完成、可取结果、可释放会话（release_session/_drain_runtime）——微软把"多 worker"做成了 context provider 而不是独立编排层，轻得多。

---

## 5. 综合结论：对 superagent-build 的六条升级方向

按"抄了立刻有用"排序：

1. **治理加 escalate 档**（AGT）：BLOCK/ALLOW 之外加"可解除的 BLOCK + 审批块"，审批绑定写内容哈希。治理从二值变三值，人工介入有了正式通道，跟 proactivity 的"打扰用户"共享同一出口。
2. **预算策略进治理**（AGT budgets.rego）：票级 token/cost 上限直接做成治理规则——超限 deny 带保留 reason（`budget_tokens_exceeded`）。成本账从"事后度量"升级为"事中硬闸"。
3. **Scheduler 写前重读 + 租约**（AO sessionguard）：deliver 前重读票状态、租约防竞态、SuppressedUnknown=零值 fail-closed。修 I6 派发与用户消息的竞态。
4. **K2 校准对齐 APx 信用门**：watchdog DEVIATION 的 run 判 INELIGIBLE（官方 precision 置 null 只给诊断值）；任务配置冻结清单 + 预登记时间戳。实验纪律从 DESIGN.md 文字变成 schema 校验。
5. **复审熔断 + reason 命名法**（AO autoreview）：同 mandate 失败复审上限 3 次 + 每个否决都有 reason 字符串；全库 ruling/notices 的 reason 统一成可枚举词表。
6. **路径治理补 symlink 逃逸**（AF）：gov.py 匹配前 realpath 归一；Linux 写走 O_NOFOLLOW。fail-closed ADR 化（补 docs/adr/0001）。

**方向性判断**：四个项目零重叠地印证了同一件事——2026 年这个赛道的竞争面是**治理与监督的确定性**（AGT 的规范工程、AO 的守卫枚举、APx 的信用门、AF 的审批硬前置），模型能力反而是公共底座。superagent-build 的 fail-closed 治理 + watchdog + 校准路线踩在主航道上；差距在"规范的机器可读化"（AGT 用 RFC2119+schema+一致性语料库锁语义，我们用中文 README）与"信用门的严肃性"（APx 连 `certified:false` 都硬编码）。
