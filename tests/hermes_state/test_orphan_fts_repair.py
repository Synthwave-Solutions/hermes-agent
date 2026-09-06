import sqlite3
import pytest
from scripts.repair_orphaned_fts import quarantine_orphaned_fts, BASES, COLUMNS

def orphan_db():
    c=sqlite3.connect(":memory:")
    c.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, content TEXT)")
    c.execute("INSERT INTO messages VALUES(1, 'retained canonical content')")
    for base in BASES:
        for suffix, columns in COLUMNS.items():
            c.execute(f'CREATE TABLE "{base}_{suffix}" ({", ".join(columns)})')
        c.execute(f"CREATE TRIGGER {base}_insert AFTER INSERT ON messages BEGIN INSERT INTO {base}(rowid,content) VALUES(new.id,new.content); END")
    c.commit()
    return c

def test_quarantine_preserves_rows_and_recreates_index():
    c=orphan_db()
    before=c.execute("SELECT * FROM messages").fetchall()
    assert quarantine_orphaned_fts(c)==list(BASES)
    assert c.execute("SELECT * FROM messages").fetchall()==before
    for base in BASES:
        c.execute(f"CREATE VIRTUAL TABLE {base} USING fts5(content)")
        c.execute(f"INSERT INTO {base}(rowid,content) SELECT id,content FROM messages")
        assert c.execute(f"SELECT count(*) FROM {base} WHERE {base} MATCH 'retained'").fetchone()[0]==1
    c.commit()
    assert quarantine_orphaned_fts(c)==[]

def test_unknown_shadow_layout_refuses_without_changes():
    c=orphan_db()
    c.execute("DROP TABLE messages_fts_data")
    c.execute("CREATE TABLE messages_fts_data(id, block, unexpected)")
    c.commit()
    before=c.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall()
    with pytest.raises(ValueError): quarantine_orphaned_fts(c)
    assert c.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall()==before

def test_partial_shadow_set_refuses_without_changes():
    c=orphan_db()
    c.execute("DROP TABLE messages_fts_data")
    c.commit()
    with pytest.raises(ValueError): quarantine_orphaned_fts(c)
    assert c.execute("SELECT count(*) FROM messages").fetchone()[0]==1
    assert c.execute("SELECT 1 FROM sqlite_master WHERE name='messages_fts_insert'").fetchone()
