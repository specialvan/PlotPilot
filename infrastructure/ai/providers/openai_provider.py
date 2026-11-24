"""OpenAI LLM 提供商实现"""
import asyncio
import inspect
import json
import logging
import openai
import httpx
from typing import Any, AsyncIterator

from openai import AsyncOpenAI

from domain.ai.services.llm_service import GenerationConfig, GenerationResult
from domain.ai.value_objects.prompt import Prompt
from domain.ai.value_objects.token_usage import TokenUsage
from application.ai.llm_retry_policy import LLM_MAX_TOTAL_ATTEMPTS, is_retryable_llm_error
from infrastructure.ai.config.settings import Settings
from infrastructure.ai.http_timeout import build_httpx_timeout
from .base import BaseProvider
from .model_resolution import require_resolved_model_id

logger = logging.getLogger(__name__)


class OpenAIProvider(BaseProvider):
    """OpenAI LLM 提供商实现

    通过 use_legacy_chat_completions 显式选择协议：
    - False（默认）：走 Responses API，仅在确认端点不支持时降级到 Chat Completions
    - True：走 Chat Completions API
    """

    def __init__(self, settings: Settings):
        super().__init__(settings)

        if not settings.api_key:
            raise ValueError("API key is required for OpenAIProvider")

        self._use_legacy = settings.use_legacy_chat_completions
        # Capabilities belong to this profile, model, and request mode. One bad
        # key/model/format must not downgrade every user of the same gateway.
        self._responses_unsupported: set[tuple[str, bool]] = set()
        self._schema_unsupported: set[str] = set()
        self._request_timeout = build_httpx_timeout(settings.http_timeout_settings)

        client_kwargs = {
            "api_key": settings.api_key,
            # Settings is already resolved from a single profile. Omitting this
            # lets the SDK silently inherit an unrelated OPENAI_BASE_URL.
            "base_url": settings.base_url or "https://api.openai.com/v1",
            "timeout": self._request_timeout,
            # Retry policy is owned by this provider/pipeline. Leaving the SDK
            # default (2) would multiply the provider's own retry budget.
            "max_retries": 0,
            "default_headers": settings.extra_headers or None,
            "default_query": settings.extra_query or None,
        }
        self._http_client = httpx.AsyncClient(
            timeout=self._request_timeout,
            trust_env=False,
        )
        client_kwargs["http_client"] = self._http_client
        self.async_client = AsyncOpenAI(**client_kwargs)

    async def generate(
        self,
        prompt: Prompt,
        config: GenerationConfig
    ) -> GenerationResult:
        for attempt in range(LLM_MAX_TOTAL_ATTEMPTS):
            try:
                capability = (self._model_id(config), False)
                if not self._use_legacy and capability not in self._responses_unsupported:
                    try:
                        return await self._generate_via_responses(prompt, config)
                    except Exception as exc:
                        if not self._responses_endpoint_unsupported(exc):
                            raise
                        self._responses_unsupported.add(capability)
                        logger.info("Responses endpoint unavailable; using Chat for model=%s", capability[0])
                return await self._generate_via_chat(prompt, config)
            except Exception as exc:
                if attempt < LLM_MAX_TOTAL_ATTEMPTS - 1 and is_retryable_llm_error(exc):
                    delay = min(1.5 * (2 ** attempt), 8.0)
                    logger.warning("LLM transient failure; retrying in %.1fs (attempt=%d/%d): %s",
                                   delay, attempt + 1, LLM_MAX_TOTAL_ATTEMPTS, self._error_description(exc))
                    await asyncio.sleep(delay)
                    continue
                if isinstance(exc, (RuntimeError, ValueError)):
                    raise
                raise RuntimeError(f"Failed to generate text: {self._error_description(exc)}") from exc
        raise RuntimeError("LLM retry budget exhausted")

    async def _generate_via_chat(self, prompt: Prompt, config: GenerationConfig) -> GenerationResult:
        """Chat generation, with targeted schema and empty-body compatibility."""
        messages = self._build_messages(prompt)
        request_kwargs = self._build_chat_request_kwargs(messages, config)
        response = await self._create_chat(request_kwargs, config)
        self._validate_chat_finish(response)
        content = self._extract_text_from_response(response)
        if not content:
            logger.warning("Chat returned empty non-stream content; aggregating stream")
            content, token_usage = await self._generate_via_stream(request_kwargs)
            return GenerationResult(content=content, token_usage=token_usage)

        usage = self._response_field(response, "usage", None)
        return GenerationResult(
            content=content,
            token_usage=TokenUsage(
                input_tokens=self._usage_tokens(usage, "prompt_tokens", "input_tokens"),
                output_tokens=self._usage_tokens(usage, "completion_tokens", "output_tokens"),
            ),
        )

    async def stream_generate(
        self,
        prompt: Prompt,
        config: GenerationConfig
    ) -> AsyncIterator[str]:
        stream = None
        try:
            capability = (self._model_id(config), True)
            if not self._use_legacy and capability not in self._responses_unsupported:
                emitted = False
                terminal = False
                try:
                    request_kwargs = self._build_responses_request_kwargs(prompt, config, stream=True)
                    stream = await self.async_client.responses.create(**request_kwargs)
                    async for chunk in stream:
                        event = self._response_field(chunk, "type", "")
                        if event in {"response.refusal.delta", "response.refusal.done"}:
                            raise RuntimeError("Responses API refused the requested output")
                        if event in {"response.failed", "response.incomplete", "response.cancelled", "error"}:
                            response = self._response_field(chunk, "response", chunk)
                            self._validate_responses_status(response, fallback_status=event)
                        if event == "response.completed":
                            response = self._response_field(chunk, "response", None)
                            content = ""
                            if response is not None:
                                self._validate_responses_status(response)
                                # Validate the final snapshot even after text
                                # deltas: a refusal must never become success.
                                content = self._extract_text_from_responses_response(response)
                            # Some compatible gateways send a complete snapshot
                            # without any deltas. Never replay it after deltas.
                            if not emitted and content:
                                emitted = True
                                yield content
                            terminal = True
                            break
                        content = self._extract_text_from_responses_chunk(chunk)
                        if content:
                            emitted = True
                            yield content
                    if not terminal:
                        raise RuntimeError("Responses stream ended before response.completed")
                    if not emitted:
                        raise RuntimeError("Responses API returned empty content")
                    return
                except Exception as exc:
                    # Replaying a generation after emitting a prefix corrupts
                    # output. Only a confirmed missing endpoint may switch API.
                    if emitted or not self._responses_endpoint_unsupported(exc):
                        raise
                    self._responses_unsupported.add(capability)
                    logger.info("Responses stream endpoint unavailable; using Chat for model=%s", capability[0])
                finally:
                    await self._close_stream(stream)
                    stream = None

            messages = self._build_messages(prompt)
            request_kwargs = self._build_chat_request_kwargs(messages, config, stream=True)
            stream = await self._create_chat(request_kwargs, config)
            emitted = False
            terminal = False
            async for chunk in stream:
                finish_reason = self._validate_chat_finish(chunk)
                content = self._extract_text_from_stream_chunk(chunk)
                if content:
                    emitted = True
                    yield content
                if finish_reason:
                    terminal = True
                    break
            if not terminal:
                raise RuntimeError("Chat stream ended before finish_reason")
            if not emitted:
                raise RuntimeError("Chat API returned empty content")
        except Exception as exc:
            raise RuntimeError(f"Failed to stream text: {self._error_description(exc)}") from exc
        finally:
            await self._close_stream(stream)

    @staticmethod
    def _build_messages(prompt: Prompt) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": prompt.system},
            {"role": "user", "content": prompt.user}
        ]

    def _model_id(self, config: GenerationConfig) -> str:
        return require_resolved_model_id(config.model, self.settings.default_model, provider_label="OpenAI 兼容")

    @staticmethod
    def _error_description(exc: Exception) -> str:
        return f"{type(exc).__name__}: {str(exc).strip() or 'upstream request failed'}"

    @staticmethod
    def _error_body(exc: Exception) -> str:
        body = getattr(exc, "body", None)
        return (json.dumps(body, default=str) if body else str(exc)).lower()

    @classmethod
    def _responses_endpoint_unsupported(cls, exc: Exception) -> bool:
        if not isinstance(exc, openai.APIStatusError):
            return False
        status = exc.status_code
        detail = cls._error_body(exc)
        # Missing model, invalid schema, auth, quota and context-length errors
        # are request failures, not evidence that Responses is unavailable.
        if "model" in detail or "schema" in detail or "response_format" in detail:
            return False
        if status in {404, 405, 501}:
            return True
        return status == 400 and "responses" in detail and any(
            marker in detail for marker in ("not supported", "unsupported endpoint", "unknown endpoint", "unknown path")
        )

    @classmethod
    def _schema_format_unsupported(cls, exc: Exception) -> bool:
        if not isinstance(exc, openai.APIStatusError) or exc.status_code not in {400, 422}:
            return False
        detail = cls._error_body(exc)
        if "invalid schema" in detail or "invalid_json_schema" in detail:
            return False
        return any(field in detail for field in ("json_schema", "response_format")) and any(
            marker in detail for marker in ("not supported", "unsupported", "not support")
        )

    async def _create_chat(self, request_kwargs: dict[str, Any], config: GenerationConfig):
        try:
            return await self.async_client.chat.completions.create(**request_kwargs)
        except Exception as exc:
            fmt = request_kwargs.get("response_format") or {}
            if fmt.get("type") != "json_schema" or not self._schema_format_unsupported(exc):
                raise
            self._schema_unsupported.add(self._model_id(config))
            request_kwargs["response_format"] = {"type": "json_object"}
            logger.info("Model does not support json_schema; using json_object for model=%s", self._model_id(config))
            return await self.async_client.chat.completions.create(**request_kwargs)

    @classmethod
    def _validate_responses_status(cls, response: Any, *, fallback_status: str = "") -> None:
        status = cls._response_field(response, "status", "") or fallback_status
        if status and status != "completed":
            incomplete = cls._response_field(response, "incomplete_details", None)
            reason = cls._response_field(incomplete, "reason", "") if incomplete else ""
            error = cls._response_field(response, "error", None) or response
            message = cls._response_field(error, "message", "")
            details = ", ".join(str(part) for part in (status, reason, message) if part)
            raise RuntimeError(f"Responses API did not complete ({details})")

    @classmethod
    def _validate_chat_finish(cls, response: Any) -> str:
        choices = cls._response_field(response, "choices", None) or []
        if not choices:
            return ""
        reason = cls._response_field(choices[0], "finish_reason", "") or ""
        if reason in {"length", "content_filter"}:
            raise RuntimeError(f"Chat API did not complete (finish_reason={reason})")
        message = cls._response_field(choices[0], "message", None) or cls._response_field(choices[0], "delta", None)
        if cls._response_field(message, "refusal", None):
            raise RuntimeError("Chat API refused the requested output")
        return reason

    @staticmethod
    async def _close_stream(stream: Any) -> None:
        if stream is None:
            return
        close = getattr(stream, "close", None) or getattr(stream, "aclose", None)
        if callable(close):
            try:
                result = close()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                # Cleanup failures must not hide an upstream failure or replace
                # a completed answer with a transport-close error.
                logger.debug("Failed to close upstream LLM stream", exc_info=True)

    def _build_chat_request_kwargs(
        self,
        messages: list[dict[str, str]],
        config: GenerationConfig,
        *,
        stream: bool = False,
    ) -> dict[str, Any]:
        model_id = require_resolved_model_id(
            config.model,
            self.settings.default_model,
            provider_label="OpenAI 兼容",
        )
        kwargs: dict[str, Any] = {
            "model": model_id,
            "messages": messages,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            "extra_headers": self.settings.extra_headers or None,
            "extra_query": self.settings.extra_query or None,
            "extra_body": self.settings.extra_body or None,
            "timeout": self._request_timeout,
        }

        # 🔥 response_format 自适应降级策略
        # 不同网关对 response_format 的支持程度不同：
        #   OpenAI 官方: json_schema ✅ | json_object ✅
        #   DeepSeek:     json_schema ❌ | json_object ✅
        #   Qwen/DashScope: json_schema ❌ | json_object ✅ (部分)
        #   豆包/Ark:     json_schema ❌ | json_object ✅ (部分)
        #   智谱/GLM:     json_schema ❌ | json_object ✅
        if config.response_format:
            fmt = config.response_format
            if fmt.get("type") == "json_schema" and model_id in self._schema_unsupported:
                kwargs["response_format"] = {"type": "json_object"}
            else:
                kwargs["response_format"] = fmt

        if stream:
            kwargs["stream"] = True
        return kwargs

    def _build_responses_request_kwargs(
        self,
        prompt: Prompt,
        config: GenerationConfig,
        *,
        stream: bool = False,
    ) -> dict[str, Any]:
        model_id = require_resolved_model_id(
            config.model,
            self.settings.default_model,
            provider_label="OpenAI 兼容",
        )
        kwargs: dict[str, Any] = {
            "model": model_id,
            "instructions": prompt.system,
            "input": [{"role": "user", "content": prompt.user}],
            "temperature": config.temperature,
            "max_output_tokens": config.max_tokens,
            "extra_headers": self.settings.extra_headers or None,
            "extra_query": self.settings.extra_query or None,
            "extra_body": self.settings.extra_body or None,
            "timeout": self._request_timeout,
        }
        response_format = self._responses_text_format(config.response_format)
        if response_format is not None:
            kwargs["text"] = {"format": response_format}
        if stream:
            kwargs["stream"] = True
        return kwargs

    @staticmethod
    def _responses_text_format(response_format: dict[str, Any] | None) -> dict[str, Any] | None:
        """Translate the shared Chat Completions format to Responses ``text``.

        The application keeps one provider-neutral contract. Responses uses a
        different JSON-schema envelope, so passing the Chat shape through would
        silently drop structured-output guarantees at OpenAI-compatible gateways.
        """
        if not response_format:
            return None
        fmt_type = response_format.get("type")
        if fmt_type in {"text", "json_object"}:
            return {"type": fmt_type}
        if fmt_type != "json_schema":
            raise ValueError(f"Unsupported response_format type: {fmt_type}")
        schema_payload = response_format.get("json_schema") or {}
        schema = schema_payload.get("schema")
        if not isinstance(schema, dict):
            raise ValueError("json_schema response_format requires a schema object")
        name = str(schema_payload.get("name") or "response").strip() or "response"
        result = {
            "type": "json_schema",
            "name": name,
            "schema": schema,
        }
        for field in ("strict", "description"):
            if field in schema_payload:
                result[field] = schema_payload[field]
        return result

    async def _generate_via_responses(self, prompt: Prompt, config: GenerationConfig) -> GenerationResult:
        """Responses API 非流式生成"""
        request_kwargs = self._build_responses_request_kwargs(prompt, config)
        response = await self.async_client.responses.create(**request_kwargs)
        self._validate_responses_status(response)

        content = self._extract_text_from_responses_response(response)
        if not content:
            status = self._response_field(response, "status", "unknown")
            incomplete = self._response_field(response, "incomplete_details", None)
            reason = self._response_field(incomplete, "reason", "") if incomplete else ""
            details = f"status={status}"
            if reason:
                details += f", incomplete_reason={reason}"
            raise RuntimeError(f"Responses API returned empty content ({details})")

        usage = self._response_field(response, "usage", None)
        input_tokens = self._usage_tokens(usage, "input_tokens", "prompt_tokens")
        output_tokens = self._usage_tokens(usage, "output_tokens", "completion_tokens")

        return GenerationResult(
            content=content,
            token_usage=TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens)
        )

    @staticmethod
    def _response_field(value: Any, name: str, default: Any = None) -> Any:
        if isinstance(value, dict):
            return value.get(name, default)
        return getattr(value, name, default)

    @classmethod
    def _extract_text_from_responses_response(cls, response: Any) -> str:
        """Extract user-visible text from official SDK and compatible gateways.

        The Responses API uses ``output_text`` content parts.  Some compatible
        gateways still emit the older ``text`` spelling, so both are accepted.
        The SDK's aggregate ``response.output_text`` is preferred when present.
        """
        content_parts: list[str] = []
        for item in cls._response_field(response, "output", None) or []:
            if cls._response_field(item, "type", "") != "message":
                continue
            for part in cls._response_field(item, "content", None) or []:
                part_type = cls._response_field(part, "type", "")
                if part_type == "refusal":
                    raise RuntimeError("Responses API refused the requested output")
                if part_type not in {"output_text", "text"}:
                    continue
                piece = cls._response_field(part, "text", "")
                if isinstance(piece, str) and piece.strip():
                    content_parts.append(piece.strip())
        aggregate = cls._response_field(response, "output_text", "")
        if isinstance(aggregate, str) and aggregate.strip():
            return aggregate.strip()
        return "\n".join(content_parts).strip()

    @classmethod
    def _usage_tokens(cls, usage: Any, primary_name: str, legacy_name: str) -> int:
        if usage is None:
            return 0
        value = cls._response_field(usage, primary_name, None)
        if value is None:
            value = cls._response_field(usage, legacy_name, 0)
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _extract_text_from_responses_chunk(cls, chunk: Any) -> str:
        """原生 Responses stream 解析封装"""
        try:
            event_type = cls._response_field(chunk, "type", "")
            if event_type == "response.output_text.delta":
                delta = cls._response_field(chunk, "delta", None)
                if isinstance(delta, str):
                    return delta
            elif event_type in ("response.content_part.added", "response.content_part.delta"):
                part = cls._response_field(chunk, "part", None)
                if part and cls._response_field(part, "type", "") in {"output_text", "text"}:
                    return cls._response_field(part, "text", "")
                delta = cls._response_field(chunk, "delta", None)
                if isinstance(delta, str):
                    return delta
            elif event_type == "message.delta":
                delta = cls._response_field(chunk, "delta", None)
                if delta:
                     content = getattr(delta, "content", None)
                     if isinstance(content, str):
                         return content
        except Exception:
            pass
        return ""

    @staticmethod
    def _normalize_chat_completion_content(content: Any) -> str:
        """兼容 message.content 为 str 或多段 content part 列表。

        🔥 自适应兼容多种网关的响应格式：
        1. 标准 OpenAI: content 是 str
        2. OpenAI 新协议: content 是 list[{type, text}]
        3. DeepSeek-R1: content 是 str，但 message.reasoning_content 单独存在
        4. 部分网关: content 是 list，包含 thinking/reasoning 类型 part
        5. 极端情况: content 是 None（空回复）
        """
        if content is None:
            return ""
        if isinstance(content, str):
            return content.strip()

        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    item_type = (item.get("type") or "").lower()
                    # 🔥 跳过推理/思考/拒绝类型的 content part
                    # DeepSeek-R1: reasoning_content 在 message 级别，不在 content part 中
                    # 但部分网关会把 thinking 放在 content part 里
                    if item_type in ("reasoning", "thinking", "refusal", "thought"):
                        continue
                    text_val = item.get("text")
                    if isinstance(text_val, str) and text_val.strip():
                        parts.append(text_val)
                else:
                    text_attr = getattr(item, "text", None)
                    if isinstance(text_attr, str) and text_attr.strip():
                        parts.append(text_attr)
            return "\n".join(parts).strip()

        return str(content).strip()

    @staticmethod
    def _extract_text_from_response(response: Any) -> str:
        if not getattr(response, "choices", None):
            return ""

        message = getattr(response.choices[0], "message", None)
        content = getattr(message, "content", None)
        result = OpenAIProvider._normalize_chat_completion_content(content)

        # 🔥 DeepSeek-R1 等模型：正文在 message.content，思考在 message.reasoning_content
        # reasoning_content 不需要返回给调用方（已经由 llm_output_sanitize 处理）
        # 但如果 content 为空且 reasoning_content 不为空，说明模型只输出了思考没有正文
        if not result.strip():
            reasoning = getattr(message, "reasoning_content", None)
            if reasoning and isinstance(reasoning, str) and reasoning.strip():
                logger.debug("message.content 为空但有 reasoning_content，模型可能只输出了推理")

        return result

    @staticmethod
    def _extract_text_from_stream_chunk(chunk: Any) -> str:
        if not getattr(chunk, "choices", None):
            return ""

        delta = getattr(chunk.choices[0], "delta", None)
        content = getattr(delta, "content", None)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return OpenAIProvider._normalize_chat_completion_content(content)
        return ""

    async def _generate_via_stream(self, request_kwargs: dict[str, Any]) -> tuple[str, TokenUsage]:
        stream = await self.async_client.chat.completions.create(
            **{**request_kwargs, "stream": True}
        )

        parts: list[str] = []
        input_tokens = 0
        output_tokens = 0

        terminal = False
        try:
            async for chunk in stream:
                finish_reason = self._validate_chat_finish(chunk)
                if finish_reason:
                    terminal = True
                content = self._extract_text_from_stream_chunk(chunk)
                if content:
                    parts.append(content)

                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    input_tokens = self._usage_tokens(usage, "prompt_tokens", "input_tokens")
                    output_tokens = self._usage_tokens(usage, "completion_tokens", "output_tokens")
                if finish_reason:
                    break
        finally:
            await self._close_stream(stream)

        content = "".join(parts).strip()
        if not content:
            raise RuntimeError("API returned empty content")
        if not terminal:
            raise RuntimeError("Chat stream ended before finish_reason")

        return content, TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
