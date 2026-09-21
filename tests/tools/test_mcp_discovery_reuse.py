"""Credential cache invalidation and live-registry discovery reuse contracts."""
import copy
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import tools.mcp_schema_cache as cache
import tools.mcp_tool as mcp


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    for name in ('_servers', '_lazy_server_configs', '_lazy_server_tool_names', '_server_connect_errors', '_server_connect_retry_after'):
        monkeypatch.setattr(mcp, name, {})
    for name in ('_server_connecting', '_parallel_safe_servers'):
        monkeypatch.setattr(mcp, name, set())
    monkeypatch.setattr(mcp, '_MCP_AVAILABLE', True)


@pytest.mark.parametrize('field,first,second', [
    ('headers', {'Authorization':'synthetic-account-a'}, {'Authorization':'synthetic-account-b'}),
    ('env', {'TENANT':'synthetic-account-a'}, {'TENANT':'synthetic-account-b'}),
    ('auth', {'type':'oauth','scope':'read'}, {'type':'oauth','scope':'write'}),
])
def test_credential_scope_change_invalidates_real_disk_entry(monkeypatch, tmp_path, field, first, second):
    monkeypatch.setattr(cache, '_cache_path', lambda: tmp_path/'schema.json')
    base={'command':'synthetic','args':[],field:first}
    before=cache.config_fingerprint(base)
    cache.write_cache_entry('same-server', before, tools=[{'name':'scoped_tool'}])
    assert cache.get_cached_entry('same-server', before) is not None
    after=cache.config_fingerprint({**base,field:second})
    assert cache.get_cached_entry('same-server', after) is None
    assert 'synthetic-account' not in (tmp_path/'schema.json').read_text()
    assert cache.get_cached_entry('same-server', before) is not None


def test_credential_mapping_order_is_stable_but_removal_invalidates():
    a={'headers':{'X-Tenant':'A','Authorization':'synthetic-key'},'env':{'A':'1','B':'2'},'auth':{'type':'oauth','scope':'read'}}
    b={'auth':{'scope':'read','type':'oauth'},'env':{'B':'2','A':'1'},'headers':{'Authorization':'synthetic-key','X-Tenant':'A'}}
    assert cache.config_fingerprint(a)==cache.config_fingerprint(b)
    assert cache.config_fingerprint(a)!=cache.config_fingerprint({**a,'headers':{}})


def connected(config):
    return SimpleNamespace(_config=copy.deepcopy(config),session=object(),_error=None,_registered_tool_names=['mcp__synthetic__ping'])


def test_connected_same_config_skips_held_crossprocess_lock(monkeypatch, tmp_path):
    cfg={'command':'synthetic','enabled':True,'supports_parallel_tool_calls':True}
    mcp._servers['synthetic']=connected(cfg)
    monkeypatch.setattr(mcp,'_load_mcp_config',lambda:{'synthetic':cfg})
    monkeypatch.setattr(mcp,'_MCP_DISCOVERY_LOCK_PATH',str(tmp_path/'.mcp-discovery.lock'))
    cookie=mcp._try_acquire_mcp_discovery_lock()
    assert cookie not in (None,mcp._LOCK_UNAVAILABLE)
    waits=[]
    monkeypatch.setattr(mcp,'_MCP_DISCOVERY_LOCK_MAX_RETRIES',2)
    monkeypatch.setattr(mcp.time,'sleep',lambda seconds:waits.append(seconds))
    original=mcp._try_acquire_mcp_discovery_lock
    attempts=[]
    def acquire():
        attempts.append(True);return original()
    monkeypatch.setattr(mcp,'_try_acquire_mcp_discovery_lock',acquire)
    try:
        assert mcp.discover_mcp_tools()==['mcp__synthetic__ping']
    finally:
        cookie.release()
    assert attempts==[] and waits==[]
    assert 'synthetic' in mcp._parallel_safe_servers


@pytest.mark.parametrize('difference', ['missing','config','parked','error','connecting'])
def test_unconfirmed_registry_keeps_guarded_registration(monkeypatch,difference):
    cfg={'command':'synthetic','enabled':True}
    server=connected(cfg)
    mcp._servers['synthetic']=server
    if difference=='missing':mcp._servers.clear()
    if difference=='config':server._config['headers']={'Authorization':'old-synthetic-key'}
    if difference=='parked':server.session=None
    if difference=='error':server._error=RuntimeError('synthetic-error')
    if difference=='connecting':mcp._server_connecting.add('synthetic')
    monkeypatch.setattr(mcp,'_load_mcp_config',lambda:{'synthetic':cfg})
    cookie=MagicMock();acquire=MagicMock(return_value=cookie)
    monkeypatch.setattr(mcp,'_try_acquire_mcp_discovery_lock',acquire)
    register=MagicMock(return_value=['existing-result']);monkeypatch.setattr(mcp,'register_mcp_servers',register)
    assert mcp.discover_mcp_tools()==['existing-result']
    acquire.assert_called_once();register.assert_called_once_with({'synthetic':cfg});cookie.release.assert_called_once()


def test_discovery_reuse_does_not_enable_parallel_calls_without_config(monkeypatch):
    cfg={'command':'synthetic','enabled':True,'supports_parallel_tool_calls':False}
    mcp._servers['synthetic']=connected(cfg);mcp._parallel_safe_servers.add('synthetic')
    monkeypatch.setattr(mcp,'_load_mcp_config',lambda:{'synthetic':cfg})
    assert mcp.discover_mcp_tools()==['mcp__synthetic__ping']
    assert 'synthetic' not in mcp._parallel_safe_servers


def test_resolved_config_security_filter_still_applies_to_live_registry(monkeypatch):
    # A stale/preexisting entry must not skip the final resolved-config filter.
    # No shell command runs: only the actual validator and registry read execute.
    cfg={'command':'sh','args':['-c','curl https://example.invalid'], 'enabled':True}
    mcp._servers['synthetic']=connected(cfg)
    monkeypatch.setattr(mcp,'_load_mcp_config',lambda:{'synthetic':cfg})
    monkeypatch.setattr(mcp,'_try_acquire_mcp_discovery_lock',lambda:mcp._LOCK_UNAVAILABLE)
    assert mcp.discover_mcp_tools()==[]
