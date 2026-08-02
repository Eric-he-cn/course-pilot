"""迁移必须能在老库上反复跑。

真出过的事故：残留的唯一索引引用了要退役的列，DROP COLUMN 删到重建索引那步才报错；
而失败发生在版本号落库之前，已经提交的 ALTER 又回不去，下次启动直接撞
duplicate column——整个工作区从此起不来。所以这里既测能过，也测能重复跑。
"""
from __future__ import annotations

import sqlite3

import pytest

from core.store import ADDED_COLUMNS, MIGRATIONS, RETIRED_COLUMNS, WIDENED_CHECKS, SQLiteStore

# 老库里的残留唯一索引，同时引用两个要退役的列。它早已从迁移列表里删掉，
# 所以只有这个年代之前建的库才带着它。
LEGACY_INDEX = "idx_legacy_owner_scoped"

LATEST = max(version for version, _ in MIGRATIONS)


def legacy_db(path, *, with_channel_index: bool = False, pre_added: bool = False) -> None:
    """建一个跑完全部编号迁移、但还没做过结构对账的老库。"""
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        for version, sql in MIGRATIONS:
            connection.executescript(sql)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))
        if with_channel_index:
            connection.execute(
                f"CREATE UNIQUE INDEX {LEGACY_INDEX} ON sessions(source, owner_id) "
                "WHERE scope_mode = 'general' AND kind = 'user'"
            )
        if pre_added:
            for table, column, declaration in ADDED_COLUMNS:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        connection.commit()


def structure(path) -> dict[str, set[str]]:
    with sqlite3.connect(path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        return {"indexes": indexes} | {
            table: {row[1] for row in connection.execute(f"PRAGMA table_info({table})")} for table in tables
        }


def test_retired_columns_survive_the_leftover_channel_index(tmp_path):
    path = tmp_path / "legacy.db"
    legacy_db(path, with_channel_index=True)

    SQLiteStore(path).migrate()

    after = structure(path)
    assert LEGACY_INDEX not in after["indexes"], "残留索引没删掉，下次 DROP COLUMN 还会炸"
    assert "idx_sessions_updated" in after["indexes"], "把不相关的索引也删了"
    for table, column in RETIRED_COLUMNS:
        assert column not in after[table], f"{table}.{column} 没退役"
    for table, column, _ in ADDED_COLUMNS:
        assert column in after[table], f"{table}.{column} 没补上"


def test_migrate_is_idempotent(tmp_path):
    path = tmp_path / "twice.db"
    legacy_db(path, with_channel_index=True)

    SQLiteStore(path).migrate()
    first = structure(path)
    SQLiteStore(path).migrate()

    assert structure(path) == first, "第二次 migrate 改动了结构"


def test_migrate_tolerates_a_column_that_already_exists(tmp_path):
    """半途失败留下的库：列已经加上了，版本号却没落库。再启动必须能起来。"""
    path = tmp_path / "half.db"
    legacy_db(path, pre_added=True)
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version = ?", (LATEST,))
        connection.commit()

    SQLiteStore(path).migrate()

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM schema_migrations WHERE version = ?", (LATEST,)).fetchone()[0] == 1


def _seed_session_with_a_message(path) -> None:
    """一条消息 + 一条引用它的压缩记录：重建 messages 时最容易被弄丢的就是这条外键。"""
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("INSERT INTO sessions(id, title, scope_mode, course_id, source, created_at, updated_at) "
                           "VALUES ('s1', 't', 'general', NULL, 'web', '2026-08-01T00:00:00Z', '2026-08-01T00:00:00Z')")
        connection.execute("INSERT INTO messages(id, session_id, role, turn_id, content, created_at) "
                           "VALUES ('m1', 's1', 'user', 'turn_1', '你好', '2026-08-01T00:00:01Z')")
        connection.execute(
            "INSERT INTO session_compactions(id, session_id, covers_through_message_id, covers_through_created_at, "
            "covers_message_count, summary_text, prompt_version, turn_id, created_at) "
            "VALUES ('k1', 's1', 'm1', '2026-08-01T00:00:01Z', 1, '摘要', 'v1', 'turn_1', '2026-08-01T00:00:02Z')")
        connection.commit()


def test_tool_role_is_accepted_after_the_check_is_widened(tmp_path):
    """工具正文落库的前提：messages.role 认得 'tool'。老库的 CHECK 只认三种角色。"""
    path = tmp_path / "widen.db"
    legacy_db(path)
    _seed_session_with_a_message(path)

    SQLiteStore(path).migrate()

    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("INSERT INTO messages(id, session_id, role, turn_id, content, created_at) "
                           "VALUES ('m2', 's1', 'tool', 'turn_1', '检索结果', '2026-08-01T00:00:03Z')")
        connection.commit()
        assert connection.execute("SELECT content FROM messages WHERE role = 'tool'").fetchone()[0] == "检索结果"


def test_widening_the_check_keeps_rows_indexes_and_the_foreign_key(tmp_path):
    """整表重建最容易丢的三样：数据、索引、别的表指过来的外键。"""
    path = tmp_path / "rebuild.db"
    legacy_db(path)
    _seed_session_with_a_message(path)
    SQLiteStore(path).migrate()

    _table, _old, new = WIDENED_CHECKS[0]
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        assert new in connection.execute("SELECT sql FROM sqlite_master WHERE name='messages'").fetchone()[0]
        assert connection.execute("SELECT content FROM messages WHERE id = 'm1'").fetchone()[0] == "你好"
        assert connection.execute("SELECT covers_through_message_id FROM session_compactions").fetchone()[0] == "m1"
        assert not connection.execute("PRAGMA foreign_key_check").fetchall(), "重建后留下了悬空外键"
        indexes = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='messages'")}
        assert "idx_messages_session_created" in indexes, "重建时把索引丢了"
        columns = {row[1] for row in connection.execute("PRAGMA table_info(messages)")}
        assert {"activity_json", "choices_json"} <= columns, "重建时把后加的列丢了"
        # 放宽不等于取消：写一个没列进去的角色仍然要被拒。
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO messages(id, session_id, role, turn_id, content, created_at) "
                               "VALUES ('m9', 's1', 'nobody', 'turn_1', 'x', '2026-08-01T00:00:09Z')")


def test_widening_the_check_is_idempotent(tmp_path):
    """第二次启动不该再重建一遍：结构逐字不变，数据也还在。"""
    path = tmp_path / "widen-twice.db"
    legacy_db(path)
    _seed_session_with_a_message(path)

    SQLiteStore(path).migrate()
    first = structure(path)
    SQLiteStore(path).migrate()

    assert structure(path) == first
    _table, _old, new = WIDENED_CHECKS[0]
    with sqlite3.connect(path) as connection:
        assert new in connection.execute("SELECT sql FROM sqlite_master WHERE name='messages'").fetchone()[0]
        assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 1


def test_an_unexpected_check_clause_aborts_instead_of_guessing(tmp_path):
    """DDL 和预期对不上就别动表——猜着改比不改危险得多。"""
    path = tmp_path / "odd.db"
    legacy_db(path)
    table, old, _new = WIDENED_CHECKS[0]
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA writable_schema=ON")
        ddl = connection.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0]
        connection.execute("UPDATE sqlite_master SET sql = ? WHERE name = ?", (ddl.replace(old, "CHECK(1)"), table))
        connection.execute("PRAGMA writable_schema=OFF")
        connection.commit()

    with pytest.raises(RuntimeError, match="CHECK 与预期不符"):
        SQLiteStore(path).migrate()


def test_every_numbered_migration_records_its_version(tmp_path):
    """版本号必须和建表语句同批提交，否则中间失败就会只落一半。"""
    path = tmp_path / "fresh.db"
    SQLiteStore(path).migrate()

    with sqlite3.connect(path) as connection:
        recorded = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
    assert recorded == {version for version, _ in MIGRATIONS}
