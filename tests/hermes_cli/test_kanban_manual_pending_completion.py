"""Manual pending-task completion retains dependency and worker run fences."""
import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def conn(tmp_path):
    connection = kb.connect(tmp_path / "kanban.db")
    yield connection
    connection.close()


def set_status(conn, task_id, status):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))


@pytest.mark.parametrize("status", ["todo", "triage"])
def test_pending_completion_requires_manual_opt_in_and_records_normal_lifecycle(conn, status):
    task_id = kb.create_task(conn, title="Manually finished work")
    set_status(conn, task_id, status)
    assert not kb.complete_task(conn, task_id)
    assert kb.get_task(conn, task_id).status == status
    assert kb.complete_task(conn, task_id, allow_pending=True, result="Human completed it")
    completed = kb.get_task(conn, task_id)
    assert completed.status == "done"
    assert completed.result == "Human completed it"
    assert completed.completed_at is not None
    assert completed.claim_lock is None
    events = conn.execute("SELECT kind FROM task_events WHERE task_id = ?", (task_id,)).fetchall()
    assert sum(event["kind"] == "completed" for event in events) == 1


def test_manual_pending_completion_cannot_bypass_unfinished_dependency(conn):
    parent = kb.create_task(conn, title="Prerequisite")
    child = kb.create_task(conn, title="Depends on prerequisite", parents=[parent])
    assert kb.get_task(conn, child).status == "todo"
    assert not kb.complete_task(conn, child, allow_pending=True)
    assert kb.get_task(conn, child).status == "todo"
    assert kb.get_task(conn, child).completed_at is None
    assert kb.complete_task(conn, parent)
    assert kb.complete_task(conn, child, allow_pending=True)


@pytest.mark.parametrize("status", ["todo", "triage", "archived", "done"])
def test_manual_opt_in_does_not_expand_run_fenced_worker_states(conn, status):
    task_id = kb.create_task(conn, title="Worker completion fence")
    set_status(conn, task_id, status)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET current_run_id = 42 WHERE id = ?", (task_id,))
    assert not kb.complete_task(conn, task_id, allow_pending=True, expected_run_id=42)
    assert kb.get_task(conn, task_id).status == status


def test_manual_opt_in_cannot_restore_archived_or_missing_task(conn):
    task_id = kb.create_task(conn, title="Archived")
    assert kb.archive_task(conn, task_id)
    assert not kb.complete_task(conn, task_id, allow_pending=True)
    assert kb.get_task(conn, task_id).status == "archived"
    assert not kb.complete_task(conn, "missing", allow_pending=True)
