"""Only server-supplied cache hints override legacy manifest cache behavior."""
import asyncio

import pytest
from mcp.types import ListToolsResult
from tools.mcp_tool import _paginate_full_list
from tools import mcp_schema_cache as cache


@pytest.mark.parametrize('wire,expected', [
    ({'tools': []}, {}),
    ({'tools': [], 'ttlMs': 0, 'cacheScope': 'private'}, {'ttl_ms': 0, 'cache_scope': 'private'}),
    ({'tools': [], 'ttlMs': 60000}, {'ttl_ms': 60000}),
])
def test_sdk_defaults_do_not_become_wire_directives(wire, expected, monkeypatch, tmp_path):
    result=ListToolsResult.model_validate(wire)
    async def list_tools():
        return result
    hints={}
    assert asyncio.run(_paginate_full_list(list_tools, 'tools', 'synthetic', hints)) == []
    assert hints==expected
    monkeypatch.setattr(cache, '_cache_path', lambda: tmp_path/'manifest.json')
    cache.write_cache_entry('synthetic','fp',tools=[],**hints)
    entry=cache.get_cached_entry('synthetic','fp')
    assert (entry is None)==(wire.get('ttlMs')==0)
