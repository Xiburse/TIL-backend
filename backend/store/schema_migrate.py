
"""建表与迁移阶梯。

⚠ 绝不能把 ALTER 写进 SCHEMA:SQLite 没有 ADD COLUMN IF NOT EXISTS,
放进去会让第一次启动成功后、之后每次启动都报 duplicate column name。
"""

from __future__ import annotations

import sqlite3

from pathlib import Path

from backend.store.schema import _INDEXES, _SCHEMA, SCHEMA_VERSION


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    _migrate(conn)
    conn.executescript(_INDEXES)  # 必须在迁移补列之后,见 _INDEXES 的注释
    conn.commit()


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _columns(conn: sqlite3.Connection, table: str = "images") -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn: sqlite3.Connection, name: str, decl: str,
                table: str = "images") -> None:
    if name not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _migrate(conn: sqlite3.Connection) -> None:
    """把老库补到当前版本。

    _SCHEMA 已经用 IF NOT EXISTS 建好了新表,这里只处理建表覆盖不到的情况。
    SQLite 没有 ADD COLUMN IF NOT EXISTS,所以 ALTER 绝不能写进 _SCHEMA ——
    那会让第一次启动成功后,之后每次启动都报 duplicate column name。
    """
    version = schema_version(conn)

    if version < 2:
        # collections 已在 _SCHEMA 里建好,父表存在,ALTER 才能带上 REFERENCES
        _add_column(conn, "collection_id",
                    "INTEGER REFERENCES collections(id) ON DELETE SET NULL")

    if version < 3:
        _add_column(conn, "source_sha256", "TEXT")
        _add_column(conn, "library_sha256", "TEXT")
        _add_column(conn, "source_size_bytes", "INTEGER")
        _add_column(conn, "xmp_ok", "INTEGER")

        # **回填是必须的,这是整个迁移里最容易漏、代价最大的一步。**
        # 不回填的话老行的 source_sha256 全是 NULL,find_by_hash 一个都命中
        # 不了 —— 整个已有库会被当成新图重跑一遍 GPU,并再归档一份。
        if "sha256" in _columns(conn):
            conn.execute(
                "UPDATE images SET source_sha256 = sha256 WHERE source_sha256 IS NULL"
            )
            conn.execute("DROP INDEX IF EXISTS idx_images_sha")
            # 留着一个叫 sha256 却装「写之前的哈希」的列,迟早有人当成文件内容
            # 指纹来用,所以直接删掉。
            conn.execute("ALTER TABLE images DROP COLUMN sha256")

    if version < 4:
        # 合集不再记录 model(每张图自己的 ishelf:data 里仍然记着)。
        # 留一个恒为 NULL 的列只会误导后来的人,直接删掉。
        cols = {row[1] for row in conn.execute("PRAGMA table_info(collections)")}
        if "model_name" in cols:
            conn.execute("ALTER TABLE collections DROP COLUMN model_name")

    if version < 5:
        # 合集改用 coll_id 命名 + 支持嵌套。
        # **列声明里不能带 UNIQUE** —— SQLite 会报 "Cannot add a UNIQUE column",
        # 而且此时 _SCHEMA 已经跑过、迁移没完成,库会卡在半迁移状态里,
        # 之后每轮启动都死在同一处。唯一性交给 _INDEXES 的索引。
        _add_column(conn, "coll_id", "TEXT", "collections")
        _add_column(conn, "side", "INTEGER NOT NULL DEFAULT 0", "collections")
        _add_column(conn, "parent_coll_id", "TEXT", "collections")
        _add_column(conn, "depth", "INTEGER NOT NULL DEFAULT 0", "collections")

    if version < 6:
        # uuid -> image_id:身份改成「归档文件名的主干」,也就是磁盘上的名字本身。
        #
        # 原来的 uuid 列存的是 short_id(文件名第三段),而自增主键更糟 ——
        # 换台设备重建数据库,同一个合集/图片拿到的数字就变了。文件名是唯一
        # 一个**在磁盘上看得见、跨设备稳定**的身份。
        cols = _columns(conn)
        if "uuid" in cols and "image_id" not in cols:
            conn.execute("ALTER TABLE images RENAME COLUMN uuid TO image_id")
            cols = _columns(conn)
        if "image_id" in cols:
            # 回填:从 filename 取主干(不带扩展名)
            rows = conn.execute(
                "SELECT id, filename, image_id FROM images").fetchall()
            for r in rows:
                stem = Path(r[1]).stem
                if r[2] != stem:
                    conn.execute("UPDATE images SET image_id=? WHERE id=?",
                                 (stem, r[0]))

    if version < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    conn.commit()
