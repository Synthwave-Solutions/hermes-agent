"""Policy persists atomically and gates actual external dispatch, including failures."""
import pytest
from cron import jobs, scheduler_delivery


def test_create_update_and_bad_payload_are_atomic():
    job = jobs.create_job('check service', 'every 1h', category='Monitoring', managed_by='synthwave')
    assert job['notify'] == {'in_app':'failures', 'delivery':'all'}
    job = jobs.update_job(job['id'], {'notify': {'delivery':'off'}})
    assert job['notify']['delivery'] == 'off'
    with pytest.raises(ValueError):
        jobs.update_job(job['id'], {'name':'must not persist','notify': {'delivery':'typo'}})
    assert jobs.get_job(job['id'])['name'] == job['name']


@pytest.mark.parametrize('level,failed,expected', [('off',False,False),('off',True,False),('failures',False,False),('failures',True,True),('all',False,True)])
def test_policy_gates_transport_before_target_resolution(monkeypatch,level,failed,expected):
    seen=[]
    monkeypatch.setattr(scheduler_delivery, '_resolve_delivery_targets', lambda *a,**k: seen.append(True) or [])
    job={'id':'probe','deliver':'local','notify':{'delivery':level}}
    assert scheduler_delivery._deliver_result(job,'private result',for_failure=failed) is None
    assert bool(seen) is expected
    assert bool(job.get('_notification_policy_muted')) is (not expected)
