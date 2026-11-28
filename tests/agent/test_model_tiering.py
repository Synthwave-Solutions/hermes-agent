"""Semantic routing preserves profile policy, explicit reasoning and prompt bytes."""

from copy import deepcopy
import json
from types import SimpleNamespace as NS

import pytest
from agent.model_tiering import (
    apply_model_tiering,
    configure_model_tiering,
    tiering_request_user,
)


class Classifier:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = []
        self.chat = NS(completions=self)

    def with_options(self, **kw):
        assert kw["max_retries"] == 0 and kw["timeout"] <= 30
        return self

    def create(self, **kw):
        self.calls.append(kw)
        result = next(self.results)
        if isinstance(result, Exception):
            raise result
        return NS(
            choices=[NS(finish_reason="stop", message=NS(content=json.dumps(result)))]
        )


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("HERMES_MODEL_TIERING", raising=False)


def setup(results, **state):
    a = NS(client=Classifier(results), reasoning_config=None, **state)
    configure_model_tiering(
        a,
        {
            "model": {
                "routing": {
                    "classifier_model": "classifier",
                    "candidates": {
                        "small": {
                            "model": "small-model",
                            "efforts": ["low", "medium", "high"],
                        },
                        "large": {
                            "model": "large-model",
                            "efforts": ["low", "high", "xhigh"],
                        },
                    },
                }
            }
        },
    )
    return a


def decision(candidate="small", effort="low"):
    return {"candidate": candidate, "effort": effort, "reason": "task requirements"}


def call(agent, text="Wat betekent architectuur?", messages=None):
    kwargs = {"model": "synthwave-auto"}
    apply_model_tiering(agent, kwargs, messages or [{"role": "user", "content": text}])
    return kwargs


def test_semantic_choice_not_keywords_and_independent_effort():
    a = setup(
        [decision("small", "low"), decision("large", "low"), decision("small", "high")]
    )
    assert (
        call(a)["model"] == "small-model"
    )  # architecture keyword does not dictate size
    assert call(a, "second")["extra_body"]["reasoning"]["effort"] == "low"
    out = call(a, "third")
    assert (
        out["model"] == "small-model"
        and out["extra_body"]["reasoning"]["effort"] == "high"
    )
    assert len(a.client.calls) == 3


def test_classified_effort_replaces_transport_defaults():
    agent = setup([decision()], _wire_reasoning_config={"effort": "medium"})
    kwargs = {
        "model": "synthwave-auto",
        "reasoning_effort": "medium",
        "extra_body": {"reasoning_effort": "medium"},
    }
    apply_model_tiering(agent, kwargs, [{"role": "user", "content": "hello"}])
    assert kwargs["reasoning_effort"] == "low"
    assert kwargs["extra_body"]["reasoning_effort"] == "low"
    assert kwargs["extra_body"]["reasoning"]["effort"] == "low"
    assert agent._wire_reasoning_config["effort"] == "low"


@pytest.mark.parametrize(
    "reasoning", [{"enabled": False}, {"effort": "high"}, {"effort": "low"}]
)
def test_explicit_reasoning_preserved(reasoning):
    a = setup([decision()])
    a.reasoning_config = reasoning
    out = {"model": "synthwave-auto", "extra_body": {"reasoning": deepcopy(reasoning)}}
    apply_model_tiering(a, out, [{"role": "user", "content": "hello"}])
    assert out["model"] == "small-model" and out["extra_body"]["reasoning"] == reasoning


@pytest.mark.parametrize(
    "state",
    [
        {"_reasoning_effort_rejected": True},
        {"_reasoning_disable_rejected": True},
        {"_wire_reasoning_config": {"enabled": False}},
        {"request_overrides": {"reasoning_effort": "low"}},
        {"request_overrides": {"extra_body": {"thinking": {"type": "disabled"}}}},
    ],
)
def test_recovery_and_request_overrides_preserved(state):
    assert call(setup([decision()], **state)) == {"model": "small-model"}


@pytest.mark.parametrize("value", [False, "false", 0, "off"])
def test_profile_switch(value):
    a = setup([])
    configure_model_tiering(a, {"model": {"tiering": value}})
    assert call(a) == {"model": "synthwave-auto"} and not a.client.calls


def test_explicit_model_and_native_protocol_are_untouched():
    a = setup([])
    out = {"model": "explicit"}
    apply_model_tiering(a, out, [])
    assert out == {"model": "explicit"}
    a.api_mode = "codex_responses"
    assert call(a) == {"model": "synthwave-auto"} and not a.client.calls


@pytest.mark.parametrize(
    "result",
    [
        ValueError("unavailable"),
        {"candidate": "unknown", "effort": "low"},
        decision("small", "unsupported"),
    ],
)
def test_invalid_or_failed_classifier_leaves_original_and_is_cached(result):
    a = setup([result])
    assert call(a) == {"model": "synthwave-auto"}
    assert call(a) == {"model": "synthwave-auto"} and len(a.client.calls) == 1


def test_prompt_provenance_cache_and_new_steer():
    a = setup([decision(), decision("large", "high")], _persist_user_message_idx=1)
    messages = [
        {"role": "system", "content": "secret stable prefix"},
        {"role": "user", "content": "injected architecture context; hoi"},
    ]
    before = deepcopy(messages)
    with tiering_request_user(a, messages, "hoi"):
        assert call(a, messages=messages)["model"] == "small-model"
        assert call(a, messages=messages)["model"] == "small-model"
    payload = json.loads(a.client.calls[0]["messages"][1]["content"])
    assert payload["task"] == "hoi" and "secret stable prefix" not in json.dumps(
        payload
    )
    assert messages == before and a.reasoning_config is None
    messages.append({"role": "user", "content": "new task"})
    with tiering_request_user(a, messages, "old task"):
        assert call(a, messages=messages)["model"] == "large-model"
    assert json.loads(a.client.calls[1]["messages"][1]["content"])["task"] == "new task"


def test_profile_catalog_snapshot_and_no_cross_agent_cache():
    one = setup([decision()])
    two = setup([decision("large", "high")])
    assert call(one)["model"] == "small-model"
    assert call(two)["model"] == "large-model"
    assert call(one)["model"] == "small-model"
    assert len(one.client.calls) == len(two.client.calls) == 1


def test_tool_reassessment_is_bounded():
    a = setup([decision(), decision("large", "high")])
    m = [{"role": "user", "content": "task"}]
    assert call(a, messages=m)["model"] == "small-model"
    for i in range(8):
        m.append({"role": "tool", "content": "step result"})
        assert call(a, messages=m)["model"] == "large-model"
    assert len(a.client.calls) == 2


def test_classifier_failure_uses_independent_prompt_route_once():
    a = setup([TimeoutError("upstream stalled"), decision("large", "xhigh")])
    a._semantic_routing_config["fallback_classifier_model"] = "second-classifier"
    result = call(a)
    assert result["model"] == "large-model"
    assert result["extra_body"]["reasoning"]["effort"] == "xhigh"
    assert [r["model"] for r in a.client.calls] == ["classifier", "second-classifier"]
    assert a._semantic_route_decision["classifier_model"] == "second-classifier"
    assert a._semantic_routing_status["attempts"] == 2
    assert call(a) == result and len(a.client.calls) == 2


def test_both_classifier_failures_are_visible_and_bounded():
    a = setup([TimeoutError(), ValueError("invalid JSON")])
    a._semantic_routing_config["fallback_classifier_model"] = "second-classifier"
    assert call(a) == {"model": "synthwave-auto"}
    assert a._semantic_routing_status == {
        "status": "fallback",
        "attempts": 2,
        "errors": ["TimeoutError", "ValueError"],
    }
    assert call(a) == {"model": "synthwave-auto"} and len(a.client.calls) == 2


def test_truncated_classifier_output_uses_fallback_without_mutating_prompt():
    a = setup([decision("large", "high")])
    a._semantic_routing_config["fallback_classifier_model"] = "second-classifier"
    normal_create = a.client.create
    requests = []

    def truncated_once(**kwargs):
        requests.append(deepcopy(kwargs))
        if len(requests) == 1:
            return NS(
                choices=[
                    NS(finish_reason="length", message=NS(content='{"candidate":'))
                ]
            )
        return normal_create(**kwargs)

    a.client.chat.completions.create = truncated_once
    messages = [
        {"role": "system", "content": "stable prefix"},
        {"role": "user", "content": "Prove this concurrency invariant."},
    ]
    before = deepcopy(messages)
    assert call(a, messages=messages)["model"] == "large-model"
    assert messages == before and len(requests) == 2
    assert all(r["max_tokens"] >= 1024 for r in requests)
    assert all(r["temperature"] > 0.1 for r in requests)
    assert all(r["extra_headers"]["x-omniroute-no-memory"] == "true" for r in requests)
