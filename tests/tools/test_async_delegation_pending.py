"""An idle parent can still own live or not-yet-delivered child work."""
import sqlite3

import pytest

from tools import async_delegation as delegation


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / 'state.db'
    monkeypatch.setattr(delegation, '_db_path', lambda: path)
    monkeypatch.setattr(delegation, '_records', {})
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE async_delegations (delegation_id TEXT, origin_ui_session_id TEXT, state TEXT, delivery_state TEXT, parent_session_id TEXT DEFAULT '', origin_session TEXT DEFAULT '')")
    return path


def test_pending_combines_live_and_durable_without_double_counting(ledger):
    delegation._records.update({
        'live': {'origin_ui_session_id': 'parent', 'status': 'running'},
        'finalizing': {'origin_ui_session_id': 'parent', 'status': 'finalizing'},
        'other-live': {'origin_ui_session_id': 'other', 'status': 'running'},
    })
    with sqlite3.connect(ledger) as conn:
        conn.executemany('INSERT INTO async_delegations (delegation_id, origin_ui_session_id, state, delivery_state) VALUES (?,?,?,?)', [
            ('live', 'parent', 'running', 'pending'),
            ('queued-result', 'parent', 'completed', 'pending'),
            ('already-delivered', 'parent', 'completed', 'delivered'),
            ('other', 'other', 'completed', 'pending'),
        ])
    assert delegation.pending_for_session('parent') == 3
    assert delegation.pending_for_session('other') == 2
    assert delegation.pending_for_session('') == 0


def test_queued_completion_stays_pending_until_delivery(ledger):
    with sqlite3.connect(ledger) as conn:
        conn.execute('INSERT INTO async_delegations (delegation_id, origin_ui_session_id, state, delivery_state) VALUES (?,?,?,?)', ('child', 'parent', 'completed', 'pending'))
    assert delegation.active_for_session('parent') == 0
    assert delegation.pending_for_session('parent') == 1
    with sqlite3.connect(ledger) as conn:
        conn.execute("UPDATE async_delegations SET delivery_state='delivered'")
    assert delegation.pending_for_session('parent') == 0


def test_status_does_not_create_missing_database(tmp_path, monkeypatch):
    path = tmp_path / 'missing.db'
    monkeypatch.setattr(delegation, '_db_path', lambda: path)
    monkeypatch.setattr(delegation, '_records', {})
    assert delegation.pending_for_session('parent') == 0
    assert not path.exists()


def test_schema_without_delegation_table_is_empty(tmp_path, monkeypatch):
    path = tmp_path / 'old.db'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE unrelated (id TEXT)')
    monkeypatch.setattr(delegation, '_db_path', lambda: path)
    monkeypatch.setattr(delegation, '_records', {})
    assert delegation.pending_for_session('parent') == 0


def test_corrupt_ledger_is_unknown_not_false_completion(ledger):
    ledger.write_bytes(b'not a sqlite database')
    with pytest.raises(sqlite3.DatabaseError):
        delegation.pending_for_session('parent')


def test_legacy_ui_origin_requires_both_exact_parent_and_routing_aliases(ledger):
    delegation._records.update({
        'legacy-live': {'origin_ui_session_id': '', 'parent_session_id': 'parent', 'session_key': 'parent', 'status': 'running'},
        'conflicting-live': {'origin_ui_session_id': 'other', 'parent_session_id': 'parent', 'session_key': 'parent', 'status': 'running'},
    })
    with sqlite3.connect(ledger) as conn:
        conn.executemany('INSERT INTO async_delegations VALUES (?,?,?,?,?,?)', [
            ('legacy-pending', '', 'completed', 'pending', 'parent', 'parent'),
            ('explicit-other', 'other', 'running', 'pending', 'parent', 'parent'),
            ('wrong-route', '', 'running', 'pending', 'parent', 'other'),
            ('wrong-parent', '', 'running', 'pending', 'other', 'parent'),
        ])
    assert delegation.pending_for_session('parent') == 2
    assert delegation.pending_for_session('other') == 2


def test_dropped_result_is_unknown_even_when_parent_has_finished(ledger):
    with sqlite3.connect(ledger) as conn:
        conn.execute('INSERT INTO async_delegations (delegation_id, origin_ui_session_id, state, delivery_state) VALUES (?,?,?,?)',
                     ('lost-result', 'parent', 'completed', 'dropped'))
    with pytest.raises(RuntimeError, match='not delivered'):
        delegation.pending_for_session('parent')
    assert delegation.pending_for_session('other') == 0


def test_trusted_profile_home_union_counts_queued_completion_and_dedupes(ledger, tmp_path):
    profile = tmp_path / 'bot'
    profile.mkdir()
    other = profile / 'state.db'
    with sqlite3.connect(ledger) as src, sqlite3.connect(other) as dst:
        src.backup(dst)
    for path, rows in [(ledger, [('same', 'parent', 'running', 'pending')]),
                       (other, [('same', 'parent', 'running', 'pending'), ('queued-in-bot', 'parent', 'completed', 'pending'), ('sibling', 'other', 'completed', 'pending')])]:
        with sqlite3.connect(path) as conn:
            conn.executemany('INSERT INTO async_delegations (delegation_id, origin_ui_session_id, state, delivery_state) VALUES (?,?,?,?)', rows)
    assert delegation.pending_for_session('parent', homes=[ledger.parent, profile, profile]) == 2
    assert delegation.pending_for_session('other', homes=[ledger.parent, profile]) == 1
    assert delegation.pending_for_session('parent') == 1


def test_unknown_profile_ledger_never_becomes_zero_from_another_home(ledger, tmp_path):
    broken = tmp_path / 'broken'
    broken.mkdir()
    (broken / 'state.db').write_bytes(b'invalid database')
    with pytest.raises(sqlite3.DatabaseError):
        delegation.pending_for_session('parent', homes=[ledger.parent, broken])
    with pytest.raises(ValueError):
        delegation.pending_for_session('parent', homes=[])
