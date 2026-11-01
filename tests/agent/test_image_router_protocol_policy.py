"""Router image lookup retains catalog/override semantics without native discovery."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.auxiliary_client import reset_runtime_main, set_runtime_main
from agent.image_routing import _lookup_supports_vision


@pytest.fixture
def probes():
    with patch('agent.models_dev.get_model_capabilities', return_value=None) as catalog, \
         patch('agent.model_metadata.detect_local_server_type', return_value='ollama') as detect, \
         patch('agent.model_metadata.query_ollama_supports_vision', return_value=True) as vision:
        yield catalog, detect, vision


def test_explicit_router_unknown_keeps_unknown_without_native_probe(probes):
    _, detect, vision = probes
    assert _lookup_supports_vision('custom:omniroute', 'unknown',
        {'model': {'base_url': 'http://127.0.0.1:9999/v1'}}) is None
    detect.assert_not_called()
    vision.assert_not_called()


def test_runtime_requested_identity_is_exact_and_restored(probes):
    _, detect, _ = probes
    cfg = {'model': {'base_url': 'http://127.0.0.1:9999/v1'}}
    outer = set_runtime_main('custom', 'same', requested_provider='custom:omniroute')
    try:
        assert _lookup_supports_vision('custom', 'same', cfg) is None
        # Another model must not borrow the router identity.
        assert _lookup_supports_vision('custom', 'other', cfg) is True
        inner = set_runtime_main('custom', 'same', requested_provider='custom:local')
        try:
            assert _lookup_supports_vision('custom', 'same', cfg) is True
        finally:
            reset_runtime_main(inner)
        assert _lookup_supports_vision('custom', 'same', cfg) is None
        assert detect.call_count == 2
    finally:
        reset_runtime_main(outer)


@pytest.mark.parametrize('value', [False, True])
def test_router_capability_signals_remain_authoritative(probes, value):
    catalog, detect, _ = probes
    assert _lookup_supports_vision('custom:omniroute', 'model',
        {'model': {'supports_vision': value}}) is value
    catalog.assert_not_called()
    catalog.return_value = SimpleNamespace(supports_vision=value)
    assert _lookup_supports_vision('custom:omniroute', 'model', {}) is value
    detect.assert_not_called()


def test_native_ollama_fallback_keeps_actual_vision_probe(probes):
    _, _, vision = probes
    assert _lookup_supports_vision('ollama', 'local-model', {},
        requested_provider='custom:omniroute') is True
    vision.assert_called_once()


def test_real_local_ollama_between_router_profile_scopes(tmp_path, monkeypatch):
    """Real HTTP/import chain: router A -> local B -> router A, same endpoint."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, body, status=200):
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            calls.append(self.path)
            self.reply({'models': []} if self.path == '/api/tags' else {},
                       200 if self.path == '/api/tags' else 404)

        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length', '0')))
            calls.append(self.path)
            self.reply({'capabilities': ['vision']})

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}/v1'
    try:
        with patch('agent.models_dev.get_model_capabilities', return_value=None):
            for name, requested, expected in [('a', 'custom:omniroute', None),
                                              ('b', 'custom:local', True),
                                              ('a', 'custom:omniroute', None)]:
                home = tmp_path / name
                home.mkdir(exist_ok=True)
                monkeypatch.setenv('HERMES_HOME', str(home))
                token = set_runtime_main('custom', 'fixture', requested_provider=requested,
                                         base_url=base)
                before = len(calls)
                try:
                    assert _lookup_supports_vision('custom', 'fixture', {}) is expected
                    if name == 'a':
                        assert len(calls) == before
                finally:
                    reset_runtime_main(token)
        assert '/api/tags' in calls and '/api/show' in calls
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
