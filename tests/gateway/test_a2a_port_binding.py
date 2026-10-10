"""The A2A listener binds a host port, so only the default mux profile starts it
(PLAN 4.3 port binding, 4.5 E2: secondary profiles do not bind 9900)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import gateway.run as gateway_run
from gateway.config import (
    PORT_BINDING_PLATFORM_VALUES,
    GatewayConfig,
    Platform,
    PlatformConfig,
    platform_binds_port,
)
from gateway.run import GatewayRunner


class _Adapter:
    def __init__(self, platform):
        self.platform = platform
        self.config = None

    async def disconnect(self):
        return None

    def set_message_handler(self, handler):
        self.message_handler = handler

    def set_fatal_error_handler(self, handler):
        self.fatal_error_handler = handler

    def set_session_store(self, store):
        self.session_store = store

    def set_busy_session_handler(self, handler):
        self.busy_session_handler = handler

    def set_topic_recovery_fn(self, handler):
        self.topic_recovery_fn = handler

    def set_authorization_check(self, handler):
        self.authorization_check = handler

    def set_platform_event_handler(self, handler):
        self.platform_event_handler = handler


def _runner():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner._running = True
    runner._profile_adapters = {}
    runner._profile_failed_platforms = {}
    runner._background_tasks = set()
    runner.session_store = object()
    runner._handle_active_session_busy_message = object()
    runner._recover_telegram_topic_thread_id = object()
    runner._busy_text_mode = "queue"
    runner._make_adapter_auth_check = lambda platform, profile_name=None: object()
    runner._adapter_disconnect_timeout_secs = lambda: 0
    runner._sync_voice_mode_state_to_adapter = lambda adapter: None
    runner._redeliver_failed_obligations_for_platform = AsyncMock(return_value=0)
    return runner


def test_a2a_is_a_port_binding_platform():
    assert "a2a" in PORT_BINDING_PLATFORM_VALUES
    assert platform_binds_port("a2a") is True
    assert platform_binds_port("a2a", {"port": 9900}) is True


def test_secondary_profile_does_not_start_the_a2a_listener(monkeypatch, tmp_path):
    runner = _runner()
    profile_cfg = GatewayConfig(
        multiplex_profiles=True,
        platforms={
            Platform("a2a"): PlatformConfig(enabled=True, extra={"port": 9900}),
            Platform.DISCORD: PlatformConfig(enabled=True, token="profile-discord-token"),
        },
    )
    created = []

    def fake_create(platform, config):
        created.append(platform.value)
        return _Adapter(platform)

    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: profile_cfg)
    monkeypatch.setattr(runner, "_create_adapter", fake_create)
    monkeypatch.setattr(runner, "_connect_initial_adapter_with_timeout",
                        AsyncMock(return_value=True))
    monkeypatch.setattr(gateway_run, "_synthpulse_root_shared_credential", lambda adapter, home: False)

    connected = asyncio.run(runner._start_one_profile_adapters("sales", tmp_path, {}))

    assert "a2a" not in created
    assert created == ["discord"]
    assert connected == 1
    assert Platform("a2a") not in runner._profile_adapters.get("sales", {})
