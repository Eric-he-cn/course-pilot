"""迁移必须能在老库上反复跑。

真出过的事故：残留的唯一索引引用了要退役的列，DROP COLUMN 删到重建索引那步才报错；
而失败发生在版本号落库之前，已经提交的 ALTER 又回不去，下次启动直接撞
duplicate column——整个工作区从此起不来。所以这里既测能过，也测能重复跑。
"""
from __future__ import annotations

import sqlite3

from core.store import ADDED_COLUMNS, MIGRATIONS, RETIRED_COLUMNS, SQLiteStore

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


def test_every_numbered_migration_records_its_version(tmp_path):
    """版本号必须和建表语句同批提交，否则中间失败就会只落一半。"""
    path = tmp_path / "fresh.db"
    SQLiteStore(path).migrate()

    with sqlite3.connect(path) as connection:
        recorded = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
    assert recorded == {version for version, _ in MIGRATIONS}
