"""Gateway configuration stays inert until explicitly enabled."""

import os
from unittest.mock import patch

import yaml

from hermes_cli.config import (
    DEFAULT_CONFIG,
    _KNOWN_ROOT_KEYS,
    load_config,
    validate_config_structure,
)

EXPECTED_MODEL_GATEWAY = {
    "base_url": "",
    "enforce": False,
    "org_id": "",
    "installation_id": "",
    "module": "engine",
    "user_field": "pseudonym",
    "session_header": "X-SP-Session",
    "realtime": False,
    "tool_exceptions": [],
}

def test_block_has_the_contract_defaults():
    block = DEFAULT_CONFIG["model_gateway"]
    assert block == EXPECTED_MODEL_GATEWAY
    assert list(block) == list(EXPECTED_MODEL_GATEWAY)


def test_block_is_inert_by_default():
    block = DEFAULT_CONFIG["model_gateway"]
    assert block["enforce"] is False
    assert block["realtime"] is False
    assert block["base_url"] == ""
    assert block["tool_exceptions"] == []


def test_rendered_block_is_a_known_root_key():
    assert "model_gateway" in _KNOWN_ROOT_KEYS
    issues = validate_config_structure(
        {"model_gateway": {"base_url": "http://127.0.0.1:20128/v1", "enforce": True}}
    )
    assert not [i for i in issues if "model_gateway" in i.message]


def test_load_config_fills_the_block_without_a_file(tmp_path):
    with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
        config = load_config()
    assert config["model_gateway"] == EXPECTED_MODEL_GATEWAY


def test_partial_rendered_block_keeps_the_other_defaults(tmp_path):
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "model_gateway": {
                    "base_url": "http://gateway.internal/v1",
                    "enforce": True,
                    "org_id": "acme",
                }
            }
        ),
        encoding="utf-8",
    )
    with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
        config = load_config()
    block = config["model_gateway"]
    assert block["base_url"] == "http://gateway.internal/v1"
    assert block["enforce"] is True
    assert block["org_id"] == "acme"
    assert block["module"] == "engine"
    assert block["session_header"] == "X-SP-Session"
    assert block["tool_exceptions"] == []
    # The shared defaults dict is never mutated by a load.
    assert DEFAULT_CONFIG["model_gateway"] == EXPECTED_MODEL_GATEWAY
