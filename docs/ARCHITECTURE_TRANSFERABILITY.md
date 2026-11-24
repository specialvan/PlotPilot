# PlotPilot 架构、工程能力与可迁移性

这份文档记录 PlotPilot 在当前代码状态下已经落地的机制、兼容层留下的分叉、测试可以证明的边界，以及迁移到写文工作台时应该保留或重做的部分。文档以源码和测试为准，目标设计与实际运行路径分开记录。

最近复核日期为 2026-11-24，源码基线为 `26a85949`。本次根据 Claude 评审包 `review-plotpilot-architecture-analysis.zip` 的 `src/data.ts` 重新检查依赖、写入门禁、幂等性和 CI；评审意见经过源码与局部实验核验后才纳入。下述生产问题尚未修复，文档更新不代表整改完成。

## 1. 结论摘要

PlotPilot 已经形成一个面向长篇创作的生产系统，组合了小说状态、章节生产、AI 调用、记忆装配、章后同步、质量治理、持久化和实时工作台投影。当前最值得迁移的工程资产有六类。

1. **有提交边界的章节管线**。每一步都返回结构化结果，正文生成、章节保存、章后同步和收尾具有不同的中断处理策略。
2. **AI 调用控制平面**。提示词版本、变量快照、调用会话、生成尝试、采纳决策和提交结果被拆开管理，支持人工审阅和自动驾驶暂停。
3. **预算化上下文装配**。上下文按 T0 到 T3 分层，在总预算、最低保障和阶段指令下裁剪。
4. **章后反馈闭环**。章节保存后会把正文转成桥接资产、叙事同步、向量、伏笔、三元组、因果边、人物状态和叙事债务，供下一章继续使用。
5. **跨进程写入调度与投影状态**。经过统一连接层的常规 SQLite 写入进入队列，由 API 进程中的消费者线程提交；全局 bypass 和独立连接仍能绕开门禁。运行时状态通过共享状态、状态 API 和 SSE 投影给前端。
6. **可替换的题材管线和节点注册表**。基础管线提供默认步骤，题材管线覆写少量步骤，DAG 节点通过注册表和元数据接入。

迁移前应先处理三类边界：全局 bypass 与独立连接破坏单写者约束，Invocation 副作用与持久化去重没有完整闭环，业务层直接依赖具体基础设施和 API 工厂。生产路径也仍有兼容分叉：`EngineDaemon` 是推荐入口，同时保留 `legacy_writing_delegate`、旧 `AutopilotDaemon`、`AutoNovelGenerationWorkflow` 和 StoryPipeline。迁移时应先确认许可证适用范围，抽取稳定协议，再减少旧路径。

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

上表描述目录职责，并不证明依赖方向符合分层设计。运行 `python scripts/analyze_architecture.py` 可复核以下矩阵；加 `--json` 可查看每条引用的文件、行号、是否位于函数内，以及是否被字面的 `TYPE_CHECKING` 条件保护。

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

Claude 评审中的 342、20、37 等数字与本次 AST 口径不一致，本文采用脚本结果，不推测差异原因。普通 bypass 在五层内有 34 个直接调用表达式，其中包含启动包装器内部的一次调用；另有 1 个启动包装器调用。`ai_invocation_routes.py` 占普通 bypass 调用的 14 处。这不是 34 条独立运行路径，也不包含通过别名或动态调用的情况。

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

API 托管路径由 `interfaces/daemon_manager.py` 初始化持久化队列和消费者，再把队列传给子进程；独立脚本 `scripts/start_daemon.py` 没有对应的初始化或注入调用，还直接复用了 API 工厂。因此独立脚本不能仅凭可构造 `EngineDaemon` 就视为经过验证的生产部署路径。第 7 节记录队列未就绪时的局部复现；完整启动、依赖初始化顺序和真实写入仍需隔离数据目录的冒烟验证。

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

同时，`InvocationSessionService`、`AttemptService`、`AdoptionService` 和 `AdoptionCommitService` 本身仍保存内存字典，自动驾驶工厂创建的是空服务对象。但不能据此断言所有会话在重启后都无法读取：`ai_invocation_routes.py` 的查询和提交入口会直接从 SQLite repository 重建 session、attempt 和 decision。需要分别验证自动驾驶服务与 API 路径的恢复契约。

提交幂等的缺口更具体：`AdoptionCommitService.commit()` 只查询实例内的 `_commits_by_key`，随后执行 continuation 等副作用；API 的 `create_commit()` 每次请求都会新建该服务，完成后才 `save_commit()`。因此重复 HTTP 请求也会越过空字典，并不需要等待进程重启。

数据库并非没有防重机制：`ai_adoption_commits` 有 `UNIQUE(idempotency_key)`，repository 也提供先查询旧记录的 `create_commit()`。但上述 API 路径没有在执行副作用前调用它；`save_commit()` 只处理 `ON CONFLICT(id)`，相同幂等键的新 commit ID 还可能在副作用执行后才遭遇唯一键冲突。应先持久化抢占提交、按步骤保存执行结果，并使各 continuation 支持幂等或补偿；唯一键本身不能保证业务副作用只发生一次。

迁移前应补齐等待审阅会话的重启恢复、相同 decision 的重复请求、并发提交，以及副作用完成但提交记录尚未保存时崩溃的测试。不能把内存字典去重描述为跨请求或跨进程的幂等承诺。

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

`push()` 返回成功只代表命令已进入内存队列，不代表 SQLite 已提交；当前通道没有持久化任务日志和逐命令提交确认。消费者随 API 进程运行，进程崩溃或重建时，在途写入没有可靠重放保证，守护进程的写入可用性也依赖该消费者。应分别设计入队确认、提交确认和重放幂等。

### 7.2 例外边界

启动早期迁移、测试初始化和需要写后立即读的交互路径使用 `sqlite_writes_bypass_queue()`，Invocation 合约注册、自动驾驶结果保存和多处 API 操作也使用它。环境变量 `PLOTPILOT_ALLOW_DIRECT_SQLITE_WRITES` 可以显式开启直写。

但 bypass 的作用域与注释不同：实现增加进程级全局整数 `_startup_sqlite_bootstrap_depth`，`allow_direct_sqlite_writes()` 只要看到大于零就放行，不核对调用线程或上下文。局部实验已确认：主线程进入 bypass 后，未进入该上下文的另一个线程也由禁止直写变成允许直写，退出后恢复。这个结论不依赖 `+= 1` 是否原子，也不需要证明上下文内存在 `await`。

因此当前只能说“统一连接层在 bypass 关闭时路由普通写入”，不能宣称所有运行期写入始终由唯一消费者提交。整改应先隔离 bypass 的调用作用域，收紧运行期使用，并区分启动迁移与业务写入；若选 `ContextVar`，还要验证上下文向后台任务和线程池的传播方式。原因枚举、调用点计数和告警只能辅助审计，不能替代并发隔离。

### 7.3 独立连接的不同语义

仅搜索到 `sqlite3.connect()` 不足以证明业务写入绕过门禁，需要检查 SQL 和调用方。

| 位置 | 已确认行为 | 证据边界 |
| --- | --- | --- |
| `context_slot_providers.build_key_props_slot_content()` | 查询道具，业务 SQL 为 `SELECT` | 还会应用连接 PRAGMA，不能据此推断任意业务写入 |
| `state_snapshot_manager.AtomicStateTransaction` | `BEGIN IMMEDIATE` 后直接 `INSERT/UPDATE` 并提交 | 实现确实绕过调度；本轮未找到模块外的生产调用，不能断言当前主链路在执行它 |
| `autopilot_routes._open_sqlite_diagnostic_connection()` 的调用方 | 查询可用性，或 `BEGIN IMMEDIATE` 后提交测试写锁 | 诊断会参与写锁竞争，但没有发现该处直接修改业务行 |
| `interfaces.runtime` 的关闭清理 | 执行 `PRAGMA wal_checkpoint(TRUNCATE)` | 属于数据库维护操作，需要与消费者关闭顺序一起审查 |

### 7.4 独立脚本的队列缺口

`scripts/start_daemon.py` 没有初始化队列、启动消费者或注入队列的显式调用。在关闭直写开关、未注册 writer、队列未就绪的受控条件下，直接调用 `DatabaseConnection.execute()` 的写分支会抛出“持久化队列未就绪，写 SQL 未能入队”；这个失败分支已局部复现，未触碰项目数据库。

这支持“独立启动路径的持久化装配缺少明确保证”，但不等于已经完整运行脚本并验证其第一个失败位置。应在临时数据目录中补完整启动与一条业务写入的冒烟测试，不能通过全局开放直写掩盖装配问题。

源码证据见 [`connection.py`](../infrastructure/persistence/database/connection.py)、[`write_dispatch.py`](../infrastructure/persistence/database/write_dispatch.py)、[`persistence_queue.py`](../application/engine/services/persistence_queue.py)、[`daemon_manager.py`](../interfaces/daemon_manager.py) 和 [`start_daemon.py`](../scripts/start_daemon.py)。

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

直接执行 `interfaces/main.py` 默认监听 `0.0.0.0:8000`，与指南中显式绑定 `127.0.0.1:8005` 的命令不同。部署时应核对监听范围和访问控制；本轮没有完成认证或网络安全审计，不能只凭监听地址判定存在可利用漏洞。

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
| 高 | bypass 在进程内放行无关线程 | 全局深度计数；跨线程局部实验复现 | 隔离作用域并验证线程、异步任务和嵌套退出 |
| 高 | 提交服务去重只在实例内，副作用先于防重落库 | API 每次创建新 service；数据库唯一键在保存时检查 | 重复请求、并发、崩溃窗口与 continuation 幂等测试 |
| 高 | 跨层具体依赖和 API 工厂阻碍迁移 | application→infrastructure 352 条、application→interfaces 16 条、engine→interfaces 3 条源码引用 | 分类协议/DTO/具体实现，建立允许例外的依赖基线并防止新增违规 |
| 高 | DAG 校验器允许 `gw_retry` 环，执行器校验拒绝所有环 | `validator.py` 与 `engine.py` 语义不同 | 为合法重试环和非法业务环分别补测试 |
| 高 | 运行状态和在途任务缺少统一持久化恢复 | invocation 内存服务、非持久化写入队列和章后辅助任务 | 分别验证会话恢复、写命令提交确认和辅助任务重放，不能用同一个内存重试策略代替 |
| 高 | 独立脚本未显式装配持久化队列 | 脚本缺初始化；未就绪分支已局部复现，完整启动未验证 | 隔离数据目录启动并完成一条业务写入 |
| 高 | 生产章节路径存在 StoryPipeline 与 legacy 分叉 | `writing_delegate.py` 通过环境变量选择 | 统计真实运行配置，明确 legacy 的退出时间 |
| 高 | DAG 测试不在后端 CI 命令中 | backend-ci 仅在匹配文件的 PR 上跑 `tests/unit` | 明确主分支触发策略，纳入 DAG 和必要集成检查，验证 Python 版本矩阵 |
| 高 | AI Invocation 变量快照契约仍在演进 | 初轮 86 个测试中 80 通过、6 失败，集中在别名扩展、scope/stage 推断和 continuation 输入校验 | 统一快照键策略，补重启恢复、故事级变量和幕规划 continuation 契约测试 |
| 中 | 质量失败有 warning 继续语义 | `BaseStoryPipeline` 和 `ChapterAftermathPipeline` | 为自动驾驶、人工辅助和强制提交分别定义策略矩阵 |
| 中 | DAG 事件可能被聚合器或前端队列丢弃 | `NodeEventAggregator` 和 `useDAGSSE` 都会合并或丢弃 | 验证 status 重同步能恢复所有权威字段 |
| 中 | Prompt 包声明与模板变量发生漂移 | `bible-worldbuilding` 合约测试失败 | 校准 package.yaml 与模板，增加 CI 合约检查 |
| 中 | 大型宿主、同步和路由模块提高拆分成本 | 1225–2855 行的热点文件及 runner 多重继承 | 先梳理调用和副作用，再按稳定协议拆分 |
| 中 | 独立 SQLite 连接与维护操作不受统一门禁覆盖 | 快照事务直接写入；诊断取写锁；关闭时 checkpoint | 确认活跃调用方，分别处理业务写入与维护操作 |
| 低 | 注册表副作用污染 DAG 构造测试 | 测试出现未预期 `test_node_e` | 明确测试注册隔离和 builtins 加载生命周期 |

## 13. 测试基线

初轮分析在 2026-11-24 记录了以下窄范围测试。本次评审复核未重新运行这六组，不将旧数字冒充新的执行结果；Claude 评审也明确没有运行这些测试。

| 测试 | 结果 | 说明 |
| --- | --- | --- |
| `pytest tests/unit/interfaces/api/test_architecture_boundaries.py -q` | 22 passed | 特定接口拆分、环境配置和禁止导入 `interfaces.main` 等规则通过，不代表全部层间依赖符合设计 |
| `pytest tests/unit/engine -q` | 114 passed | 引擎单元测试通过 |
| `pytest tests/unit/infrastructure/persistence -q` | 42 passed | SQLite、迁移和仓储测试通过 |
| `pytest tests/unit/infrastructure/ai -q` | 134 passed, 5 skipped, 1 failed | `bible-worldbuilding` 变量声明缺少 `fields_desc` 与 `genre_opening_profile` |
| `pytest tests/unit/application/ai_invocation -q` | 80 passed, 6 failed | 变量快照别名扩展、故事级 scope/stage 推断和幕规划 continuation 契约仍有失败用例 |
| `pytest tests/dag -q` | 75 passed, 14 failed | DAG 测试在构造默认节点时受未预期 `test_node_e` 注册影响，另需核对环语义 |

上述失败会作为当前仓库的待修复或待隔离问题保留在风险表中，相关能力按“部分落地”记录。

### 13.1 本次复核的执行证据

本次本机环境为 Windows、Python 3.12.7、pytest 8.4.2；不以它代表 README 所列 3.14 的兼容性。

| 检查 | 本次结果 | 能证明的范围 |
| --- | --- | --- |
| `python scripts/analyze_architecture.py` | 829 个跟踪文件均可解析，输出第 2 节的依赖与热点统计 | 静态源码引用，不能证明运行路径或全部分层违规 |
| `python -m pytest tests/unit/infrastructure/persistence/database/test_write_dispatch_startup_bypass.py -q --tb=short` | 3 passed | 原有启动 bypass、嵌套恢复与环境开关测试通过，未覆盖并发隔离 |
| `ThreadPoolExecutor` 跨线程局部实验 | 无关线程的直写许可为 `False → True → False` | 在显式关闭环境直写开关时，bypass 仍跨线程放行；未向数据库写数据 |
| 同一 decision 在两个 `AdoptionCommitService` 实例间重复提交 | 同实例复用 commit，新实例产生新 commit；模拟 continuation 共调用 2 次 | 复现服务实例边界的去重缺口；替换了 continuation，不代表已执行真实 HTTP 或业务副作用 |
| 队列未就绪的 `DatabaseConnection.execute()` 局部实验 | 写 SQL 在连接访问前抛出 `RuntimeError` | 验证缺队列的失败分支，不能代替独立守护进程完整启动测试 |

### 13.2 CI 覆盖的限制

[`backend-ci.yml`](../.github/workflows/backend-ci.yml) 只在匹配 Python、requirements 或 pytest 配置文件的 `pull_request` 上运行 `pytest tests/unit -q --tb=short`，没有普通 `push` 触发，也没有包含 `tests/dag` 和 `tests/integration`。其路径过滤还不包括 workflow 文件自身。这里只记录工作流定义，未查询 GitHub 分支保护或历史运行状态。

后端 CI 使用 Python 3.11，README 标示 3.14，发布工作流也使用 3.14；版本声明和验证范围没有对齐。这不证明 3.11 必然无法运行，也不证明本机 3.12 的通过结果能覆盖另外两个版本。应先明确支持版本，再建立对应矩阵与 DAG、必要集成测试的门禁。

## 14. 维护方法

后续每次深入项目时，按以下顺序更新本文件。

1. 先定位真实入口和调用方。
2. 再确认状态是否持久化，是否能重启恢复。
3. 再确认前端看到的是权威状态还是增量投影。
4. 用窄范围测试验证结论。
5. 运行 `python scripts/analyze_architecture.py`，区分职责、静态依赖和已执行的运行路径。
6. 把兼容层、失败测试、许可前提和未验证假设写入风险表，并保留新旧测试结果的时间边界。

这样可以避免把 README、设计注释、兼容入口和生产事实混成同一张架构图。
