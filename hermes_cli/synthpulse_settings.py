"""Profile settings with a compatibility bridge for existing SynthPulse deployments.

New installations configure these options in config.yaml. Legacy environment
values are consulted only when that setting is absent. They remain process-wide
for compatibility (notably the embedding WebUI host's shared spool and inbox);
an explicit profile configuration always wins. No environment values are cached.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any


LEGACY_SETTINGS = {
    "model.tiering": "HERMES_MODEL_TIERING",
    "opencode.max_slots": "HERMES_OPENCODE_MAX_SLOTS",
    "opencode.binary": "HERMES_OPENCODE_BIN",
    "webui.state_dir": "HERMES_WEBUI_STATE_DIR",
    "webui.attachment_dir": "HERMES_WEBUI_ATTACHMENT_DIR",
    "api_server.disable_caller_governance": "HERMES_DISABLE_CALLER_GOVERNANCE",
}


def setting(path: str, default: Any = None, *, config: Mapping | None = None) -> Any:
    """Resolve one known setting; explicit false/zero values override legacy env."""
    if config is None:
        from hermes_cli.config import load_config_readonly
        config = load_config_readonly()
    value: Any = config
    for part in path.split("."):
        value = value.get(part) if isinstance(value, Mapping) else None
    if value is not None:
        return value
    return os.environ.get(LEGACY_SETTINGS[path], default)


def enabled(path: str, default: bool, *, config: Mapping | None = None) -> bool:
    from utils import is_truthy_value
    return is_truthy_value(setting(path, default, config=config))
