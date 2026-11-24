# PlotPilot 架构、工程能力与可迁移性

这份文档记录 PlotPilot 在当前代码状态下已经落地的机制、兼容层留下的分叉、测试可以证明的边界，以及迁移到写文工作台时应该保留或重做的部分。文档以源码和测试为准，目标设计与实际运行路径分开记录。

## 1. 结论摘要

PlotPilot 已经形成一个面向长篇创作的生产系统，组合了小说状态、章节生产、AI 调用、记忆装配、章后同步、质量治理、持久化和实时工作台投影。当前最值得迁移的工程资产有六类。

1. **有提交边界的章节管线**。每一步都返回结构化结果，正文生成、章节保存、章后同步和收尾具有不同的中断处理策略。
2. **AI 调用控制平面**。提示词版本、变量快照、调用会话、生成尝试、采纳决策和提交结果被拆开管理，支持人工审阅和自动驾驶暂停。
3. **预算化上下文装配**。上下文按 T0 到 T3 分层，在总预算、最低保障和阶段指令下裁剪。
4. **章后反馈闭环**。章节保存后会把正文转成桥接资产、叙事同步、向量、伏笔、三元组、因果边、人物状态和叙事债务，供下一章继续使用。
5. **单写者持久化与投影状态**。SQLite 的常规写入经过队列和唯一消费者，运行时状态通过共享状态、状态 API 和 SSE 投影给前端。
6. **可替换的题材管线和节点注册表**。基础管线提供默认步骤，题材管线覆写少量步骤，DAG 节点通过注册表和元数据接入。

当前最大的结构性问题是生产路径仍有兼容分叉。`EngineDaemon` 是推荐入口，同时保留 `legacy_writing_delegate`、旧 `AutopilotDaemon`、`AutoNovelGenerationWorkflow` 和 StoryPipeline。迁移时应该先抽取稳定协议，再逐步减少旧路径。

## 2. 实际架构地图

```text
FastAPI / 工作台请求
    -> application 用例与路由服务
    -> EngineDaemon / StoryPipelineRunner
    -> daemon_loop + novel_lifecycle
    -> planning / writing / auditing delegate
    -> BaseStoryPipeline 或 legacy 节拍路径
    -> ContextBuilder / ContextBudgetAllocator / Variable Hub
    -> AIInvocationGateway / CPMS Prompt Registry / LLM Provider
    -> 章节保存与共享状态投影
    -> ChapterAftermathPipeline
    -> 叙事同步、向量、伏笔、图谱、人物状态、债务、质量护栏
    -> SQLite 单写者、文件或向量存储、SSE / status 工作台
```

| 层 | 当前职责 | 可迁移判断 |
| --- | --- | --- |
| `domain/` | Novel、Chapter、StoryNode、CharacterState、NarrativeDebt、知识和记忆模型 | 保留领域概念和不变量，删去 PlotPilot 特有字段后迁移 |
| `application/` | 用例编排、上下文、AI Invocation、DAG、章后同步、治理 | 是迁移的主资产，优先抽取接口和 DTO |
| `engine/` | 守护进程、章节生产管线、恢复策略、题材扩展、质量护栏 | 保留管线内核，重写宿主和进程管理 |
| `infrastructure/` | SQLite、Prompt Packages、Provider、向量库、导出 | 保留端口和约束，替换存储实现 |
| `interfaces/` | FastAPI、依赖注入、运行生命周期、SSE 路由 | 迁移契约和状态投影，重新组织依赖容器 |
| `frontend/` | Vue、Pinia、Naive UI、ECharts、Tauri 工作台 | 保留事件模型和重连策略，重新设计工作台信息架构 |

## 3. 章节生产主链路

### 3.1 守护进程和状态路由

当前推荐入口是 `EngineDaemon`。它把 `StoryPipelineRunner` 暴露为兼容代理，实际运行由 `DaemonHostMixin`、通用循环和生命周期路由组成。`StoryPipelineRunner` 同时继承 `DaemonHostMixin` 与 `BaseStoryPipeline`，因此既可以被守护进程驱动，也可以直接运行章节管线。

每次 daemon tick 的实际顺序如下。

1. `run_daemon_loop()` 写入心跳，检查熔断器，读取数据库和共享状态中的活跃小说。
2. `novel_lifecycle.process_novel()` 先运行 `AutopilotRecoveryPolicy`，清理中断态和过期 invocation。
3. 根据 `Novel.current_stage` 路由到宏观规划、幕规划、写作、审计或人工审阅等待态。
4. 写作阶段由环境变量或宿主配置选择 StoryPipeline 或 legacy 节拍路径。
5. 处理结束后合并数据库状态，记录成功或连续失败次数。连续失败达到三次时进入 `AutopilotStatus.ERROR`。

源码证据见 [`engine_daemon.py`](../engine/runtime/engine_daemon.py)、[`daemon_loop.py`](../engine/runtime/daemon_loop.py)、[`novel_lifecycle.py`](../engine/runtime/novel_lifecycle.py)、[`runner.py`](../engine/runtime/runner.py)。

### 3.2 Novel 状态机

`NovelStage` 目前包含 `macro_planning`、`act_planning`、`writing`、`auditing`、`paused_for_review` 和 `completed`，并保留 `planning`、`reviewing` 兼容值。`Novel` 还记录当前幕、章节、节拍、运行 ID、活动管线步骤、最后稳定阶段、审计快照和章节后处理状态。

迁移到写文工作台时，应把这些字段拆成三类。

| 类型 | 示例 | 工作台用途 |
| --- | --- | --- |
| 业务状态 | 当前幕、章节、节拍、章节状态 | 决定下一步写作目标 |
| 运行状态 | `active_pipeline_step`、运行 ID、连续错误数 | 恢复、取消、重试和故障解释 |
| 投影状态 | 审计分数、伏笔同步标记、质量问题 | 面板展示和人工决策 |

三类字段适合保持独立。工作台需要能够区分“业务没有推进”和“运行正在生成但还没有提交”。

源码证据见 [`novel.py`](../domain/novel/entities/novel.py) 与 [`autopilot_recovery_policy.py`](../application/engine/services/autopilot_recovery_policy.py)。

### 3.3 StoryPipeline 的真实步骤

`BaseStoryPipeline.run_chapter()` 的代码顺序是：

1. 定位下一章。
2. 准备叙事治理预算。
3. 准备章节执行剧本。
4. 组装上下文。
5. 调用 Composer 生成整章正文。
6. 运行内容策略验证。
7. 保存章节。
8. 运行文风审计。
9. 执行章后管线。
10. 计算张力。
11. 收尾并推进小说状态。

项目文档把它称为“十步管线”，原因是治理准备被视为前置子步骤。代码实际有十个 `_step_*` 主步骤，加上一个 `prepare_governance` 前置阶段。这个差异要在迁移文档和运行遥测中统一，否则前端显示的步骤数会与恢复记录不一致。

`StepResult` 表达通过、跳过和失败，`PipelineContext` 是步骤间的可变数据载体，`PipelineResult` 是最终结果。步骤依赖通过 `ctx.inject()` 注入，降低了构造函数长度，也使测试可以替换局部依赖。

源码证据见 [`base.py`](../engine/pipeline/base.py)、[`context.py`](../engine/pipeline/context.py)、[`steps.py`](../engine/pipeline/steps.py)。

### 3.4 提交边界和中断恢复

`recovery.py` 为步骤声明 `stable` 或 `transient`。章节计划可以重试，正文生成中断时丢弃临时预览，章节保存完成后保留结果，章后同步和张力分数属于稳定阶段。

这套设计的迁移价值在于它把“能否重试”从异常处理代码中抽成了数据。写文工作台可以用同一份边界定义实现暂停、恢复、重试和状态解释。

需要注意两个现状。内容策略验证、文风审计、章后管线和张力失败时，主管线会记录 warning 并继续。`prepare_governance` 失败时也可能被标记为跳过后继续。是否阻断应该由工作台策略显式决定，不能从日志级别推断。

源码证据见 [`recovery.py`](../engine/pipeline/recovery.py) 和 [`base.py`](../engine/pipeline/base.py)。

## 4. AI 调用控制平面

### 4.1 统一生命周期

AI 调用由 `AIInvocationGateway` 编排，业务调用方提交 `InvocationRequest`，系统按以下顺序处理。

```text
InvocationRequest
    -> InvocationSpec
    -> context resolve
    -> VariablePlan
    -> PromptSnapshot
    -> InvocationSession
    -> InvocationAttempt
    -> AdoptionDecision
    -> AdoptionCommit
    -> continuation
```

`InvocationPolicy` 支持直接执行、调用前审阅、调用后审阅、全交互、可用时交互和自动驾驶暂停。`InvocationSessionStatus` 记录从请求、变量解析、提示词编译到生成、采纳、提交、阻塞、失败和取消的全过程。

这一层最适合作为写文工作台的 AI 基础协议。它把“生成一次文本”和“把生成结果写入项目状态”分开，人工可以审阅正文、提示词版本和变量输出，再决定是否提交。

源码证据见 [`dtos.py`](../application/ai_invocation/dtos.py)、[`gateway.py`](../application/ai_invocation/gateway.py) 和 [`services.py`](../application/ai_invocation/services.py)。

### 4.2 Prompt、变量和版本

Prompt Registry 维护节点、活动版本、系统模板、用户模板和变量 schema。`CPMSPromptAssembler` 只读取 Registry 和模板引擎，按 `VariablePlan` 渲染变量，然后生成模板哈希、组合哈希、渲染哈希和变量快照哈希。

Variable Hub 负责把变量绑定到 `scope`、`stage`、来源、默认值、路径投影和渲染模式。它还保存变量 lineage，使工作台可以解释某个提示词变量来自显式输入、默认值还是某个上下文快照。

迁移时要保留以下字段。

| 字段 | 作用 |
| --- | --- |
| `node_key` 与版本 ID | 定位使用了哪个能力和哪一版模板 |
| `variable_snapshot_hash` | 复现当次上下文变量 |
| `rendered_prompt_hash` | 复现最终发送给模型的提示词 |
| `required_missing` | 阻止变量缺失的调用 |
| `continuation_handler_key` | 采纳后回到哪个业务流程 |
| `idempotency_key` | 防止重复提交副作用 |

源码证据见 [`prompt_assembler.py`](../application/ai_invocation/prompt_assembler.py)、[`variable_hub.py`](../application/ai_invocation/variable_hub.py)、[`prompt_registry.py`](../infrastructure/ai/prompt_registry.py)。

### 4.3 已落地与待验证的持久化边界

自动驾驶工厂会为 invocation spec、变量、session、attempt 和 adoption repository 注入 SQLite 实现。`AutopilotInvocationOrchestrator._save_result()` 会把 session、attempt、decision 和 commit 写入对应仓储。

同时，`InvocationSessionService`、`AttemptService`、`AdoptionService` 和 `AdoptionCommitService` 本身仍保存内存字典。当前检查到的创建路径没有把 SQLite 记录重新水合为这些内存服务对象。迁移前应补一条重启恢复测试，确认服务重启后处于 `awaiting_acceptance` 或 `awaiting_commit` 的会话仍可继续。

源码证据见 [`factory.py`](../application/ai_invocation/autopilot/factory.py)、[`orchestrator.py`](../application/ai_invocation/autopilot/orchestrator.py) 和 [`sqlite_ai_invocation_repository.py`](../infrastructure/persistence/database/sqlite_ai_invocation_repository.py)。

## 5. 上下文、记忆与叙事阶段

### 5.1 四级预算

`ContextBudgetAllocator` 把上下文槽位分为四级。当前默认比例为 T0 20%、T1 30%、T2 35%、T3 15%，并对 T0 设置最多占总预算 40% 的保护。T3 有至少 5% 的最低保障，用于保留跨幕向量记忆。

| 层级 | 当前内容 | 写文工作台迁移建议 |
| --- | --- | --- |
| T0 强制 | FACT_LOCK、主线锚点、人物锚点、叙事合同、必要治理预算 | 保留，内容必须短、稳定、可解释 |
| T1 可压缩 | 伤疤、执念、叙事债务、因果链、已完成节拍 | 保留摘要和优先级，支持按章节裁剪 |
| T2 动态 | 最近章节、故事线、当前规划、桥接内容 | 作为章节生成的主要注意力来源 |
| T3 可牺牲 | 向量召回、跨幕历史、低优先级知识 | 保留最低配额和召回证据 |

分配器会收集槽位、按优先级压缩内容、记录压缩日志，并根据章节进度计算开局、发展、收敛和终局阶段。阶段指令可以改变新增伏笔、剧情弧线和揭秘的允许范围。

源码证据见 [`context_budget_allocator.py`](../application/engine/services/context_budget_allocator.py)、[`context_budget_policy.py`](../application/engine/services/context_budget_policy.py)、[`context_lifecycle.py`](../application/engine/services/context_lifecycle.py)。

### 5.2 可迁移的上下文资产

当前上下文装配已经覆盖主线承诺、角色伤疤与执念、活跃实体因果链、叙事债务、Previously On、故事线和汇流点、最近章节桥接、Bible 规则、向量召回、章节节拍和人物动态状态。

迁移到写文工作台时，建议把每个槽位都建成带来源和生命周期的 `ContextSlot`，并保留可解释的结构。工作台应该能回答“这一段为什么进入本章上下文”“被压缩了多少”“被哪条规则保留”。

### 5.3 角色动态状态

静态 Bible 角色与动态 `CharacterState` 分开。动态状态包含伤疤、执念、情感弧线、当前摘要、伤疤强度衰减、OOC 判断、人物突破和创伤触发。它是章节后反馈写回下一章上下文的关键桥梁。

源码证据见 `domain/novel/value_objects/character_state.py`、`domain/novel/services/narrative_state_replay.py` 和 `application/engine/services/context_slot_providers.py`。

## 6. 章后统一管线

章节保存后的 `ChapterAftermathPipeline` 是 PlotPilot 最有迁移价值的反馈闭环。当前主路径包括：

1. 提取章间桥接资产。
2. 单次叙事同步，写入摘要、事件、故事线、三元组、伏笔、因果边、人物状态、叙事债务和张力。
3. 角色叙事内核对账。
4. 复用或计算文风结果。
5. 将知识图谱推断、质量护栏快照、治理提交、演进快照、道具同步和汇流点检查放入串行辅助队列。

辅助队列按事件循环串行连接任务。这样可以避免多个章节同时触发 SQLite 写密集任务，也不让较重的 KG 推断和治理报告阻塞下一章生成。

这条链路的迁移原则是“先产生下一章必须使用的投影，再处理可延迟分析”。写文工作台可以把其余章后分析交给事件或任务队列。

源码证据见 [`chapter_aftermath_pipeline.py`](../application/engine/services/chapter_aftermath_pipeline.py) 和 `application/world/services/chapter_narrative_sync.py`。

## 7. SQLite 单写者与持久化

### 7.1 已落地机制

`DatabaseConnection.execute()` 会识别写 SQL。非 writer 线程通过 `mp.Queue` 推送 `EXECUTE_SQL`，事务通过 `EXECUTE_SQL_TXN_BATCH` 收集后交给同一消费者线程。消费者线程注册为 SQLite writer，执行重试、提交和 WAL 检查点。

`PersistenceQueue` 支持章节 upsert、小说 patch、通用 SQL、事务批量、章节删除和队列排空。`DaemonHostMixin` 的 `_queue_sql()`、章节 upsert 和小说状态刷新都走统一队列。

### 7.2 例外边界

启动早期迁移、测试初始化和少数需要写后立即读的交互路径可以使用 `sqlite_writes_bypass_queue()`。环境变量 `PLOTPILOT_ALLOW_DIRECT_SQLITE_WRITES` 也可以显式开启直写。Invocation 合约注册和自动驾驶结果保存目前使用 bypass 上下文包住 repository 写入。

准确的结论是“常规 DatabaseConnection 写入受单写者门禁保护，并保留明确的直写例外”。迁移时应把 bypass 原因做成可审计的枚举，并增加运行期计数。

源码证据见 [`connection.py`](../infrastructure/persistence/database/connection.py)、[`write_dispatch.py`](../infrastructure/persistence/database/write_dispatch.py)、[`persistence_queue.py`](../application/engine/services/persistence_queue.py)。

## 8. DAG、节点注册和实时协议

### 8.1 DAG 执行模型

DAG 模型定义节点、端口、条件边、节点配置、运行状态、全局工作流状态和 SSE 事件。节点由 `NodeRegistry` 注册，元数据包含 prompt 模式、CPMS 节点键、输入输出端口、重试次数和默认 DAG 插槽。

`DAGEngine` 优先使用 LangGraph，缺少 LangGraph 时使用自研拓扑执行器。拓扑执行器按层并行运行没有依赖的节点，支持从指定节点恢复。DAG 定义还计算结构指纹，用于缓存编译结果。

### 8.2 当前不一致

`DAGValidator` 的 DFS 检查允许包含 `gw_retry` 的合法重试环，`DAGEngine.validate()` 内部的 `_has_cycle()` 则把所有环都判为错误。实际默认 DAG 包含写作节点经 `gw_retry` 返回 `exec_writer` 的循环，因此这两套校验语义需要统一。

当前 DAG 测试还出现了注册表中未预期的 `test_node_e`，导致测试在构造 `NodeDefinition` 时就因未知节点类型报错。这个问题属于测试或注册生命周期污染，当前测试结果暂时不能证明 DAG 校验已经稳定。

源码证据见 [`models.py`](../application/engine/dag/models.py)、[`validator.py`](../application/engine/dag/validator.py)、[`engine.py`](../application/engine/dag/engine.py) 和 [`registry.py`](../application/engine/dag/registry.py)。

### 8.3 前后端事件契约

后端 `NodeEvent` 统一承载节点状态、输出、指标、边流、耗时和错误。`NodeEventAggregator` 在短窗口内按节点取最新事件，避免高频事件淹没前端。DAG 路由维护按小说划分的订阅队列和内存 DAG 定义缓存。

前端 `useDAGSSE()` 再做一层节流、批量处理和同类事件合并。队列超过上限时丢弃最旧事件，随后通过 `/dag/{novel_id}/status` 重新同步权威状态。自动驾驶日志也会被映射成 DAG 节点状态，保证 legacy 路径没有标准 NodeEvent 时仍能驱动画布。

这个协议适合直接迁移到写文工作台，但必须坚持两条规则。

1. SSE 承载增量遥测，权威状态仍由状态 API 提供。
2. 状态 API 必须能从持久化或共享状态重建节点当前状态。

源码证据见 [`useDAGSSE.ts`](../frontend/src/composables/useDAGSSE.ts)、[`dag_routes.py`](../interfaces/api/v1/engine/dag/dag_routes.py)、[`event_aggregator.py`](../application/engine/dag/event_aggregator.py)。

## 9. 质量治理

`QualityGuardrail` 当前组合语言风格、角色一致性、情节密度、命名、视角、节奏和宏观节奏七个维度，返回结构化违规项、建议、各维度分数和总分。

`check()` 返回报告，`advise()` 只提供建议，`enforce()` 在低于阈值时抛出异常。章节主管线中的内容校验和文风校验目前按 warning 继续，章后辅助阶段使用 advise 模式生成快照。

迁移到工作台时，应把质量结果分为“阻断规则”“需要人工决定的风险”和“可忽略建议”。同一个分数在不同运行模式下的后果必须可见，避免用户看到红色问题但正文已经自动提交。

源码证据见 [`quality_guardrail.py`](../engine/runtime/quality_guardrails/quality_guardrail.py) 和 [`guardrail_execution.py`](../application/engine/services/guardrail_execution.py)。

## 10. 可迁移能力分级

### 10.1 可以直接抽取

| 能力 | 抽取形式 |
| --- | --- |
| `PipelineContext` / `StepResult` / `PipelineResult` | 作为章节生成 SDK 的基础协议 |
| `PipelineStepBoundary` | 作为暂停、恢复、重试和提交策略表 |
| `InvocationSpec`、`InvocationSession`、`InvocationAttempt` | 作为 AI 调用审计和人工采纳协议 |
| T0-T3 上下文预算 | 作为工作台的上下文装配内核 |
| ChapterAftermath 的同步/辅助分流 | 作为章节保存后的事件处理策略 |
| NodeEvent 与 status 重同步 | 作为实时工作台的运行遥测协议 |
| 单写者队列 | 作为 SQLite 或其他单机数据库的写入门禁 |
| 质量报告 DTO | 作为工作台质量面板和自动门控输入 |

### 10.2 需要改造后抽取

| 能力 | 必要改造 |
| --- | --- |
| Novel 聚合根 | 拆分业务进度、运行控制和展示投影，减少单聚合字段增长 |
| 共享状态 | 由进程内字典升级为带版本号和过期策略的状态存储 |
| Invocation 内存服务 | 增加启动时 rehydrate、状态迁移和并发锁 |
| DAG 执行器 | 统一合法重试环语义，补充条件边和循环上限 |
| Prompt Registry | 统一包声明、变量 schema、版本发布和缓存失效 |
| 章后辅助队列 | 增加持久化任务、重启恢复、幂等键和失败重放 |
| 质量护栏 | 把 advise、check、enforce 映射到明确的工作台决策 |

### 10.3 只适合 PlotPilot 的部分

这些机制适合借鉴思想，工作台需要按自身领域重新实现。

- 部、卷、幕、汇流点和长篇收敛沙漏，它们依赖 PlotPilot 的故事结构模型。
- `autopilot_status` 与守护进程轮询，它们服务于连续无人值守生产。
- legacy 节拍生成、`AutoNovelGenerationWorkflow` 和旧版兼容字段。
- Anti-AI 规则、特定题材管线和当前 Prompt Package 目录。
- SQLite 直写 bypass 的历史兼容入口。

## 11. 写文工作台建议落地顺序

### 阶段 0：先定义稳定协议

建立 `WritingJob`、`WritingStep`、`ContextSlot`、`InvocationSession`、`QualityReport` 和 `ProjectionEvent` 六个核心协议。先写状态转换和幂等规则，再接 UI。

### 阶段 1：单章可恢复生产

只实现定位章节、上下文预算、生成、草稿保存、审阅、提交和恢复。保留 `transient` 与 `stable` 边界，正文预览和正式正文分离。

### 阶段 2：AI 调用和提示词审计

引入 Prompt Registry、变量快照、模板哈希、生成 attempt 和人工采纳。把章节正文、章纲、摘要、改写和审阅都接入同一 Gateway。

### 阶段 3：章后反馈闭环

先做摘要、桥接、人物状态和伏笔，再接向量和知识图谱。所有后处理都用事件或任务队列，支持幂等和重放。

### 阶段 4：实时工作台和自动驾驶

以 ProjectionEvent 驱动 Pinia 状态，SSE 只发送增量，状态 API 提供重同步。最后再加入 DAG 视图、连续写作和无人值守守护进程。

## 12. 当前风险与验证清单

| 优先级 | 风险 | 当前证据 | 下一步验证 |
| --- | --- | --- | --- |
| 高 | DAG 校验器允许 `gw_retry` 环，执行器校验拒绝所有环 | `validator.py` 与 `engine.py` 语义不同 | 为合法重试环和非法业务环分别补测试 |
| 高 | invocation 服务有内存状态，SQLite 恢复路径未完全闭合 | factory 注入 SQLite repository，service 使用内存字典 | 模拟进程重启，恢复等待审阅和待提交会话 |
| 高 | 生产章节路径存在 StoryPipeline 与 legacy 分叉 | `writing_delegate.py` 通过环境变量选择 | 统计真实运行配置，明确 legacy 的退出时间 |
| 中 | 质量失败有 warning 继续语义 | `BaseStoryPipeline` 和 `ChapterAftermathPipeline` | 为自动驾驶、人工辅助和强制提交分别定义策略矩阵 |
| 中 | 辅助章后任务是进程内串行队列 | `_SerializedAuxiliaryQueue` 没有任务持久化 | 增加重启、重复消费和失败重放测试 |
| 中 | DAG 事件可能被聚合器或前端队列丢弃 | `NodeEventAggregator` 和 `useDAGSSE` 都会合并或丢弃 | 验证 status 重同步能恢复所有权威字段 |
| 中 | Prompt 包声明与模板变量发生漂移 | `bible-worldbuilding` 合约测试失败 | 校准 package.yaml 与模板，增加 CI 合约检查 |
| 低 | 注册表副作用污染 DAG 构造测试 | 测试出现未预期 `test_node_e` | 明确测试注册隔离和 builtins 加载生命周期 |
| 高 | AI Invocation 变量快照契约仍在演进 | 86 个测试中 80 通过、6 失败，集中在别名扩展、scope/stage 推断和 continuation 输入校验 | 统一快照键策略，补重启恢复、故事级变量和幕规划 continuation 契约测试 |
| 低 | 依赖容器和 API dependencies 职责过大 | `interfaces/api/dependencies.py` 体量较大 | 按读模型、写模型、运行时和 AI 依赖拆分 |

## 13. 测试基线

本轮在 2026-11-24 运行了以下窄范围测试。

| 测试 | 结果 | 说明 |
| --- | --- | --- |
| `pytest tests/unit/interfaces/api/test_architecture_boundaries.py -q` | 22 passed | 接口架构边界通过 |
| `pytest tests/unit/engine -q` | 114 passed | 引擎单元测试通过 |
| `pytest tests/unit/infrastructure/persistence -q` | 42 passed | SQLite、迁移和仓储测试通过 |
| `pytest tests/unit/infrastructure/ai -q` | 134 passed, 5 skipped, 1 failed | `bible-worldbuilding` 变量声明缺少 `fields_desc` 与 `genre_opening_profile` |
| `pytest tests/unit/application/ai_invocation -q` | 80 passed, 6 failed | 变量快照别名扩展、故事级 scope/stage 推断和幕规划 continuation 契约仍有失败用例 |
| `pytest tests/dag -q` | 75 passed, 14 failed | DAG 测试在构造默认节点时受未预期 `test_node_e` 注册影响，另需核对环语义 |

上述失败会作为当前仓库的待修复或待隔离问题保留在风险表中，相关能力按“部分落地”记录。

## 14. 维护方法

后续每次深入项目时，按以下顺序更新本文件。

1. 先定位真实入口和调用方。
2. 再确认状态是否持久化，是否能重启恢复。
3. 再确认前端看到的是权威状态还是增量投影。
4. 用窄范围测试验证结论。
5. 把兼容层、失败测试和未验证假设写入风险表。

这样可以避免把 README、设计注释、兼容入口和生产事实混成同一张架构图。
