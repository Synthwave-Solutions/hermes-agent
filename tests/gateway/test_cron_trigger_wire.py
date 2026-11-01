"""Manual cron metadata must cross the public dispatcher, not only the internal handler."""
from types import SimpleNamespace

import pytest

from cron import jobs
from cron.run_context import normalize_context, script_environment
from gateway import run
from gateway.config import GatewayConfig
from gateway.session import SessionStore
from gateway.session_authority import SessionAuthority
from gateway.session_controls import AuthorityConnection
from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch, get_session_admission


@pytest.fixture
def owner(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(run, '_load_gateway_config', lambda: {'model': {}, 'platform_toolsets': {'cli': []}})
    with jobs.use_cron_store(tmp_path / 'cron'), SessionDB(tmp_path / 'state.db') as db:
        job = jobs.create_job(prompt='prepare', schedule='every 1h', deliver='local', model='fixture')
        runner = SimpleNamespace(_draining=False, adapters={},
                                 session_store=SessionStore(tmp_path / 'sessions', GatewayConfig()))
        runner._adapter_for_source = lambda source: runner.adapters.get(source.platform)
        authority = SessionAuthority(runner, profile_id=str(tmp_path), instance_id='test', db=db,
                                     epoch=begin_runtime_epoch(db, instance_id='test'))
        runner.session_authority = authority
        monkeypatch.setattr(authority, '_schedule', lambda ref: None)
        connection = AuthorityConnection(authority, object(), {'user_id': 'owner'})
        yield connection, job


@pytest.mark.asyncio
@pytest.mark.parametrize('trigger', [None, 'manual', 'api'])
async def test_dispatch_admits_and_recovers_exact_trigger_context(owner, trigger):
    connection, job = owner
    context = normalize_context(trigger or 'schedule', 'owner@example.com', 'prepare now')
    params = {'job_id': job['id'], 'request_id': 'fire', 'extra_prompt': context['run_prompt'] or None}
    if trigger:
        params['run_context'] = context
    try:
        submit = await connection.dispatch({'id': 1, 'method': 'cron.submit', 'params': params})
        assert 'result' in submit, submit
        recovered = await connection.dispatch({'id': 2, 'method': 'cron.recover', 'params': params})
        assert recovered['result']['session_id'] == submit['result']['session_id']
        env = script_environment(recovered['result']['job'])
        assert env['HERMES_CRON_TRIGGER'] == (trigger or 'schedule')
        assert env['HERMES_CRON_RUN_PROMPT'] == context['run_prompt']
        assert env['HERMES_CRON_TRIGGER_ACTOR'] == context['trigger_actor']
        if trigger:
            for changed_context in (dict(context, trigger_actor='other@example.com'), None):
                changed = dict(params, run_context=changed_context)
                for method in ('cron.submit', 'cron.recover'):
                    answer = await connection.dispatch({'id': 3, 'method': method, 'params': changed})
                    assert answer['error']['data']['reason'] == 'admission_conflict'
    finally:
        await connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('tamper', ['principal', 'request', 'context', 'extra_field', 'non_cron_policy'])
async def test_context_claim_accepts_only_the_exact_stored_cron_fire(owner, tamper):
    from copy import deepcopy
    from dataclasses import replace
    from gateway.session_contract import SessionRef
    from gateway.session_operator import check_local_input, is_viewer_input
    from hermes_state_runtime import RuntimeStoreError
    connection, job = owner
    context = normalize_context('manual', 'owner@example.com')
    params = {'job_id': job['id'], 'request_id': 'fire', 'extra_prompt': None, 'run_context': context}
    try:
        response = await connection.dispatch({'id': 1, 'method': 'cron.submit', 'params': params})
        receipt, authority = response['result'], connection.authority
        ref = SessionRef(authority.profile_id, receipt['session_id'])
        row = get_session_admission(authority.db, admission_id=receipt['admission_id'])
        check_local_input(authority, ref, row)
        assert not is_viewer_input(row)
        row = deepcopy(row)
        adapter = authority.runner._adapter_for_source(authority.sessions[ref.session_id].source)
        mutations = {
            'principal': (row, 'principal_id', 'other'),
            'request': (row, 'request_id', row['request_id'] + '-other'),
            'context': (row['payload']['run_context'], 'trigger_actor', 'other@example.com'),
            'extra_field': (row['payload'], 'env', {'HOME': '/other'}),
            'non_cron_policy': (adapter.policies, ref.session_id,
                                replace(adapter.policies[ref.session_id], source='cli')),
        }
        target, key, value = mutations[tamper]
        target[key] = value
        with pytest.raises(RuntimeStoreError, match='permission_denied'):
            check_local_input(authority, ref, row)
    finally:
        await connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('trigger', [None, 'manual', 'api'])
async def test_dispatch_drains_cron_to_terminal_with_the_frozen_context(owner, monkeypatch, trigger):
    from cron import scheduler
    from gateway.session_contract import SessionRef
    connection, job = owner
    context = normalize_context(trigger or 'schedule', 'owner@example.com', 'prepare now')
    seen = []

    def run_job(frozen, **kwargs):
        seen.append(script_environment(frozen))
        return True, 'Prepared report', 'Prepared report', None

    monkeypatch.setattr(scheduler, 'run_job', run_job)  # the external model boundary only
    params = {'job_id': job['id'], 'request_id': 'fire', 'extra_prompt': context['run_prompt'] or None}
    if trigger:
        params['run_context'] = context
    try:
        response = await connection.dispatch({'id': 1, 'method': 'cron.submit', 'params': params})
        assert 'result' in response, response
        receipt = response['result']
        authority = connection.authority
        await authority._drain(SessionRef(authority.profile_id, receipt['session_id']))
        state = await connection.dispatch({'id': 2, 'method': 'cron.status', 'params': receipt})
        assert state['result']['status'] == 'terminal', state
        assert state['result']['result'] == [True, 'Prepared report', 'Prepared report', None]
        assert len(seen) == 1
        assert seen[0]['HERMES_CRON_TRIGGER'] == context['trigger']
        assert seen[0]['HERMES_CRON_TRIGGER_ACTOR'] == context['trigger_actor']
        assert seen[0]['HERMES_CRON_RUN_PROMPT'] == context['run_prompt']
    finally:
        await connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('include_null', [False, True])
async def test_dispatch_null_context_matches_omitted_context(owner, include_null):
    connection, job = owner
    omitted = {'job_id': job['id'], 'request_id': 'fire', 'extra_prompt': 'prepare now'}
    explicit_null = dict(omitted, run_context=None)
    try:
        missing = await connection.dispatch({'id': 1, 'method': 'cron.recover', 'params': explicit_null})
        assert missing['result']['status'] == 'missing'
        params = explicit_null if include_null else omitted
        submit = await connection.dispatch({'id': 2, 'method': 'cron.submit', 'params': params})
        assert 'result' in submit, submit
        receipt = submit['result']
        row = get_session_admission(connection.authority.db, admission_id=receipt['admission_id'])
        assert row['payload'] == {'text': omitted['extra_prompt']}
        for retry in (omitted, explicit_null):
            duplicate = await connection.dispatch({'id': 3, 'method': 'cron.submit', 'params': retry})
            assert duplicate['result'] == receipt
            recovered = await connection.dispatch({'id': 4, 'method': 'cron.recover', 'params': retry})
            assert recovered['result']['session_id'] == receipt['session_id']
            assert recovered['result']['admission_id'] == receipt['admission_id']
            assert recovered['result']['job'] == job
        changed = dict(omitted, run_context=normalize_context('manual', 'owner@example.com', 'prepare now'))
        for method in ('cron.submit', 'cron.recover'):
            answer = await connection.dispatch({'id': 5, 'method': method, 'params': changed})
            assert answer['error']['data']['reason'] == 'admission_conflict'
    finally:
        await connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('context', [
    {'trigger': 'unknown', 'trigger_actor': '', 'run_prompt': ''},
    {'trigger': 'manual', 'trigger_actor': 'owner@example.com', 'run_prompt': 'mismatch'},
    {'trigger': 'manual', 'trigger_actor': '', 'run_prompt': '', 'env': {'HOME': '/other'}},
])
async def test_dispatch_rejects_invalid_context_before_creating_a_session(owner, context):
    connection, job = owner
    params = {'job_id': job['id'], 'request_id': 'fire', 'extra_prompt': None, 'run_context': context}
    try:
        for method in ('cron.submit', 'cron.recover'):
            answer = await connection.dispatch({'id': 1, 'method': method, 'params': params})
            assert answer['error']['data']['reason'] == 'invalid_params'
        assert not connection.authority.sessions
        assert connection.authority.db.session_count() == 0
    finally:
        await connection.close()
