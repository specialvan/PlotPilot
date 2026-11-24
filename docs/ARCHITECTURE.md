# PlotPilot（墨枢）架构

> **PlotPilot（墨枢）** 是面向长篇 AI 创作的剧情引擎内核。代码结构采用 DDD 分层，并在传统四层之外保留独立的 `engine/` 运行内核，用于承载生产守护进程、章节写作管线和题材扩展。产品说明与启动入口见根目录 [README.md](../README.md)。

状态由谁解释、候选何时成为正式正文、恢复与异步反馈有哪些保证，见 [工程模型与不变量](ENGINEERING_MODEL.md)；该文同时列出实现边界和可复核反例。

## 系统概览

- **输入**：书名、梗概、类型、章数、每章目标字数、风格提示。
- **输出**：完整的小说项目，包含 Bible（设定库）、Outline（大纲）、Beat Sheets（章纲）、Chapters（正文）。

## 架构分层

```
（项目根目录）/
├── domain/                 # 领域层 - 小说、Bible、人物、知识、记忆、道具等纯业务模型
│   ├── novel/             # 小说聚合根、章节、故事线、伏笔、因果与张力值对象
│   ├── bible/             # 设定库、人物档案、地点、世界设定
│   ├── character/         # 人物实体与关系能力
│   ├── knowledge/         # 知识图谱三元组
│   ├── memory/            # 叙事记忆与上下文长期状态
│   ├── prop/              # 道具生命周期与事件
│   └── shared/            # 共享内核（基类、异常、事件、ID）
│
├── application/           # 应用层 - 用例编排，协调领域模型、引擎运行时与基础设施
│   ├── core/              # 小说 / 章节 / 导出等基础用例
│   ├── onboarding/        # 新书向导、Bible 初始化、前置设定生成
│   ├── blueprint/         # 宏观规划、连续规划、Beat Sheet、故事结构
│   ├── engine/            # 上下文构建、章后管线、治理预算、AI 调用编排
│   ├── governance/        # 叙事治理、质量约束、章节预算
│   ├── audit/             # 章节审阅、宏观重构、章节元素分析
│   ├── analyst/           # 文风、张力、伏笔账本、叙事状态分析
│   ├── world/             # Bible、知识图谱、世界观与人物关系服务
│   └── workflows/         # 自动生成工作流、兼容编排与后台任务
│
├── engine/                # 剧情引擎内核 - 生产运行时、章节写作管线与题材扩展
│   ├── runtime/           # EngineDaemon、StoryPipelineRunner、守护进程委托、质量守门
│   ├── pipeline/          # BaseStoryPipeline 十步章节生成管线
│   ├── pipelines/         # 题材 Pipeline 注册与扩展
│   ├── core/              # 引擎侧实体、端口、服务契约
│   └── infrastructure/    # 引擎事件、记忆编排、checkpoint 适配
│
├── infrastructure/        # 基础设施层 - 技术实现
│   ├── ai/                # LLM Provider、Prompt Packages、向量存储、嵌入服务
│   ├── persistence/       # SQLite 仓储、迁移、Write Dispatch 单写者调度器
│   ├── export/            # DOCX / EPUB / PDF 导出
│   └── runtime/           # 数据目录、日志环境与进程级运行配置
│
├── interfaces/            # 接口层 - FastAPI、依赖注入、运行状态与外部边界
│   └── api/v1/            # REST API（core / world / blueprint / engine / audit / analyst 等）
│
├── frontend/              # 官方工作台 - Vue 3 + TypeScript + Tauri 桌面壳
└── shared/                # 跨端共享配置与分类体系资源
```

## 核心模块

### Domain 层

| 模块 | 职责 |
|------|------|
| `novel/` | 小说聚合根、章节实体、故事线、伏笔注册表 |
| `bible/` | 设定库、人物实体（含 POV 防火墙）、地点、时间线 |
| `character/` | 人物实体、关系与调度相关模型 |
| `knowledge/` | 知识三元组、故事知识 |
| `memory/` | 长期记忆、叙事状态与上下文相关模型 |
| `prop/` | 道具生命周期与道具事件 |
| `ai/` | LLM 服务接口、提示词值对象、Token 使用统计 |

### Application 层

| 模块 | 职责 |
|------|------|
| `core/` | 小说/章节的 CRUD 服务 |
| `blueprint/` | 宏观规划（部-卷-幕）、幕级规划（章节规划）|
| `engine/` | 上下文构建、章后管线、治理预算、AI 调用编排 |
| `governance/` | 叙事治理、章节预算、质量约束 |
| `world/` | Bible 管理、知识图谱构建、人物关系 |
| `audit/` | 章节审阅、宏观重构、陈词滥调扫描 |

### Engine 层

| 模块 | 职责 |
|------|------|
| `runtime/` | `EngineDaemon`、`StoryPipelineRunner`、守护进程委托与质量守门 |
| `pipeline/` | `BaseStoryPipeline` 十步章节生成管线 |
| `pipelines/` | 题材 Pipeline 注册与扩展 |
| `core/` | 引擎侧实体、端口和服务契约 |

### Infrastructure 层

| 模块 | 职责 |
|------|------|
| `ai/llm_client.py` | 动态 LLM provider 包装，统一生成与流式调用 |
| `ai/chromadb_vector_store.py` | FAISS 与 JSON 元数据存储，保留 ChromaDB 兼容命名 |
| `ai/local_embedding_service.py` | 本地嵌入模型 |
| `persistence/database/` | SQLite 仓储实现 |

## 数据流

### 自动驾驶模式

```
1. 宏观规划 → 生成部-卷-幕结构
2. 幕级规划 → 为当前幕生成章节大纲
3. EngineDaemon → StoryPipelineRunner 调度章节写作
4. BaseStoryPipeline → 治理预算、章节计划、上下文装配、正文生成
5. 章后管线 → 摘要、事件、因果边、伏笔、人物状态、知识图谱与向量索引更新
6. 审阅审计 → 文风检测、张力评分、一致性检查、状态落库
7. 循环至完成
```

### 人工辅助模式

```
用户创建小说 → 手动规划 → 手动撰写 → AI 辅助生成
```

## 入口点

在**仓库根目录**（含 `application/`、`interfaces/`；本文件在 `docs/` 下）执行：

```bash
# 推荐：与 README 一致，端口 8005
uvicorn interfaces.main:app --host 127.0.0.1 --port 8005 --reload
```

可选方式：

```bash
# 直接运行 FastAPI 模块（默认 127.0.0.1:8000；监听地址由 PLOTPILOT_BIND_HOST 控制）
python interfaces/main.py

# EngineDaemon 守护进程（当前维护入口）
python scripts/start_daemon.py

# 临时数据目录中的完整启动、常规队列写入、读取和停止验证，不调用真实 LLM
python scripts/start_daemon.py --smoke-test
```

独立脚本在启动阶段初始化数据库，随后注册并启动本进程的 SQLite 消费者；关闭时先停止 daemon，再排空队列和关闭数据库。`--smoke-test` 在独立子进程中设置临时数据目录、关闭环境直写和向量存储，退出后删除临时目录。

## 写入与采纳提交边界

`write_dispatch` 用 `ContextVar[int]` 隔离 bypass 深度，启动包装器仍可使用。运行期 bypass 必须提供非空 `reason`，每次进入都会计数并记录日志。`asyncio.to_thread` 会复制上下文，`run_in_executor` 和裸线程不会自动复制；不要把 bypass 当作后台任务的默认授权。Invocation API 原有 14 处 bypass 已改为等待 writer 完成，异步路由将阻塞操作交给线程。

生产采纳提交先用 `(session_id, decision_id)` 幂等键创建记录，再执行 continuation。执行期间使用本机文件锁串行化同一 decision，每个步骤先记录 running，完成后记录结果；重试复用 succeeded 步骤和最终结果。`save_commit` 按幂等键更新并保留原始 ID。daemon 子进程通过带响应的队列请求向 SQLite 所在进程提交；协调线程在 writer 外等待锁，再把各数据库操作交给 writer。

步骤回执不能自动保证副作用只发生一次。continuation 只有实现自身幂等并显式声明 `retry_safe=True` 后才可恢复执行；未声明的中断步骤要求先补偿。正文写入与局部回执、章节计数和节拍进度在同一事务提交，章节投影也有事务回执；变量输出以 `source_commit_id` 去重。文件锁约束限于共享同一本机数据目录的进程，不保证网络文件系统部署。普通 SQL 队列仍是内存通道，入队成功不等于落库，也没有持久化任务重放日志。

## 依赖增量门禁

`config/architecture_import_baseline.json` 固定整改前 `47959b8e` 中 application→infrastructure 的 352 条 import。检查按文件、AST 规范化语句和重复次数比较，允许减少，不允许以删旧换新抵消新增。运行 `python scripts/analyze_architecture.py --check-baseline config/architecture_import_baseline.json`；后端 CI 同时检查该基线并运行 `tests/unit` 与 `tests/dag`。基线用于限制耦合增长，现有依赖仍需逐步迁移到端口。

## Web 前端（Vue 3）

- **目录**：`frontend/`
- **技术栈**：Vue 3 + Vite + Naive UI + ECharts
- **默认端口**：3000
- **API 代理**：`/api` → `http://localhost:8005`

```bash
cd frontend
npm install
npm run dev
```

## 测试

```bash
# 运行所有测试
python -m pytest tests -v

# 单元测试
python -m pytest tests/unit -v

# 集成测试
python -m pytest tests/integration -v
```

## 环境变量

以根目录 **[.env.example](../.env.example)** 为准（方舟 `ARK_*`、嵌入 `EMBEDDING_*`、`LOG_*`、`PLOTPILOT_PROD_DATA_DIR` 等；旧名 `AITEXT_PROD_DATA_DIR` 仍兼容）。复制为 `.env` 后按需填写，勿提交密钥。

## 数据库与数据目录

- **主数据库**：默认 SQLite 文件名为 `data/plotpilot.db`（旧版为 `aitext.db`，`get_db_path()` 会自动沿用）；实际目录由 `application.paths.DATA_DIR` 解析（未设置 `PLOTPILOT_PROD_DATA_DIR` / 旧名 `AITEXT_PROD_DATA_DIR` 且非冻结运行时指向仓库内 `data/`）。
- **向量存储**：默认在 `data/chromadb/` 下持久化（实现为本地 FAISS + 元数据，与 `.env` 中 `VECTOR_STORE_TYPE=chromadb` 对应）。
- **应用日志**：默认 `logs/plotpilot.log`（由 `LOG_FILE` 控制，见 `.env.example`）。
