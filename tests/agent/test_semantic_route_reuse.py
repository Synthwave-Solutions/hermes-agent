"""Classifier is skipped for scripted runs and never blocks a continuing chat."""

import time

from tests.agent.test_model_tiering import call, decision, setup


def _next_turn(messages, text):
    messages += [
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": text},
    ]


def test_scripted_platforms_skip_the_classifier():
    a = setup([decision()], platform="cron")
    assert call(a)["model"] == "synthwave-auto"
    assert a.client.calls == []


def test_reuse_takes_fast_refresh_and_skips_tool_reassessment():
    a = setup([decision(), decision("large", "high")])
    a._semantic_routing_config.update(reuse_previous=True, reuse_wait=1)
    m = [{"role": "user", "content": "one"}]
    assert call(a, messages=m)["model"] == "small-model"
    _next_turn(m, "two")
    assert call(a, messages=m)["model"] == "large-model"
    m.append({"role": "tool", "content": "x"})
    assert call(a, messages=m)["model"] == "large-model"
    assert len(a.client.calls) == 2


def test_slow_classifier_does_not_block_and_late_result_is_used():
    a = setup([decision("large", "high"), decision("large", "xhigh")])
    a._semantic_routing_config.update(reuse_previous=True, reuse_wait=0.1)
    m = [{"role": "user", "content": "one"}]
    assert call(a, messages=m)["extra_body"]["reasoning"]["effort"] == "high"
    inner = a.client.create

    def slow(**kw):
        time.sleep(0.5)
        return inner(**kw)

    a.client.chat.completions = type("C", (), {"create": staticmethod(slow)})()
    _next_turn(m, "two")
    start = time.monotonic()
    assert call(a, messages=m)["extra_body"]["reasoning"]["effort"] == "high"
    assert time.monotonic() - start < 0.4
    time.sleep(0.6)
    m.append({"role": "tool", "content": "x"})
    assert call(a, messages=m)["extra_body"]["reasoning"]["effort"] == "xhigh"


def test_failed_refresh_keeps_previous_route():
    a = setup([decision("large", "high"), TimeoutError()])
    a._semantic_routing_config.update(reuse_previous=True, reuse_wait=1)
    m = [{"role": "user", "content": "one"}]
    assert call(a, messages=m)["model"] == "large-model"
    _next_turn(m, "two")
    assert call(a, messages=m)["model"] == "large-model"


def test_lightest_route_is_not_carried_over_blind():
    a = setup([decision(), TimeoutError()])
    a._semantic_routing_config.update(reuse_previous=True, reuse_wait=1)
    m = [{"role": "user", "content": "one"}]
    assert call(a, messages=m)["model"] == "small-model"
    _next_turn(m, "a hard question")
    assert call(a, messages=m)["model"] == "synthwave-auto"
    m.append({"role": "tool", "content": "x"})
    assert call(a, messages=m)["model"] == "synthwave-auto"
