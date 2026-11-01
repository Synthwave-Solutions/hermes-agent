"""Langfuse exports: behavior contracts."""
from __future__ import annotations

import tests.plugins.test_langfuse_plugin as _support

pytest_plugins = ["tests.plugins.test_langfuse_plugin"]


class TestFinishTraceUsesUpdateTrace:
    """Regression: SDK v3 has update_trace, not set_trace_io.

    Calling the non-existent set_trace_io raised AttributeError inside
    _finish_trace's try block and skipped root_span.end(). Generations/tools
    still exported, so the Langfuse list showed Observation Levels + Latency
    but blank Input/Output columns (no CHAIN root).
    """

    def test_finish_ends_root_and_calls_update_trace(self, monkeypatch):
        _support.sys.modules.pop("plugins.observability.langfuse", None)
        mod = _support.importlib.import_module("plugins.observability.langfuse")

        roots: list = []

        class _Span:
            def __init__(self):
                self.ended = False
                self.updates = []
                self.trace_updates = []

            def update(self, **kw):
                self.updates.append(kw)

            def end(self, **kw):
                self.ended = True

            def update_trace(self, **kw):
                self.trace_updates.append(kw)

            def start_observation(self, **kw):
                return _Span()

            # Deliberately NO set_trace_io — mirrors real LangfuseChain.

        class _RootCM:
            def __init__(self):
                self.span = _Span()
                roots.append(self.span)

            def __enter__(self):
                return self.span

            def __exit__(self, *exc):
                return False

        class _Client:
            def create_trace_id(self, seed=None):
                return f"trace::{seed}"

            def start_as_current_observation(self, **kw):
                return _RootCM()

            def flush(self):
                pass

        monkeypatch.setattr(mod, "_get_langfuse", lambda: _Client())
        monkeypatch.setattr(mod, "_end_observation", lambda *a, **k: None)
        mod._TRACE_STATE.clear()

        mod.on_pre_llm_request(
            task_id="t1",
            session_id="s1",
            model="m",
            provider="p",
            api_mode="chat",
            api_call_count=1,
            request_messages=[{"role": "user", "content": "hi"}],
            turn_id="turn-1",
        )
        mod.on_post_llm_call(
            task_id="t1",
            session_id="s1",
            model="m",
            provider="p",
            api_mode="chat",
            api_call_count=1,
            assistant_content_chars=12,
            assistant_tool_call_count=0,
            assistant_response="hello world!",
            turn_id="turn-1",
        )

        assert len(roots) == 1
        root = roots[0]
        assert root.ended is True
        assert any("output" in u for u in root.trace_updates)
        assert any("output" in u for u in root.updates)
        assert mod._TRACE_STATE == {}

    def test_finish_still_ends_when_update_trace_raises(self, monkeypatch):
        _support.sys.modules.pop("plugins.observability.langfuse", None)
        mod = _support.importlib.import_module("plugins.observability.langfuse")

        roots: list = []

        class _Span:
            def __init__(self):
                self.ended = False

            def update(self, **kw):
                pass

            def end(self, **kw):
                self.ended = True

            def update_trace(self, **kw):
                raise RuntimeError("simulated update_trace failure")

            def start_observation(self, **kw):
                return _Span()

        class _RootCM:
            def __init__(self):
                self.span = _Span()
                roots.append(self.span)

            def __enter__(self):
                return self.span

            def __exit__(self, *exc):
                return False

        class _Client:
            def create_trace_id(self, seed=None):
                return f"trace::{seed}"

            def start_as_current_observation(self, **kw):
                return _RootCM()

            def flush(self):
                pass

        monkeypatch.setattr(mod, "_get_langfuse", lambda: _Client())
        monkeypatch.setattr(mod, "_end_observation", lambda *a, **k: None)
        mod._TRACE_STATE.clear()

        mod.on_pre_llm_request(
            task_id="t1",
            session_id="s1",
            model="m",
            provider="p",
            api_mode="chat",
            api_call_count=1,
            request_messages=[{"role": "user", "content": "hi"}],
            turn_id="turn-1",
        )
        mod.on_post_llm_call(
            task_id="t1",
            session_id="s1",
            model="m",
            provider="p",
            api_mode="chat",
            api_call_count=1,
            assistant_content_chars=5,
            assistant_tool_call_count=0,
            assistant_response="done",
            turn_id="turn-1",
        )

        assert roots[0].ended is True
        assert mod._TRACE_STATE == {}


class TestCanonicalCostExport:
    """Both supported response paths must export the same complete cost."""

    @staticmethod
    def _response(input_tokens, output_tokens, cache_read=0, cache_write=0):
        cache_details = _support.SimpleNamespace(
            cached_tokens=cache_read,
            cache_write_tokens=cache_write,
        )
        usage = _support.SimpleNamespace(
            # Anthropic response shape.
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_write,
            # OpenAI chat response shape used by the included-route case.
            prompt_tokens=input_tokens + cache_read + cache_write,
            completion_tokens=output_tokens,
            prompt_tokens_details=cache_details,
        )
        return _support.SimpleNamespace(usage=usage)

    @staticmethod
    def _summary(input_tokens, output_tokens, cache_read=0, cache_write=0, request_count=1):
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "reasoning_tokens": 0,
            "request_count": request_count,
        }

    @staticmethod
    def _capture_summary_path(mod, monkeypatch, usage, *, provider, model, api_mode):
        monkeypatch.setattr(mod, "_get_langfuse", lambda: object())
        observation = object()
        state = mod.TraceState(trace_id="trace-cost", root_ctx=None, root_span=None)
        state.generations[mod._request_key(1)] = observation
        task_key = mod._trace_key("task-cost", "session-cost")
        monkeypatch.setitem(mod._TRACE_STATE, task_key, state)
        captured = {}

        def fake_end_observation(
            obs,
            *,
            output=None,
            metadata=None,
            usage_details=None,
            cost_details=None,
        ):
            captured["usage_details"] = usage_details
            captured["cost_details"] = cost_details

        monkeypatch.setattr(mod, "_end_observation", fake_end_observation)
        mod.on_post_llm_call(
            task_id="task-cost",
            session_id="session-cost",
            api_call_count=1,
            model=model,
            provider=provider,
            api_mode=api_mode,
            response={"model": model},
            usage=usage,
        )
        return captured["usage_details"], captured["cost_details"]

    def _run_both_paths(
        self,
        mod,
        monkeypatch,
        usage,
        *,
        provider="anthropic",
        model="priced-model",
        api_mode="anthropic_messages",
    ):
        response_result = mod._usage_and_cost(
            self._response(
                usage["input_tokens"],
                usage["output_tokens"],
                usage.get("cache_read_tokens", 0),
                usage.get("cache_write_tokens", 0),
            ),
            provider=provider,
            api_mode=api_mode,
            model=model,
            base_url="",
        )
        summary_result = self._capture_summary_path(
            mod,
            monkeypatch,
            usage,
            provider=provider,
            model=model,
            api_mode=api_mode,
        )
        assert response_result[0] == summary_result[0]
        return response_result[1], summary_result[1]

    @_support.pytest.mark.parametrize(
        ("cache_read", "cache_write", "expected_total"),
        [
            (0, 0, 0.00002),
            (2, 3, 0.0000255),
        ],
        ids=("no-cache", "cached"),
    )
    def test_known_costs_include_canonical_total_on_both_paths(
        self,
        monkeypatch,
        cache_read,
        cache_write,
        expected_total,
    ):
        _support.sys.modules.pop("plugins.observability.langfuse", None)
        mod = _support.importlib.import_module("plugins.observability.langfuse")
        import agent.usage_pricing as pricing

        entry = pricing.PricingEntry(
            input_cost_per_million=_support.Decimal(1),
            output_cost_per_million=_support.Decimal(2),
            cache_read_cost_per_million=_support.Decimal("0.5"),
            cache_write_cost_per_million=_support.Decimal("1.5"),
            source="custom_contract",
        )
        monkeypatch.setattr(pricing, "get_pricing_entry", lambda *_, **__: entry)
        usage = self._summary(10, 5, cache_read, cache_write)

        response_cost, summary_cost = self._run_both_paths(mod, monkeypatch, usage)
        expected = {
            "total": expected_total,
            "input": 0.00001,
            "output": 0.00001,
        }
        if cache_read:
            expected["cache_read_input_tokens"] = 0.000001
        if cache_write:
            expected["cache_creation_input_tokens"] = 0.0000045
        assert response_cost == _support.pytest.approx(expected)
        assert summary_cost == _support.pytest.approx(expected)

    def test_total_uses_request_cost_instead_of_component_sum(self, monkeypatch):
        _support.sys.modules.pop("plugins.observability.langfuse", None)
        mod = _support.importlib.import_module("plugins.observability.langfuse")
        import agent.usage_pricing as pricing

        entry = pricing.PricingEntry(
            input_cost_per_million=_support.Decimal(1),
            output_cost_per_million=_support.Decimal(2),
            cache_read_cost_per_million=_support.Decimal("0.5"),
            cache_write_cost_per_million=_support.Decimal("1.5"),
            request_cost=_support.Decimal("0.01"),
            source="provider_models_api",
        )
        monkeypatch.setattr(pricing, "get_pricing_entry", lambda *_, **__: entry)
        usage = self._summary(10, 5, cache_read=2, cache_write=3)

        response_cost, summary_cost = self._run_both_paths(mod, monkeypatch, usage)
        for cost_details in (response_cost, summary_cost):
            component_sum = sum(
                value for key, value in cost_details.items() if key != "total"
            )
            assert cost_details["total"] == _support.pytest.approx(0.0100255)
            assert component_sum == _support.pytest.approx(0.0000255)

    def test_request_only_price_still_exports_total(self, monkeypatch):
        _support.sys.modules.pop("plugins.observability.langfuse", None)
        mod = _support.importlib.import_module("plugins.observability.langfuse")
        import agent.usage_pricing as pricing

        entry = pricing.PricingEntry(
            request_cost=_support.Decimal("0.01"),
            source="provider_models_api",
        )
        monkeypatch.setattr(pricing, "get_pricing_entry", lambda *_, **__: entry)
        usage = self._summary(0, 0)

        response_cost, summary_cost = self._run_both_paths(mod, monkeypatch, usage)
        assert response_cost == {"total": 0.01}
        assert summary_cost == {"total": 0.01}

    def test_partial_cache_pricing_exports_no_costs(self, monkeypatch):
        _support.sys.modules.pop("plugins.observability.langfuse", None)
        mod = _support.importlib.import_module("plugins.observability.langfuse")
        import agent.usage_pricing as pricing

        entry = pricing.PricingEntry(
            input_cost_per_million=_support.Decimal(1),
            output_cost_per_million=_support.Decimal(2),
            cache_read_cost_per_million=None,
            source="provider_models_api",
        )
        monkeypatch.setattr(pricing, "get_pricing_entry", lambda *_, **__: entry)
        usage = self._summary(10, 5, cache_read=2)

        response_cost, summary_cost = self._run_both_paths(mod, monkeypatch, usage)
        assert response_cost == {}
        assert summary_cost == {}

    def test_unknown_pricing_exports_no_costs(self, monkeypatch):
        _support.sys.modules.pop("plugins.observability.langfuse", None)
        mod = _support.importlib.import_module("plugins.observability.langfuse")
        import agent.usage_pricing as pricing

        monkeypatch.setattr(pricing, "get_pricing_entry", lambda *_, **__: None)
        usage = self._summary(10, 5)

        response_cost, summary_cost = self._run_both_paths(mod, monkeypatch, usage)
        assert response_cost == {}
        assert summary_cost == {}

    def test_included_route_does_not_pin_total(self, monkeypatch):
        _support.sys.modules.pop("plugins.observability.langfuse", None)
        mod = _support.importlib.import_module("plugins.observability.langfuse")
        usage = self._summary(10, 5, cache_read=2)

        response_cost, summary_cost = self._run_both_paths(
            mod,
            monkeypatch,
            usage,
            provider="openai-codex",
            model="gpt-5.3-codex",
            api_mode="chat_completions",
        )
        assert response_cost == summary_cost
        assert "total" not in response_cost
        # Subscription-included routes must send NO cost keys at all —
        # explicit zeros are treated as authoritative by Langfuse and block
        # its own model-based estimation (#43129).
        assert response_cost == {}
