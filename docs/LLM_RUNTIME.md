# LLM 配置与调用行为

本文说明模型档案、调用预算、OpenAI 兼容协议及流式输出的运行行为。启动方式和环境变量见 [README](../README.md#环境变量)。

## 档案与请求参数

- 环境变量仅在空数据库首次启动时初始化档案。已有档案请在「LLM 控制」中编辑、保存并激活。
- `max_tokens` 必须为正整数；显式设置的小预算会原样保留，不再被提升到长篇生成默认值 120000。已有档案中保存的 120000 不会自动调小。
- 通过 `DynamicLLMService` 调用时，未指定的预算和温度继承当前角色档案；显式传入的参数优先，包括恰好等于系统默认值的 120000 和 1.0。`LLMClient` 同样保留这一区别。
- 主力、经济、知识图谱角色分别缓存连接。档案参数、完整凭证或扩展参数变化后，后续请求使用新连接；正在生成或输出的旧连接会保留到最后一个使用者退出。缓存指纹使用摘要，不包含明文凭证。
- 连通性测试使用 128 个输出 token，生成阶段的总超时为档案超时与 60 秒中的较小值；成功、失败或取消后均尝试释放测试连接。短预算测试只能检查连接，不能证明长篇生成质量或稳定性。

## OpenAI 兼容协议

默认优先使用 Responses API；档案启用 `use_legacy_chat_completions` 后直接使用 Chat Completions。

Responses 请求使用 `max_output_tokens`，并将共享的 `response_format` 转换为 `text.format`。JSON Schema 的名称、schema、strict 和 description 会随请求发送。档案中的 `extra_headers`、`extra_query`、`extra_body` 及分层 HTTP 超时均会传入请求。

自动切换协议只针对可确认的端点不支持错误。凭证错误、模型不存在、参数或 schema 错误会向上返回，不会据此切换。端点能力按 Provider 实例、模型和流式模式分别记录，避免一次失败影响其它档案、模型或调用模式。Chat Completions 的 schema 格式降级仅在上游明确表示不支持该格式时进行；无效 schema 会报错。

OpenAI SDK 自带重试已关闭。非流式 Provider 的瞬时错误重试循环最多尝试三次（含首次）；协议兼容回退、空回复的流式聚合及外层结构化解析可能产生额外请求，因此这不是一次业务操作最多三次 HTTP 请求的承诺。流式输出开始后不会重放生成。

## 输出完成、失败与取消

- Responses 流必须到达 `response.completed`；Chat 流必须到达有效的 `finish_reason`。终止事件到达后即关闭流，不等待网关断开连接。
- `incomplete`、`failed`、拒绝输出、长度截断、内容过滤或缺少终止事件的断流会报错，即使之前已经收到部分正文。部分正文不能被当作完整成功结果。
- Responses 仅在未收到正文增量时使用完成快照补齐内容，避免重复输出；完成快照中的拒绝信息仍会被检查。
- 取消或显式关闭流会向下传递并释放该请求的流资源。取消一次请求不会关闭仍可复用的当前 Provider。调用方提前停止消费时，应在 `finally` 中调用 `await stream.aclose()`。
- 服务 `aclose()` 后拒绝新请求；已借出的连接在请求退出后关闭。连接关闭期间的普通清理异常会记录日志，保留原始调用结果或错误。

网关的非流式、流式、结构化输出和不同模型能力需分别验证。一次连通性测试通过不能证明这些能力全部可用。离线测试使用真实 SDK 配合内存 HTTP 传输，覆盖请求形状、失败、取消和连接释放；它们不证明实际网关的可用性。

## 维护验证

```bash
python -m pytest tests/unit/infrastructure/ai/test_dynamic_llm_service.py tests/unit/infrastructure/ai/providers/test_openai_transport_contract.py tests/unit/application/ai/test_llm_control_service.py tests/unit/application/ai/test_llm_retry_policy.py -q --tb=short
python -m pytest tests/unit tests/dag -q --tb=short
python scripts/analyze_architecture.py --check-baseline config/architecture_import_baseline.json
python scripts/check_docs.py
```

回归验证应使用独立数据目录，例如通过 `PLOTPILOT_PROD_DATA_DIR` 指向临时目录；不要用测试覆盖正在使用的模型档案或小说数据。
