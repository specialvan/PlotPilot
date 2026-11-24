import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from application.ai.llm_control_service import LLMControlConfig, LLMControlService, LLMProfile
from domain.ai.services.llm_service import DEFAULT_MAX_OUTPUT_TOKENS
from infrastructure.ai.llm_environment import ARK_DEFAULT_BASE_URL


LLM_ENV_NAMES = (
    "LLM_PROVIDER",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "gpt_API_KEY",
    "gpt_BASE_URL",
    "gpt_MODEL",
    "GEMINI_API_KEY",
    "GEMINI_BASE_URL",
    "GEMINI_MODEL",
    "ARK_API_KEY",
    "ARK_BASE_URL",
    "ARK_MODEL",
)


def _clear_llm_env(monkeypatch):
    for name in LLM_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_initial_config_prefers_anthropic_when_provider_unset(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "anthropic-token")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-test")

    config = LLMControlService()._build_initial_config()

    assert config.active_profile_id == "claude-official-default"
    active = config.profiles[1]
    assert active.api_key == "anthropic-token"
    assert active.model == "claude-test"


def test_initial_config_uses_openai_official_without_base_url(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-test")

    config = LLMControlService()._build_initial_config()

    active = config.profiles[0]
    assert config.active_profile_id == active.id
    assert active.preset_key == "openai-official"
    assert active.base_url == ""
    assert active.model == "gpt-test"


def test_initial_config_uses_custom_openai_when_base_url_present(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://gateway.example/v1")

    config = LLMControlService()._build_initial_config()

    active = config.profiles[0]
    assert active.preset_key == "custom-openai-compatible"
    assert active.base_url == "https://gateway.example/v1"


def test_initial_config_keeps_ark_default_base_url(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("ARK_API_KEY", "ark-key")
    monkeypatch.setenv("ARK_MODEL", "ark-model")

    config = LLMControlService()._build_initial_config()

    active = config.profiles[0]
    assert active.name == "豆包 / Ark"
    assert active.base_url == ARK_DEFAULT_BASE_URL
    assert active.model == "ark-model"


def test_initial_config_uses_gpt_gateway_alias_with_explicit_provider(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "other-provider-key")
    monkeypatch.setenv("gpt_API_KEY", "gateway-key")
    monkeypatch.setenv("gpt_BASE_URL", "https://gateway.example/v1")
    monkeypatch.setenv("gpt_MODEL", "gateway-model")
    service = LLMControlService()

    config = service._build_initial_config()
    active = service.resolve_active_profile(config)

    assert active is not None
    assert config.active_profile_id == "openai-compatible-default"
    assert active.protocol == "openai"
    assert active.preset_key == "custom-openai-compatible"
    assert active.api_key == "gateway-key"
    assert active.base_url == "https://gateway.example/v1"
    assert active.model == "gateway-model"


def test_profile_preserves_small_explicit_max_tokens():
    profile = LLMProfile(id="p", name="Profile", max_tokens=4096)

    assert profile.max_tokens == 4096


def test_row_to_profile_preserves_large_explicit_max_tokens():
    row = {
        "id": "p",
        "name": "Profile",
        "preset_key": "custom-openai-compatible",
        "protocol": "openai",
        "base_url": "",
        "api_key": "",
        "model": "",
        "temperature": 0.7,
        "max_tokens": DEFAULT_MAX_OUTPUT_TOKENS + 1000,
        "timeout_seconds": 300,
        "extra_headers": "{}",
        "extra_query": "{}",
        "extra_body": "{}",
        "notes": "",
        "use_legacy_chat_completions": 0,
    }

    profile = LLMControlService()._row_to_profile(row)

    assert profile.max_tokens == DEFAULT_MAX_OUTPUT_TOKENS + 1000


def _role_config(*, cheap_model: str = "cheap-model") -> LLMControlConfig:
    return LLMControlConfig(
        active_profile_id="main",
        endpoint_mode="independent",
        profiles=[
            LLMProfile(
                id="main",
                name="主力模型",
                api_key="main-key",
                base_url="https://gateway.example/v1",
                model="main-model",
            ),
            LLMProfile(
                id="cheap",
                name="经济模型",
                api_key="",
                base_url="",
                model=cheap_model,
            ),
        ],
    )


def test_independent_role_inherits_main_credentials_and_compatible_gateway():
    resolved = LLMControlService().resolve_role_profile("cheap", _role_config())

    assert resolved is not None
    assert resolved.id == "cheap"
    assert resolved.model == "cheap-model"
    assert resolved.api_key == "main-key"
    assert resolved.base_url == "https://gateway.example/v1"


def test_incomplete_independent_role_falls_back_to_main_profile():
    resolved = LLMControlService().resolve_role_profile(
        "cheap",
        _role_config(cheap_model=""),
    )

    assert resolved is not None
    assert resolved.id == "main"
    assert resolved.model == "main-model"


def test_unified_mode_ignores_independent_role_profile():
    config = _role_config()
    config.endpoint_mode = "unified"

    resolved = LLMControlService().resolve_role_profile("cheap", config)

    assert resolved is not None
    assert resolved.id == "main"


def test_save_and_reload_preserve_explicit_token_budget(tmp_path, monkeypatch):
    from infrastructure.persistence.database.connection import DatabaseConnection

    db = DatabaseConnection(str(tmp_path / "llm-control.db"))
    service = LLMControlService()
    monkeypatch.setattr(service, "_db", lambda: db)
    config = LLMControlConfig(profiles=[LLMProfile(id="p", name="Profile", max_tokens=512)])
    try:
        saved = service.save_config(config)
        loaded = service.get_config()
        assert saved.profiles[0].max_tokens == 512
        assert loaded.profiles[0].max_tokens == 512
    finally:
        db.close_all()


def _probe_profile(**overrides):
    return LLMProfile(id="p", name="Probe", api_key="test-key", model="test-model", **overrides)


@pytest.mark.asyncio
@pytest.mark.parametrize("close_fails", [False, True])
async def test_connectivity_probe_uses_small_budget_and_always_closes(close_fails):
    provider = SimpleNamespace(
        generate=AsyncMock(return_value=SimpleNamespace(content="OK")),
        aclose=AsyncMock(side_effect=RuntimeError("close failed") if close_fails else None),
    )
    result = await LLMControlService().test_profile_model(_probe_profile(), lambda _: provider)

    assert result.ok
    _, config = provider.generate.await_args.args
    assert config.max_tokens == 128
    assert config.temperature == 0
    provider.aclose.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("upstream failed"), TimeoutError()])
async def test_connectivity_probe_reports_failure_and_closes(error):
    provider = SimpleNamespace(generate=AsyncMock(side_effect=error), aclose=AsyncMock())

    result = await LLMControlService().test_profile_model(_probe_profile(), lambda _: provider)

    assert not result.ok
    assert result.error
    if isinstance(error, TimeoutError):
        assert "60" in result.error
        assert "超时" in result.error
    else:
        assert result.error == "upstream failed"
    provider.aclose.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout_seconds, expected", [(2, 2), (300, 60)])
async def test_connectivity_probe_enforces_total_timeout(monkeypatch, timeout_seconds, expected):
    timeouts = []
    real_timeout = asyncio.timeout
    cancelled = asyncio.Event()

    def immediate_timeout(seconds):
        timeouts.append(seconds)
        return real_timeout(0)

    async def blocked(*_):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr("application.ai.llm_control_service.asyncio.timeout", immediate_timeout)
    provider = SimpleNamespace(generate=AsyncMock(side_effect=blocked), aclose=AsyncMock())

    result = await LLMControlService().test_profile_model(
        _probe_profile(timeout_seconds=timeout_seconds), lambda _: provider,
    )

    assert not result.ok
    assert timeouts == [expected]
    assert cancelled.is_set()
    provider.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_connectivity_probe_propagates_cancellation_and_closes():
    entered = asyncio.Event()

    async def blocked(*_):
        entered.set()
        await asyncio.Event().wait()

    provider = SimpleNamespace(generate=AsyncMock(side_effect=blocked), aclose=AsyncMock())
    task = asyncio.create_task(LLMControlService().test_profile_model(_probe_profile(), lambda _: provider))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    provider.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_connectivity_probe_rejects_missing_credentials_without_creating_provider():
    factory = Mock()
    result = await LLMControlService().test_profile_model(LLMProfile(id="p", name="Probe"), factory)
    assert not result.ok
    factory.assert_not_called()
