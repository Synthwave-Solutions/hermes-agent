"""Run agent requests: behavior contracts."""
from __future__ import annotations

import tests.agent.test_run_agent as _support

pytest_plugins = ["tests.agent.test_run_agent"]


class TestBuildApiKwargs:
    def test_basic_kwargs(self, agent):
        messages = [{"role": "user", "content": "hi"}]
        kwargs = agent._build_api_kwargs(messages)
        assert kwargs["model"] == agent.model
        assert kwargs["messages"] is messages

    def test_explicit_request_local_tools_reach_native_transport(self, agent, monkeypatch):
        from agent.prompt_caching import build_prompt_cache_plan

        canonical_tools = [
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        plan = build_prompt_cache_plan(
            [{"role": "system", "content": "stable\nvolatile"}, {"role": "user", "content": "lookup"}],
            canonical_tools,
            native_anthropic=True,
            static_system_prefix="stable",
            direct_native_tool_cache=True,
        )
        transport = _support.MagicMock()
        transport.build_kwargs.side_effect = lambda **kwargs: kwargs
        agent.api_mode = "anthropic_messages"
        agent.provider = "anthropic"
        agent.base_url = "https://api.anthropic.com"
        monkeypatch.setattr(agent, "_get_transport", lambda: transport)
        monkeypatch.setattr(agent, "_prepare_anthropic_messages_for_api", lambda messages: messages)

        kwargs = agent._build_api_kwargs(plan.messages, tools_for_api=plan.tools)

        assert "cache_control" not in canonical_tools[-1]
        assert kwargs["tools"][-1]["cache_control"] == {"type": "ephemeral"}

    def test_public_moonshot_kimi_k2_5_omits_temperature(self, agent):
        """Kimi models should NOT have client-side temperature overrides.

        The Kimi gateway selects the correct temperature server-side.
        """
        agent.base_url = "https://api.moonshot.ai/v1"
        agent._base_url_lower = agent.base_url.lower()
        agent.model = "kimi-k2.5"
        messages = [{"role": "user", "content": "hi"}]

        kwargs = agent._build_api_kwargs(messages)

        assert "temperature" not in kwargs


    def test_kimi_coding_endpoint_disables_thinking(self, agent):
        """When reasoning_config.enabled=False, thinking should be disabled
        and reasoning_effort should be omitted entirely — mirroring Kimi
        CLI's with_thinking("off") which maps to reasoning_effort=None."""
        agent.provider = "kimi-coding"
        agent.base_url = "https://api.kimi.com/coding/v1"
        agent._base_url_lower = agent.base_url.lower()
        agent.model = "kimi-for-coding"
        agent.reasoning_config = {"enabled": False}
        messages = [{"role": "user", "content": "hi"}]

        kwargs = agent._build_api_kwargs(messages)

        assert kwargs["extra_body"]["thinking"] == {"type": "disabled"}
        assert "reasoning_effort" not in kwargs


    def test_provider_preferences_injected(self, agent):
        agent.provider = "openrouter"
        agent.base_url = "https://openrouter.ai/api/v1"
        agent.providers_allowed = ["Anthropic"]
        messages = [{"role": "user", "content": "hi"}]
        kwargs = agent._build_api_kwargs(messages)
        assert kwargs["extra_body"]["provider"]["only"] == ["Anthropic"]


    def test_reasoning_config_default_openrouter(self, agent):
        """Default reasoning config for OpenRouter should be medium."""
        agent.provider = "openrouter"
        agent.base_url = "https://openrouter.ai/api/v1"
        agent.model = "anthropic/claude-sonnet-4-20250514"
        messages = [{"role": "user", "content": "hi"}]
        kwargs = agent._build_api_kwargs(messages)
        reasoning = kwargs["extra_body"]["reasoning"]
        assert reasoning["enabled"] is True


    def test_reasoning_not_sent_for_unsupported_openrouter_model(self, agent):
        agent.base_url = "https://openrouter.ai/api/v1"
        agent.model = "minimax/minimax-m2.5"
        messages = [{"role": "user", "content": "hi"}]
        kwargs = agent._build_api_kwargs(messages)
        assert "reasoning" not in kwargs.get("extra_body", {})


    def test_reasoning_sent_for_copilot_gpt5(self, agent):
        """Copilot/GitHub Models: GPT-5 reasoning goes in extra_body.reasoning."""
        from agent.transports import get_transport
        from providers import get_provider_profile

        transport = get_transport("chat_completions")
        profile = get_provider_profile("copilot")
        msgs = [{"role": "user", "content": "hi"}]
        kwargs = transport.build_kwargs(
            model="gpt-5.4",
            messages=msgs,
            tools=None,
            supports_reasoning=True,
            provider_profile=profile,
        )
        assert kwargs["extra_body"]["reasoning"] == {"effort": "medium"}


    def test_core_responses_preserves_supported_xhigh(self, agent, monkeypatch):
        """The core GitHub Responses path must preserve a supported xhigh."""
        monkeypatch.setattr(
            "hermes_cli.models.github_model_reasoning_efforts",
            lambda _model: ["none", "low", "medium", "high", "xhigh"],
        )
        agent.model = "gpt-5.5"
        agent.reasoning_config = {"enabled": True, "effort": "xhigh"}

        assert agent._github_models_reasoning_extra_body() == {"effort": "xhigh"}


    def test_qwen_portal_formats_messages_and_metadata(self, agent):
        agent.provider = "qwen-oauth"
        agent.base_url = "https://portal.qwen.ai/v1"
        agent._base_url_lower = agent.base_url.lower()
        agent.session_id = "sess-123"
        messages = [
            {"role": "system", "content": "You are helpful"},
            {"role": "assistant", "content": "Got it"},
            {"role": "user", "content": "hi"},
        ]
        kwargs = agent._build_api_kwargs(messages)
        assert kwargs["metadata"]["sessionId"] == "sess-123"
        assert kwargs["extra_body"]["vl_high_resolution_images"] is True
        assert isinstance(kwargs["messages"][0]["content"], list)
        assert kwargs["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
        assert kwargs["messages"][2]["content"][0]["text"] == "hi"

    def test_qwen_portal_normalizes_bare_string_content_parts(self, agent):
        agent.provider = "qwen-oauth"
        agent.base_url = "https://portal.qwen.ai/v1"
        agent._base_url_lower = agent.base_url.lower()
        messages = [
            {"role": "system", "content": [{"type": "text", "text": "system"}]},
            {"role": "user", "content": ["hello", {"type": "text", "text": "world"}]},
        ]
        kwargs = agent._build_api_kwargs(messages)
        user_content = kwargs["messages"][1]["content"]
        assert user_content[0] == {"type": "text", "text": "hello"}
        assert user_content[1] == {"type": "text", "text": "world"}


    def test_non_custom_provider_unaffected(self, agent):
        """OpenRouter provider with effort=none should NOT inject think=false."""
        agent.provider = "openrouter"
        agent.model = "qwen/qwen3.5-plus-02-15"
        agent.reasoning_config = {"effort": "none"}
        messages = [{"role": "user", "content": "hi"}]
        kwargs = agent._build_api_kwargs(messages)
        assert kwargs.get("extra_body", {}).get("think") is None
