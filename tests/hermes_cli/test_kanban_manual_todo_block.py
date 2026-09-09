"""Human Todo blocking keeps reasons, dependency gates and worker fencing."""
import json

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def conn(tmp_path):
    connection = kb.connect(tmp_path / 'kanban.db')
    yield connection
    connection.close()


def todo(conn):
    task_id = kb.create_task(conn, title='Manual blocked task')
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (task_id,))
    return task_id


def test_todo_block_requires_explicit_manual_opt_in_and_records_reason(conn):
    task_id = todo(conn)
    assert not kb.block_task(conn, task_id, reason='Human needs input')
    assert kb.get_task(conn, task_id).status == 'todo'
    assert kb.block_task(conn, task_id, reason='Human needs input', allow_todo=True)
    task = kb.get_task(conn, task_id)
    assert task.status == 'blocked'
    assert task.claim_lock is None and task.worker_pid is None
    event = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = 'blocked'", (task_id,)).fetchone()
    assert json.loads(event['payload'])['reason'] == 'Human needs input'
    assert json.loads(event['payload'])['source_status'] == 'todo'
    assert kb.unblock_task(conn, task_id)
    assert kb.get_task(conn, task_id).status == 'ready'


def test_manual_unblock_retains_unfinished_parent_gate(conn):
    parent = kb.create_task(conn, title='Unfinished prerequisite')
    child = kb.create_task(conn, title='Waiting child', parents=[parent])
    assert kb.block_task(conn, child, reason='Waiting for review', allow_todo=True)
    assert kb.unblock_task(conn, child)
    assert kb.get_task(conn, child).status == 'todo'
    assert not kb.complete_task(conn, child, allow_pending=True)


@pytest.mark.parametrize('recurrences', [0, kb.BLOCK_RECURRENCE_LIMIT - 1])
def test_worker_run_fence_cannot_opt_into_todo_even_on_loop_branch(conn, recurrences):
    task_id = todo(conn)
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET current_run_id = 42, block_recurrences = ? WHERE id = ?', (recurrences, task_id))
    assert not kb.block_task(conn, task_id, allow_todo=True, expected_run_id=42)
    task = kb.get_task(conn, task_id)
    assert task.status == 'todo' and task.block_recurrences == recurrences


def test_manual_block_keeps_recurrence_limit(conn):
    task_id = todo(conn)
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET block_recurrences = ? WHERE id = ?', (kb.BLOCK_RECURRENCE_LIMIT - 1, task_id))
    assert kb.block_task(conn, task_id, reason='Still unresolved', allow_todo=True)
    task = kb.get_task(conn, task_id)
    assert task.status == 'triage'
    assert task.block_recurrences == kb.BLOCK_RECURRENCE_LIMIT
    assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'block_loop_detected'", (task_id,)).fetchone()[0] == 1


@pytest.mark.parametrize('status', ['triage', 'done', 'archived'])
def test_manual_todo_opt_in_does_not_allow_other_invalid_states(conn, status):
    task_id = todo(conn)
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET status = ? WHERE id = ?', (status, task_id))
    assert not kb.block_task(conn, task_id, allow_todo=True)
    assert kb.get_task(conn, task_id).status == status


def test_dependency_kind_routing_keeps_original_eligibility(conn):
    task_id = todo(conn)
    assert not kb.block_task(conn, task_id, kind='dependency', allow_todo=True)
    assert kb.get_task(conn, task_id).status == 'todo'
