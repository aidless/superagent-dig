# dsh-superagent-prototype 源码拆解总报告（任务①）

> 仓库：deepseek-ai/dsh-superagent-prototype（~12.4k 行 TS，dsh bundle 插件，MIT）。
> 本报告由三路独立子代理拆解合成（准入机制 / 健康与异步执行 / 订阅与上下文），
> 全部结论带实测 file:line 引用。三个子代理的完整报告见文末"来源"。

## 1. 系统真实架构（一张图说清）

```
Source（GitHub/飞书）→ Trigger → Intake（去重/范围过滤）
  → Eval：gate（规则，policy.ts）→ assessment（模型研判，主 Session 内）
  → accept → Mandate → Ticket（含订阅线程）→ Assignment → worker-<ticketId> 会话
  → Report（ready/blocked/dropped）→ Outbox → 回原对话
全程：单一 SQLite（WAL+FULL）为唯一真相源；Router 主 Session 只路由不执行。
```

## 2. 最扎实的三块（值得直接抄）

**a) 两级准入确实成立，且实现得比文章严谨。**
第一级是确定性 gate：名单内+@→explicit（src/policy.ts:197）、admission 三格表
（src/admission.ts:9-13，按 Source×接收范围一行）、follow-up 判定（policy.ts:206-207）、
bot 门（policy.ts:171-188）、observe 模式只记 would_accept 不接单（policy.ts:55-57）。
第二级是主 Session 内的模型研判（非独立调用）：prompt 三事实前置（prompts/admission.md:17），
输出硬校验——规则覆盖恰好一次、accept 需 matched 规则+intent_evidence
（src/policy.ts:40-61 assessmentOutcome）。研判上下文是**有界**的：前 5 条+trigger 前后各 5 条
（src/sources/admission-context.ts:9），trigger 全文 4000 字符、近邻消息 1200 截断
（src/app/assessment.ts:37,195-205）。

**b) 可靠性底座是教科书级的。**
先存后做：receipts 幂等落库后才 enqueue 回复效果（src/app/outbox.ts:27-32），
任意点崩溃不丢回复；outbox 保序+退避重试 5s→300s，唯二合法放弃是 channelGone/channelIgnored
（outbox.ts:39-51）；agent 侧 messageId 幂等+投递后 flush 才 ack（src/app/delivery.ts:16,61-64,84）；
单一 SQLite WAL+synchronous=FULL（src/app/store.ts:64）；stage 是纯函数重算而非存储状态机
（src/core/stage.ts:9-21）；崩溃恢复 = WAL+效果队列残留行续跑+executor.resume+14 天回看
（src/sources/poll.ts:6）。

**c) "Router 少说话"是 prompt 层纪律，写得非常狠。**
不等待（never wait with sleep/polling，prompts/orchestrator.md）、不越权读代码和日志
（orchestrator.md）、不改写用户请求（prompts/dispatch.md "Do not paraphrase a short request"）、
IM 回复 100 词硬顶（prompts/replies.md）、进度不上浮（prompts/shared.md）。

## 3. 文章核心卖点的兑现度（差距清单）

| 文章卖点 | 代码现实 | 判定 |
|---|---|---|
| 两级过滤（规则→语义） | gate + 主 Session 内研判，硬校验齐全 | **兑现** |
| Watchdog 看任务跑偏 | 全库 0 命中 "watchdog"；只有被动健康投影（src/app/retry-health.ts 只投影预算重试）+ 效果队列 3 次标 error（effects.ts:151-160）+ 自报式 attention。"跑偏"靠 worker 自觉上报 | **未兑现** |
| Router 靠订阅 Thread 感知冲突 | Router 根本不订阅任何 Thread（订阅者是 ticket，src/core/model.ts:164）；冲突只覆盖消息归属（handback/attach，tickets.ts:185-195）；**两 worker 改同一文件无任何检测**，只在 PR 层以 merge conflict 暴露 | **未兑现（退化为消息路由仲裁）** |
| 上下文与执行环境解耦（大脑云端/记录独立/代码本地） | 三者全绑定单机单进程：同一 SQLite+同一执行根+同一 dsh 进程；"记录独立存"实为 Engine 独占写的 SQLite 表 | **未兑现（单机版）** |
| 长期记忆/经验沉淀 | 全库无 embedding/vector/记忆存储；memory.ts 是测试用内存仓库（src/core/memory.ts:1）；correlated 字段只写不读（intake.ts:37 写入，零消费方） | **未兑现（存而不读半成品）** |
| 多 Worker 并行协作 | maxWorkers 槽位排队（dsh.ts:230），worker 间零直连，全靠 Router 星型中转（tickets.ts:395-401 note）；事件总线 12 个事件里 3 个有消费者 | **弱化兑现** |

另有两个隐藏差距：**冷却/静默时段不存在**（两级过滤无时间维度，全库 grep 零命中）；
**语义研判不是独立模型调用**（settings.eval.model 字段存在但链路上没有按它选模的独立调用面）。

## 4. 对"值不值得学"的判定

- **架构骨架值得学**：准入两级化、Mandate 复审（policy 版本变更丢弃进行中 Eval 并重排，
  evals.ts:218-221）、outbox 三角保证、有界上下文分页补读——这四件是可以直接搬走的模式。
- **可靠性实现值得抄**：先存后做 + 幂等 receipt + 纯函数 stage 重算，比多数自研编排器扎实。
- **不要指望拿到"任务跑偏检测"**：那是文章的前瞻方向，不是这份代码的能力。
  想要 Watchdog，得自己造——恰好任务③的治理工具和任务⑤的开口阈值模型是往这个方向走的。
- **README:5 的自我声明是诚实的**："research code, not production-ready"，
  拆解证据支持这个定性。

## 来源

- 子代理报告 1/3：Admission 准入/两级过滤（完整 file:line 清单，本会话消息存档）
- 子代理报告 2/3：Watchdog/健康/异步执行/可靠投递（同上）
- 子代理报告 3/3：Thread Subscription/Router-Worker/上下文/差距清单（同上）
- 仓库本体：superagent-dig/dsh-superagent-prototype/（docs/000-008 编号文档按序可读）
