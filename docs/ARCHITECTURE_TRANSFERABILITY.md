# PlotPilot 架构、工程能力与可迁移性

这份文档记录 PlotPilot 在当前代码状态下已经落地的机制、兼容层留下的分叉、测试可以证明的边界，以及迁移到写文工作台时应该保留或重做的部分。文档以源码和测试为准，目标设计与实际运行路径分开记录。

最近复核日期为 2026-11-25。初轮以 `26a85949` 为源码基线，根据 Claude 评审包 `review-plotpilot-architecture-analysis.zip` 的 `src/data.ts` 核验依赖、写入门禁、幂等性和 CI。随后整改已实现 bypass 上下文隔离、采纳提交持久化、DAG 规则统一、独立 daemon 冒烟、默认回环监听与依赖增量门禁。第 2 节保留历史测量，第 13 节区分历史实验与整改验证；剩余边界见第 12 节。

能力清单需要结合调用方解释其保证。以 `fd1bbb8f` 继续核对的提交所有权、预算与渲染反例、质量闸门、异步反馈延迟和 DAG 重连边界，见 [工程模型与不变量](ENGINEERING_MODEL.md)。

## 1. 结论摘要

PlotPilot 已经形成一个面向长篇创作的生产系统，组合了小说状态、章节生产、AI 调用、记忆装配、章后同步、质量治理、持久化和实时工作台投影。当前最值得迁移的工程资产有六类。

1. **有提交边界的章节管线**。每一步都返回结构化结果，正文生成、章节保存、章后同步和收尾具有不同的中断处理策略。
2. **AI 调用控制平面**。提示词版本、变量快照、调用会话、生成尝试、采纳决策和提交结果被拆开管理，支持人工审阅和自动驾驶暂停。
3. **预算化上下文装配**。上下文按 T0 到 T3 分层，在总预算、最低保障和阶段指令下裁剪。
4. **章后反馈闭环**。章节保存后会把正文转成桥接资产、叙事同步、向量、伏笔、三元组、因果边、人物状态和叙事债务，供下一章继续使用。
5. **跨进程写入调度与投影状态**。经过统一连接层的常规 SQLite 写入进入队列，由宿主进程中的消费者线程提交；采纳提交支持确认响应，显式 bypass 和独立连接仍需审计。运行时状态通过共享状态、状态 API 和 SSE 投影给前端。
6. **可替换的题材管线和节点注册表**。基础管线提供默认步骤，题材管线覆写少量步骤，DAG 节点通过注册表和元数据接入。

迁移前应继续处理三类边界：显式 bypass 与独立连接仍在门禁之外，非 SQLite 副作用需要各自的幂等或补偿协议，业务层仍直接依赖具体基础设施和 API 工厂。生产路径也仍有兼容分叉：`EngineDaemon` 是推荐入口，同时保留 `legacy_writing_delegate`、旧 `AutopilotDaemon`、`AutoNovelGenerationWorkflow` 和 StoryPipeline。迁移时应先确认许可证适用范围，抽取稳定协议，再减少旧路径。

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

### 2.1 职责与实际依赖

上表描述目录职责，并不证明依赖方向符合分层设计。以下矩阵保留整改前的测量；运行 `python scripts/analyze_architecture.py` 查看当前值，加 `--json` 可查看每条引用的文件、行号、是否位于函数内，以及是否被字面的 `TYPE_CHECKING` 条件保护。扫描现在也包含尚未提交、未被忽略的 Python 文件。

统计范围为五个后端层中由 Git 跟踪的 829 个 Python 文件。使用标准库 AST，包含多行 import、函数内 import 和类型检查分支；同一条 import 对同一目标层只计一次，排除同层、相对及动态导入。计数表达源码引用，不表达运行次数，也不自动判定违规。

| 来源 → 目标 | domain | application | engine | infrastructure | interfaces |
| --- | ---: | ---: | ---: | ---: | ---: |
| domain | — | 1 | 1 | 0 | 0 |
| application | 452 | — | 20 | 352 | 16 |
| engine | 44 | 125 | — | 48 | 3 |
| infrastructure | 156 | 21 | 1 | — | 1 |
| interfaces | 58 | 288 | 11 | 158 | — |

需要区分三种情况。

- `application → infrastructure` 的 352 条引用中，222 条位于函数内，4 条处于类型检查分支。这显示具体实现耦合广泛存在；函数内导入本身不能证明是在规避循环依赖。`application → interfaces` 有 16 条，`engine → interfaces` 有 3 条，说明 API 工厂还承担了业务和运行时的服务定位器角色。
- `domain/engine/dag/repositories/dag_version_repository.py` 导入 application 的 `DAGDefinition`，`domain/novel/value_objects/story_phase.py` 从 engine 重导出 `StoryPhase`，均为真实的外向依赖，需要移动共享契约或消除兼容重导出。
- `infrastructure → application` 的 21 条引用不能全部算作违规：基础设施实现应用层端口、转换 DTO 可以符合依赖倒置。引用 `application.engine.services.persistence_queue` 或配置服务则涉及运行时与配置耦合，应逐条分类。

Claude 评审中的 342、20、37 等数字与该 AST 口径不一致，本文采用脚本结果，不推测差异原因。整改前普通 bypass 在五层内有 34 个直接调用表达式，其中包含启动包装器内部的一次调用；另有 1 个启动包装器调用。`ai_invocation_routes.py` 原占 14 处，本轮均已改走带确认的 writer 调用。这不是 34 条独立运行路径，也不包含通过别名或动态调用的情况。

`config/architecture_import_baseline.json` 固定 `47959b8e` 的 352 条 application→infrastructure import。`python scripts/analyze_architecture.py --check-baseline config/architecture_import_baseline.json` 按文件、规范化语句和重复次数禁止新增，允许减少；删除旧依赖不会抵消不同的新依赖。门禁已接入后端 CI，现有依赖不要求一次性清零。

### 2.2 迁移热点和领域不变量

以下文件是拆分与依赖检查的优先对象。行数说明审查成本，不能单独证明设计错误；脚本也会输出函数定义数量和更完整的热点列表。

| 文件 | 行数 | 迁移关注点 |
| --- | ---: | --- |
| `interfaces/api/v1/engine/autopilot_routes.py` | 2855 | 路由、运行控制和诊断边界 |
| `application/blueprint/services/continuous_planning_service.py` | 2843 | 规划编排与领域决策的分离 |
| `application/world/services/chapter_narrative_sync.py` | 2486 | 同步协议、解析与多仓储写入 |
| `engine/runtime/daemon_host.py` | 2361 | 69 个函数定义覆盖宿主、持久化和恢复职责 |
| `application/engine/services/context_budget_allocator.py` | 2115 | 预算算法与槽位装配的分离 |
| `interfaces/api/dependencies.py` | 1355 | 跨层工厂与应用组合根 |
| `engine/pipeline/base.py` | 1225 | 可抽取协议与宿主依赖的边界 |

`StoryPipelineRunner` 同时继承 `DaemonHostMixin` 和 `BaseStoryPipeline`，迁移成本不能只看 runner 本身。领域层体积较小也不能直接判定为“贫血领域”：`Novel.add_chapter()` 检查章节连续性，`Chapter.update_tension_score()` 限制 0–100，`Scar` 和 `Motivation` 校验强度、优先级并提供衰减或解决行为。应逐个保留这些已存在的不变量，再检查 application 服务中哪些规则需要下沉。

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

API 托管路径由 `interfaces/daemon_manager.py` 初始化持久化队列和消费者，再把队列传给子进程。独立脚本 `scripts/start_daemon.py` 现会初始化数据库、注册处理器、启动消费者，在 writer 上完成依赖装配，并按 daemon、消费者、数据库的顺序关闭。第 7 节记录隔离目录中的真实启动、写入和停止验证；脚本仍复用 API 工厂，尚未验证真实 LLM 生成链路。

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
| `idempotency_key` | 标识重复提交；需持久化去重与副作用幂等共同保障 |

源码证据见 [`prompt_assembler.py`](../application/ai_invocation/prompt_assembler.py)、[`variable_hub.py`](../application/ai_invocation/variable_hub.py)、[`prompt_registry.py`](../infrastructure/ai/prompt_registry.py)。

### 4.3 已落地与待验证的持久化边界

自动驾驶工厂会为 invocation spec、变量、session、attempt 和 adoption repository 注入 SQLite 实现。`AutopilotInvocationOrchestrator._save_result()` 会把 session、attempt、decision 和 commit 写入对应仓储。

`InvocationSessionService`、`AttemptService` 和 `AdoptionService` 仍有内存状态。生产组合根为 `AdoptionCommitService` 注入 SQLite adoption repository；不注入仓储的内存模式供单元测试和工具使用，不提供进程重启恢复保证。API 从 SQLite 重建 session、attempt 和 decision，自动提交则先补齐这些依赖再抢占 commit。

生产提交先调用 `repos["adoption"].create_commit()`，按 `(session_id, decision_id)` 唯一键取得原始记录；同一 decision 由本机 OS 文件锁串行执行，锁等待发生在 writer 外，进程退出后由 OS 释放。已成功提交直接返回原结果，并保留会话后续进度。其余提交按步骤记录 running、succeeded、blocked 或 failed，恢复时跳过成功步骤；`save_commit()` 按幂等键更新，始终保留原始 ID。

这些检查点不能自动保证业务副作用只执行一次。continuation 需用稳定 `ContinuationContext.idempotency_key` 实现自身去重，或提供补偿；只有明确声明 `retry_safe=True` 的 handler 才允许中断恢复，否则返回需要补偿的失败结果。正文内容、局部回执、计数和节拍进度已在同一事务保存，章节投影也有事务回执，变量输出以 `source_commit_id` 去重。其他外部文件、向量服务等仍需各自的幂等策略；本机锁不构成网络文件系统或多机部署保证。

API 提交及 Gateway、自动驾驶异步入口把同步 commit 放到工作线程。daemon 子进程使用带响应的队列请求，由拥有 writer 的宿主协调提交，避免在子进程等待不可执行的本地 callback。回归测试覆盖重复 HTTP、并发 HTTP、事件循环响应、两个 spawned 进程请求、真实进程在副作用后退出，以及安全重试与拒绝不安全重试。

源码证据见 [`factory.py`](../application/ai_invocation/autopilot/factory.py)、[`orchestrator.py`](../application/ai_invocation/autopilot/orchestrator.py)、[`ai_invocation_routes.py`](../interfaces/api/v1/engine/ai_invocation_routes.py)、[`sqlite_ai_invocation_repository.py`](../infrastructure/persistence/database/sqlite_ai_invocation_repository.py) 和 [`add_ai_invocation_variable_hub.sql`](../infrastructure/persistence/database/migrations/add_ai_invocation_variable_hub.sql)。

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

`DatabaseConnection.execute()` 会识别写 SQL。非 writer 线程通过 `mp.Queue` 推送 `EXECUTE_SQL`，事务通过 `EXECUTE_SQL_TXN_BATCH` 收集后交给同一消费者线程。API 托管模式下，`DaemonProcessManager.start()` 在 API 进程初始化队列、注册处理器并启动消费者，再把队列传给独立守护进程；子进程用 `inject_persistence_queue()` 连接该通道。消费者线程注册为 SQLite writer，执行重试、提交和 WAL 检查点。因此这是跨进程的写入调度，不能只按线程模型理解。

`PersistenceQueue` 支持章节 upsert、小说 patch、通用 SQL、事务批量、章节删除和队列排空。`DaemonHostMixin` 的 `_queue_sql()`、章节 upsert 和小说状态刷新都走统一队列。

普通 SQL 的 `push()` 返回成功只代表命令已进入内存队列，不代表 SQLite 已提交，也没有持久化任务日志。新增本进程 writer callback 会等待完成并返回结果或异常；子进程采纳提交则通过响应管道返回持久化结果。callback 不自动重试；排队超时且尚未开始的操作会取消，已开始的操作可能继续完成。进程崩溃仍可能丢失在途普通命令，提交确认与持久化重放是不同保证。

### 7.2 例外边界

启动早期迁移仍通过 `startup_sqlite_writes_bypass_queue()` 支持直写。运行期普通 bypass 必须传非空 `reason`，进入时登记计数并记录日志；Invocation API 原有 14 处调用已改为等待 writer。合约注册、部分 continuation、自动驾驶结果保存和快照等兼容调用仍有显式例外。环境变量 `PLOTPILOT_ALLOW_DIRECT_SQLITE_WRITES` 可以显式开启直写。

整改前全局整数会让无关线程获得直写许可。现使用带 token/reset 的 `ContextVar[int]` 保存嵌套深度，无关线程和异步任务不会因别处进入 bypass 而放行。`asyncio.to_thread` 会复制调用时的上下文，`run_in_executor` 和裸 `threading.Thread` 不自动复制；测试分别验证这些传播差异，以及线程 A 进入 bypass 时线程 B 仍被拒绝。

因此可确认普通写入路由和 bypass 的上下文隔离，但不能宣称所有运行期写入均经过唯一消费者。保留的显式例外和独立连接仍应逐步收敛；原因日志与计数用于定位实际使用，不能替代调用方副作用和事务审查。

### 7.3 独立连接的不同语义

仅搜索到 `sqlite3.connect()` 不足以证明业务写入绕过门禁，需要检查 SQL 和调用方。

| 位置 | 已确认行为 | 证据边界 |
| --- | --- | --- |
| `context_slot_providers.build_key_props_slot_content()` | 查询道具，业务 SQL 为 `SELECT` | 还会应用连接 PRAGMA，不能据此推断任意业务写入 |
| `state_snapshot_manager.AtomicStateTransaction` | `BEGIN IMMEDIATE` 后直接 `INSERT/UPDATE` 并提交 | 实现确实绕过调度；本轮未找到模块外的生产调用，不能断言当前主链路在执行它 |
| `autopilot_routes._open_sqlite_diagnostic_connection()` 的调用方 | 查询可用性，或 `BEGIN IMMEDIATE` 后提交测试写锁 | 诊断会参与写锁竞争，但没有发现该处直接修改业务行 |
| `interfaces.runtime` 的关闭清理 | 执行 `PRAGMA wal_checkpoint(TRUNCATE)` | 属于数据库维护操作，需要与消费者关闭顺序一起审查 |

### 7.4 独立脚本的启动验证

整改前只复现了队列未就绪时写 SQL 的失败分支。现 `python scripts/start_daemon.py --smoke-test` 在全新子进程中设置临时数据目录，关闭两个环境直写开关和向量存储，执行真实依赖装配、daemon 首次轮询、常规 SQL 队列写入一条暂停的小说记录、writer 屏障与独立读取，然后停止 daemon、排空消费者、关闭连接并删除临时目录。

该冒烟已成功执行，并作为子进程回归测试纳入 `tests/unit/engine/test_standalone_daemon_smoke.py`。它证明独立入口的启动、写入与停止，不证明真实 LLM、向量索引或长期无人值守生成已验证。

源码证据见 [`connection.py`](../infrastructure/persistence/database/connection.py)、[`write_dispatch.py`](../infrastructure/persistence/database/write_dispatch.py)、[`persistence_queue.py`](../application/engine/services/persistence_queue.py)、[`daemon_manager.py`](../interfaces/daemon_manager.py) 和 [`start_daemon.py`](../scripts/start_daemon.py)。

## 8. DAG、节点注册和实时协议

### 8.1 DAG 执行模型

DAG 模型定义节点、端口、条件边、节点配置、运行状态、全局工作流状态和 SSE 事件。节点由 `NodeRegistry` 注册，元数据包含 prompt 模式、CPMS 节点键、输入输出端口、重试次数和默认 DAG 插槽。

`DAGEngine` 优先使用 LangGraph，缺少 LangGraph 时使用自研拓扑执行器。拓扑执行器按层并行运行没有依赖的节点，支持从指定节点恢复。DAG 定义还计算结构指纹，用于缓存编译结果。

### 8.2 统一环规则与注册隔离

`DAGEngine._has_cycle()` 现复用 `DAGValidator` 的规则：移除经过 `gw_retry` 的边后，剩余业务图必须无环。这样既允许显式重试路径，也不会让同一强连通区域中的非法业务子环被重试节点掩盖。参数化测试分别验证合法重试环与非法业务环。

`tests/dag/test_registry.py` 在加载内置节点后，为每条测试保存并恢复注册表的类型与元数据字典，消除了 `test_node_e` 对后续默认 DAG 构造的污染。

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

### 10.1 优先抽取的协议候选

下表按职责给出候选边界，不代表现有模块可以原样复制。每项仍需清点导入、注入依赖、状态持久化和许可证条件；第 2 节的热点文件尤其需要先拆协议与实现。

| 能力 | 抽取形式 |
| --- | --- |
| `PipelineContext` / `StepResult` / `PipelineResult` | 作为章节生成 SDK 的基础协议 |
| `PipelineStepBoundary` | 作为暂停、恢复、重试和提交策略表 |
| `InvocationSpec`、`InvocationSession`、`InvocationAttempt` | 作为 AI 调用审计和人工采纳协议 |
| T0-T3 上下文预算 | 作为工作台的上下文装配内核 |
| ChapterAftermath 的同步/辅助分流 | 作为章节保存后的事件处理策略 |
| NodeEvent 与 status 重同步 | 作为实时工作台的运行遥测协议 |
| 质量报告 DTO | 作为工作台质量面板和自动门控输入 |

### 10.2 需要改造后抽取

| 能力 | 必要改造 |
| --- | --- |
| Novel 聚合根 | 拆分业务进度、运行控制和展示投影，减少单聚合字段增长 |
| 共享状态 | 为跨进程内存投影增加版本、过期策略和重建契约 |
| Invocation 服务与提交路径 | 统一 repository 恢复、提交前持久化抢占、步骤幂等和崩溃恢复 |
| SQLite 写入队列 | 隔离 bypass，清理独立写连接，补入队/提交确认和重放协议 |
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

### 10.4 许可证是迁移前置条件

仓库 [`LICENSE`](../LICENSE) 采用 Apache 2.0 加 Commons Clause，不能按纯 Apache 2.0 推断商用授权。附加条件限制向第三方收费或取得其他对价、且价值全部或实质上来自本软件功能的产品或服务，并要求保留相关许可通知。

“目标工作台收费”本身也不足以直接得出禁止复用的结论；需要核对具体复制范围、目标产品的价值来源、分发或服务方式，以及是否存在额外授权。技术上可抽取的模块与许可上可复用的代码应分别登记，在确定迁移实现前解决授权边界。条款以仓库许可证为准，补充说明见 [Commons Clause 官方原文与 FAQ](https://commonsclause.com/)。

### 10.5 文档事实与默认暴露面

`LLMClient` 实际委托动态 provider；`chromadb_vector_store.py` 保留兼容命名，主要实现为 FAISS 与 JSON 元数据，不应按文件名判定使用了 ChromaDB 引擎。架构概览与 README 应同步这些事实，性能宣传需要可复现基准。

直接执行 `interfaces/main.py` 现默认监听 `127.0.0.1:8000`，CLI 也使用相同默认地址；可通过 `PLOTPILOT_BIND_HOST` 或 CLI `--host` 显式调整。指南中的 uvicorn 命令仍显式使用 `127.0.0.1:8005`，其地址由 uvicorn `--host` 控制。本轮未进行完整认证或网络安全审计。

## 11. 写文工作台建议落地顺序

### 阶段 0：先定义稳定协议

先确认许可与目标产品形态，记录实际依赖基线，隔离写入 bypass，并补独立启动的写入验证。随后建立 `WritingJob`、`WritingStep`、`ContextSlot`、`InvocationSession`、`QualityReport` 和 `ProjectionEvent` 六个核心协议。先写状态转换和持久化幂等规则，再接 UI。

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
| 前置 | 许可证可能约束目标产品的代码复用 | Apache 2.0 附带 Commons Clause | 按复用范围、价值来源与分发方式确认许可或额外授权 |
| 高 | 显式 bypass 兼容路径仍可绕过 writer | ContextVar 隔离已验证，合约注册、部分 continuation、结果保存和快照仍有带原因的例外 | 根据原因计数收敛活跃调用，区分事务需求与迁移需求 |
| 高 | 任意 continuation 副作用不能自动保证只执行一次 | 持久化抢占和步骤恢复已实现；未知 handler 的中断重试会被拒绝 | 新增 handler 必须实现自身幂等或补偿，外部文件及服务按各自协议处理 |
| 高 | 跨层具体依赖和 API 工厂阻碍迁移 | 已固定 352 条 application→infrastructure 历史 import，并禁止新增 | 分类协议/DTO/具体实现，逐步移除存量依赖 |
| 高 | 普通在途 SQL 和章后辅助任务缺少持久化重放 | 采纳提交已有确认与 SQLite 回执，通用队列和辅助任务仍在内存 | 设计任务日志、重放和故障交接；本机文件锁不替代多机协调 |
| 高 | 独立 daemon 的真实生成尚未验证 | 已通过隔离目录启动、普通队列写入、读取和停止 | 在明确配置下另行验证真实 LLM、向量索引与长时间运行 |
| 高 | 生产章节路径存在 StoryPipeline 与 legacy 分叉 | `writing_delegate.py` 通过环境变量选择 | 统计真实运行配置，明确 legacy 的退出时间 |
| 高 | CI Python 版本声明和集成覆盖仍有限 | 已加入依赖基线、`tests/dag` 与 master push 触发；Python 3.11 与发布 3.14 不同 | 明确支持矩阵，补必要集成检查 |
| 中 | 质量失败有 warning 继续语义 | `BaseStoryPipeline` 和 `ChapterAftermathPipeline` | 为自动驾驶、人工辅助和强制提交分别定义策略矩阵 |
| 中 | DAG 事件可能被聚合器或前端队列丢弃 | `NodeEventAggregator` 和 `useDAGSSE` 都会合并或丢弃 | 验证 status 重同步能恢复所有权威字段 |
| 中 | 大型宿主、同步和路由模块提高拆分成本 | 1225–2855 行的热点文件及 runner 多重继承 | 先梳理调用和副作用，再按稳定协议拆分 |
| 中 | 独立 SQLite 连接与维护操作不受统一门禁覆盖 | 快照事务直接写入；诊断取写锁；关闭时 checkpoint | 确认活跃调用方，分别处理业务写入与维护操作 |

## 13. 测试基线

初轮分析在 2026-11-24 记录了以下窄范围测试。此表保留整改前的历史结果；整改验证单独列在第 13.3 节，不能把旧失败当作当前执行结果。

| 测试 | 结果 | 说明 |
| --- | --- | --- |
| `pytest tests/unit/interfaces/api/test_architecture_boundaries.py -q` | 22 passed | 特定接口拆分、环境配置和禁止导入 `interfaces.main` 等规则通过，不代表全部层间依赖符合设计 |
| `pytest tests/unit/engine -q` | 114 passed | 引擎单元测试通过 |
| `pytest tests/unit/infrastructure/persistence -q` | 42 passed | SQLite、迁移和仓储测试通过 |
| `pytest tests/unit/infrastructure/ai -q` | 134 passed, 5 skipped, 1 failed | `bible-worldbuilding` 变量声明缺少 `fields_desc` 与 `genre_opening_profile` |
| `pytest tests/unit/application/ai_invocation -q` | 80 passed, 6 failed | 变量快照别名扩展、故事级 scope/stage 推断和幕规划 continuation 契约仍有失败用例 |
| `pytest tests/dag -q` | 75 passed, 14 failed | DAG 测试在构造默认节点时受未预期 `test_node_e` 注册影响，另需核对环语义 |

其中 bypass、采纳提交、变量快照和 DAG 问题已在本轮整改；未覆盖或尚未消除的风险仍列在第 12 节。

### 13.1 整改前复核的执行证据

历史复核和本轮整改均在 Windows、Python 3.12.7、pytest 8.4.2 上执行；不以它代表 README 所列 3.14 的兼容性。下表为整改前的缺陷复现。

| 检查 | 本次结果 | 能证明的范围 |
| --- | --- | --- |
| `python scripts/analyze_architecture.py` | 829 个跟踪文件均可解析，输出第 2 节的依赖与热点统计 | 静态源码引用，不能证明运行路径或全部分层违规 |
| `python -m pytest tests/unit/infrastructure/persistence/database/test_write_dispatch_startup_bypass.py -q --tb=short` | 3 passed | 原有启动 bypass、嵌套恢复与环境开关测试通过，未覆盖并发隔离 |
| `ThreadPoolExecutor` 跨线程局部实验 | 无关线程的直写许可为 `False → True → False` | 在显式关闭环境直写开关时，bypass 仍跨线程放行；未向数据库写数据 |
| 同一 decision 在两个 `AdoptionCommitService` 实例间重复提交 | 同实例复用 commit，新实例产生新 commit；模拟 continuation 共调用 2 次 | 复现服务实例边界的去重缺口；替换了 continuation，不代表已执行真实 HTTP 或业务副作用 |
| 队列未就绪的 `DatabaseConnection.execute()` 局部实验 | 写 SQL 在连接访问前抛出 `RuntimeError` | 验证缺队列的失败分支，不能代替独立守护进程完整启动测试 |

### 13.2 CI 覆盖的限制

[`backend-ci.yml`](../.github/workflows/backend-ci.yml) 现运行依赖基线检查和 `pytest tests/unit tests/dag -q --tb=short`，在匹配改动的 PR 和 master push 上触发。路径过滤包含 Python、requirements、pytest 配置、基线、提示词包和工作流自身；仍未包含 `tests/integration`，也未查询 GitHub 分支保护或历史运行状态。

后端 CI 使用 Python 3.11，README 标示 3.14，发布工作流也使用 3.14；版本声明和验证范围没有对齐。这不证明 3.11 必然无法运行，也不证明本机 3.12 的通过结果能覆盖另外两个版本。应先明确支持版本，再建立对应矩阵与 DAG、必要集成测试的门禁。

### 13.3 本轮整改验证

第一轮定向验证的 519 项测试全部通过：`python -m pytest tests/unit/application/ai_invocation tests/unit/application/engine tests/unit/engine tests/unit/infrastructure/persistence/database tests/unit/interfaces tests/dag -q --tb=short`。包括真实 writer、跨线程和异步上下文、重复与并发 HTTP、spawned 子进程提交、真实进程在副作用后退出、内置正文 handler 的事务回执恢复、后续内容与进度保留，以及完整独立 daemon 冒烟。

`python scripts/analyze_architecture.py --check-baseline config/architecture_import_baseline.json`、`python scripts/check_docs.py` 和 `git diff --check` 通过。变量快照仍保留公开渲染别名，但只为实际绑定和未覆盖的上下文事实记录快照，避免合成别名重复，并修正默认 runtime 元数据对故事级 scope/stage 推断的遮盖。

完整后端检查还修复两处既有提示词契约漂移：旧章节工作流的公开 Prompt 构建入口现在接受并传递类型画像、读者契约和节奏约束；世界观模板使用运行期 `fields_desc`，使逐维度补全只要求当前维度，相关变量同步声明在包中。最终提交顺序也补充故障测试：先保存会话终态，再保存 commit 终态；会话保存失败时 commit 保持可恢复，重试复用已完成的副作用。

2026-11-25 最终执行 `python -m pytest tests/unit tests/dag -q --tb=short`，结果为 **1671 passed、7 skipped**。采纳恢复专项的 13 项测试全部通过。此结果覆盖本机后端单元测试和 DAG 测试，不包含真实 LLM 调用、完整集成套件或其他 Python 版本。

## 14. 维护方法

后续每次深入项目时，按以下顺序更新本文件。

1. 先定位真实入口和调用方。
2. 再确认状态是否持久化，是否能重启恢复。
3. 再确认前端看到的是权威状态还是增量投影。
4. 用窄范围测试验证结论。
5. 运行 `python scripts/analyze_architecture.py`，区分职责、静态依赖和已执行的运行路径。
6. 把兼容层、失败测试、许可前提和未验证假设写入风险表，并保留新旧测试结果的时间边界。

这样可以避免把 README、设计注释、兼容入口和生产事实混成同一张架构图。
