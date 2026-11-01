"""Model metadata pricing: behavior contracts."""
from __future__ import annotations

import tests.agent.test_model_metadata as _support

pytest_plugins = ["tests.agent.test_model_metadata"]


def test_endpoint_pricing_already_per_million_is_not_inflated():
    """#112018 / #34256 / #79174: a /models catalog quoting USD per 1M tokens (with or without an explicit
    ``unit``) must reach usage_pricing as per-token rates, so the cost estimate is $0.60/M — not $600,000/M."""
    from agent import model_metadata as mm
    from agent import usage_pricing as up

    catalog = {
        "minimax-m3": {"id": "minimax-m3", "pricing": {"currency": "USD", "prompt": 0.6, "completion": 1.2, "cache_read": 0.12}},
        "glm-x": {"id": "glm-x", "pricing": {"currency": "CNY", "unit": "per_1m_tokens", "prompt": 1, "completion": 2}},
        "crof-a": {"id": "crof-a", "cost": {"input": 0.04, "output": 0.15}},
    }
    meta = {mid: _support._model_metadata_payloads._endpoint_model_entry(model, mid, None) for mid, model in catalog.items()}
    dollars_per_million = {
        mid: up._pricing_entry_from_metadata(meta, mid, source_url="x", pricing_version="openai-compatible-models-api")
        for mid in catalog
    }
    assert float(dollars_per_million["minimax-m3"].input_cost_per_million) == _support.pytest.approx(0.6)
    assert float(dollars_per_million["minimax-m3"].cache_read_cost_per_million) == _support.pytest.approx(0.12)
    assert float(dollars_per_million["glm-x"].output_cost_per_million) == _support.pytest.approx(2.0)
    assert float(dollars_per_million["crof-a"].input_cost_per_million) == _support.pytest.approx(0.04)


def test_endpoint_pricing_per_token_quotes_pass_through_unchanged():
    """Control: per-token quotes (the OpenRouter convention) and per-request fees are left alone."""
    from agent import model_metadata as mm
    from agent import usage_pricing as up

    model = {"id": "m", "pricing": {"prompt": "0.0000006", "completion": "0.0000012", "request": "0.005"}}
    meta = {"m": _support._model_metadata_payloads._endpoint_model_entry(model, "m", None)}
    entry = up._pricing_entry_from_metadata(meta, "m", source_url="x", pricing_version="openai-compatible-models-api")
    assert float(entry.input_cost_per_million) == _support.pytest.approx(0.6)
    assert float(entry.output_cost_per_million) == _support.pytest.approx(1.2)
    assert float(entry.request_cost) == _support.pytest.approx(0.005)
