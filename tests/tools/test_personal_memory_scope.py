from contextvars import copy_context
from concurrent.futures import ThreadPoolExecutor
import pytest
from tools.memory_tool import MemoryStore, bind_personal_memory_dir, reset_personal_memory_dir


def scoped(path):
    token = bind_personal_memory_dir(path)
    try:
        store = MemoryStore()
        store.load_from_disk()
        return store
    finally:
        reset_personal_memory_dir(token)


def test_store_pins_actor_scope_across_lifecycle_and_threads(tmp_path):
    alice = scoped(tmp_path / "alice")
    bob = scoped(tmp_path / "bob")
    alice.memory_entries = ["ALICE_PRIVATE"]
    alice.save_to_disk("memory")
    bob.load_from_disk()
    assert bob.memory_entries == []
    alice.load_from_disk()
    assert alice.memory_entries == ["ALICE_PRIVATE"]
    token = bind_personal_memory_dir(tmp_path / "alice")
    try:
        with ThreadPoolExecutor() as pool:
            child = pool.submit(copy_context().run, MemoryStore).result()
        child.load_from_disk()
        assert child.memory_entries == ["ALICE_PRIVATE"]
    finally:
        reset_personal_memory_dir(token)


def test_bound_file_symlink_cannot_read_or_overwrite(tmp_path):
    store = scoped(tmp_path / "alice")
    secret = tmp_path / "secret"
    secret.write_text("PRIVATE_OTHER")
    (tmp_path / "alice" / "MEMORY.md").symlink_to(secret)
    with pytest.raises(PermissionError):
        store.load_from_disk()
    with pytest.raises(PermissionError):
        store.save_to_disk("memory")
    assert secret.read_text() == "PRIVATE_OTHER"


def test_scope_rejects_relative_and_parent_symlink(tmp_path):
    with pytest.raises(ValueError):
        bind_personal_memory_dir("relative")
    (tmp_path / "link").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(PermissionError):
        bind_personal_memory_dir(tmp_path / "link" / "memory")


def test_shared_conversation_blocks_memory_in_children_and_resets(tmp_path):
    from tools.memory_tool import get_builtin_memory_store_flags
    token = bind_personal_memory_dir(tmp_path / "alice", enabled=False)
    try:
        assert get_builtin_memory_store_flags({}) == (False, False)
        with ThreadPoolExecutor() as pool:
            future = pool.submit(copy_context().run, MemoryStore)
            with pytest.raises(PermissionError):
                future.result()
    finally:
        reset_personal_memory_dir(token)
    assert scoped(tmp_path / "private").memory_entries == []


def test_real_file_dispatch_denies_other_actor_and_shared_personal_reads(tmp_path, monkeypatch):
    import json
    from tools.file_tools import read_file_tool
    root = tmp_path / "personal"
    alice = root / "alice" / "memories"
    bob = root / "bob" / "memories"
    alice.mkdir(parents=True)
    bob.mkdir(parents=True)
    (alice / "MEMORY.md").write_text("ALICE_ONLY")
    (bob / "MEMORY.md").write_text("BOB_ONLY")
    monkeypatch.setenv("TERMINAL_ENV", "local")
    token = bind_personal_memory_dir(alice)
    try:
        assert "ALICE_ONLY" in read_file_tool(str(alice / "MEMORY.md"))
        assert "BOB_ONLY" not in read_file_tool(str(bob / "MEMORY.md"))
        assert "error" in json.loads(read_file_tool(str(bob / "MEMORY.md")))
    finally:
        reset_personal_memory_dir(token)
    token = bind_personal_memory_dir(alice, enabled=False)
    try:
        result = read_file_tool(str(alice / "MEMORY.md"))
        assert "ALICE_ONLY" not in result
        assert "error" in json.loads(result)
    finally:
        reset_personal_memory_dir(token)
