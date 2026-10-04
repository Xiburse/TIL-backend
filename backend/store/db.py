"""SQLite 存储层:连接、写入、读取、自愈。

写入顺序(重要):INSERT status='pending' -> 移动文件 -> UPDATE status='stored'。
进程若在两步之间崩溃,留下的是「pending 行 + 文件仍在 inbox」,下一轮
reconcile 会自愈。反过来先移动文件的话,崩溃会留下 library 里永远查不到
的孤儿文件 —— 而且回滚移动的动作还可能因为杀毒软件短暂占用文件而失败。

合集同理:INSERT status='building' -> 逐图处理 -> rebuild + finalized。
崩溃留下的 building 行由 reconcile 重算或删除。

表结构在 schema,迁移阶梯在 schema_migrate,只读查询在 search。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from backend import config
# SCHEMA_VERSION / init_schema / schema_version 从本模块转出:
# 它们概念上属于「数据库」,调用方 db.init_schema(conn) 读起来最自然。
from backend.store.schema import (
    _COLLECTION_COLUMNS,
    _INSERT_COLUMNS,
    SCHEMA_VERSION,
)
from backend.store.schema_migrate import init_schema, schema_version  # noqa: F401

_INSERT_SQL = (
    f"INSERT INTO images ({', '.join(_INSERT_COLUMNS)}) "
    f"VALUES ({', '.join(':' + c for c in _INSERT_COLUMNS)})"
)


def _now() -> str:
    return datetime.now().strftime(config.TS_DB_FMT)


def connect(db_path: Path) -> sqlite3.Connection:
    """建立可写连接。

    PRAGMA 分两类,不能混:
      - journal_mode=WAL 写进文件,之后一直有效
      - foreign_keys 是**连接级**开关,默认关闭,每次连接都得重新打开
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # WAL 下 NORMAL 不损完整性,最坏只丢最后一次提交,省掉每张图一次 fsync
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def close(conn: sqlite3.Connection) -> None:
    """收尾:把 WAL 合并回主库。

    不做这一步的话,只拷 tags.db 备份会漏掉还留在 -wal 里的数据。
    """
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
    except sqlite3.Error:
        pass
    finally:
        conn.close()


# ---------- 图片写入 ----------


def insert_image(conn: sqlite3.Connection, row: dict, status: str = "pending") -> int:
    """写入一行,提交后返回 id。

    status 是显式参数而不是写死的 —— 重建索引时插入的行必须是 'stored',
    否则 search_images 和 rebuild_collection_tags 全都过滤不到它。
    """
    data = {c: row.get(c) for c in _INSERT_COLUMNS}
    data["status"] = status
    cur = conn.execute(_INSERT_SQL, data)
    conn.commit()
    return int(cur.lastrowid)


def mark_stored(
    conn: sqlite3.Connection,
    image_id: int,
    rel_path: str,
    library_sha256: str | None = None,
    xmp_ok: int | None = None,
    size_bytes: int | None = None,
) -> None:
    conn.execute(
        "UPDATE images SET status='stored', rel_path=?, library_sha256=?, "
        "xmp_ok=?, size_bytes=COALESCE(?, size_bytes), error=NULL WHERE id=?",
        (rel_path, library_sha256, xmp_ok, size_bytes, image_id),
    )
    conn.commit()


def delete_image(conn: sqlite3.Connection, image_id: int) -> None:
    conn.execute("DELETE FROM images WHERE id=?", (image_id,))
    conn.commit()


def insert_tags(conn: sqlite3.Connection, image_id: int, records: list[tuple]) -> None:
    """records 是 (tag, category, confidence, passed) 四元组。"""
    conn.executemany(
        "INSERT OR REPLACE INTO tags (image_id, tag, category, confidence, passed) "
        "VALUES (?, ?, ?, ?, ?)",
        [(image_id, t, c, p, int(ok)) for t, c, p, ok in records],
    )
    conn.commit()


# ---------- 合集 ----------


def insert_collection(conn: sqlite3.Connection, row: dict,
                      status: str = "building") -> int:
    """status 是显式参数 —— 重建索引时插入的合集必须是 'finalized',
    否则 list_collections 过滤不到它。"""
    data = {c: row.get(c) for c in _COLLECTION_COLUMNS}
    data["status"] = status
    cur = conn.execute(
        f"INSERT INTO collections ({', '.join(_COLLECTION_COLUMNS)}) "
        f"VALUES ({', '.join(':' + c for c in _COLLECTION_COLUMNS)})",
        data,
    )
    conn.commit()
    return int(cur.lastrowid)


def delete_collection(conn: sqlite3.Connection, collection_id: int) -> None:
    conn.execute("DELETE FROM collections WHERE id=?", (collection_id,))
    conn.commit()


def find_collection_by_name_key(conn: sqlite3.Connection, name_key: str) -> list:
    return conn.execute(
        "SELECT * FROM collections WHERE name_key=? ORDER BY id", (name_key,)
    ).fetchall()


def get_collection(conn: sqlite3.Connection, collection_id: int):
    return conn.execute("SELECT * FROM collections WHERE id=?", (collection_id,)).fetchone()


def rebuild_collection_tags(conn: sqlite3.Connection, collection_id: int) -> int:
    """从每图 tag 重算合集的频次缓存,返回写入的 tag 条数。

    这是**唯一**的频次计算实现 —— 刻意从库里算而不是用内存 Counter,这样
    reconcile 能直接复用它;而 collection_tags 也就明确是可重算的派生缓存,
    不是会和真相漂移的第二份数据。

    刻意不读 tags.passed:那个标记来自「图片当初被打标时」的阈值,而合集里
    可能混着复用旧 tag 的重复图,两套阈值混在一起口径就乱了。这里统一按
    本合集自己的阈值重新套用 confidence,因此结果永远可重算。
    """
    row = get_collection(conn, collection_id)
    if row is None:
        return 0

    gen = row["gen_threshold"]
    char = row["char_threshold"]
    gen = config.GENERAL_THRESHOLD if gen is None else gen
    char = config.CHARACTER_THRESHOLD if char is None else char

    conn.execute("DELETE FROM collection_tags WHERE collection_id=?", (collection_id,))
    conn.execute(
        """
        WITH agg AS (
            SELECT t.tag AS tag,
                   t.category AS category,
                   -- confidence < 0 表示分数未知(重建索引时只从 dc:subject
                   -- 拿到了 tag 名)。这时退回按 passed 计数 —— 否则整张
                   -- 频次表会被算成空,而且一次崩溃自愈就能把它抹掉。
                   SUM(CASE WHEN t.confidence < 0 THEN t.passed
                            WHEN t.confidence >=
                                 (CASE WHEN t.category = 4 THEN :char ELSE :gen END)
                            THEN 1 ELSE 0 END) AS cnt,
                   COUNT(*) AS loose,
                   AVG(CASE WHEN t.confidence < 0 THEN 0.0 ELSE t.confidence END) AS avgc
            FROM tags t
            JOIN images i ON i.id = t.image_id
            WHERE i.collection_id = :cid AND i.status = 'stored'
            GROUP BY t.tag
        )
        INSERT INTO collection_tags
            (collection_id, tag, category, count, count_loose, avg_confidence)
        SELECT :cid, tag, category, cnt, loose, avgc
        FROM agg
        ORDER BY cnt DESC, avgc DESC, tag ASC
        LIMIT :top
        """,
        {"cid": collection_id, "gen": gen, "char": char, "top": config.COLLECTION_TOP_N},
    )
    conn.commit()
    return conn.execute(
        "SELECT COUNT(*) FROM collection_tags WHERE collection_id=?", (collection_id,)
    ).fetchone()[0]


def count_collection_images(conn: sqlite3.Connection, collection_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM images WHERE collection_id=? AND status='stored'",
        (collection_id,),
    ).fetchone()[0]


def finalize_collection(
    conn: sqlite3.Connection,
    collection_id: int,
    failed_count: int = 0,
    skipped_files: int = 0,
    skipped_dirs: int = 0,
) -> int:
    """收尾:回填计数、重算频次、置 finalized。返回合集图片数。"""
    image_count = count_collection_images(conn, collection_id)
    conn.execute(
        "UPDATE collections SET status='finalized', finished_at=?, image_count=?, "
        "failed_count=?, skipped_files=?, skipped_dirs=? WHERE id=?",
        (_now(), image_count, failed_count, skipped_files, skipped_dirs, collection_id),
    )
    conn.commit()
    rebuild_collection_tags(conn, collection_id)
    return image_count


def collection_tag_rows(conn: sqlite3.Connection, collection_id: int) -> list:
    return conn.execute(
        "SELECT * FROM collection_tags WHERE collection_id=? "
        "ORDER BY count DESC, avg_confidence DESC, tag ASC",
        (collection_id,),
    ).fetchall()


# ---------- 图片读取 ----------


def find_by_hash(conn: sqlite3.Connection, digest: str):
    """找之前处理过的同一张图,用于复用 tag、跳过 GPU 推理。

    **两个哈希都要查**,缺一不可:
      - `source_sha256`  覆盖「用户从手机/别的设备重新投放同一张原件」
      - `library_sha256` 覆盖「用户把 library 里已带 XMP 的图再丢回 inbox」
                         —— 此时文件内容已因嵌入而改变,等于当初归档的那份
    """
    return conn.execute(
        "SELECT * FROM images WHERE (source_sha256=? OR library_sha256=?) "
        "AND status='stored' ORDER BY id DESC LIMIT 1",
        (digest, digest),
    ).fetchone()


def find_by_relpath(conn: sqlite3.Connection, rel_path: str):
    return conn.execute(
        "SELECT * FROM images WHERE rel_path=? ORDER BY id DESC LIMIT 1", (rel_path,)
    ).fetchone()


def load_tags(conn: sqlite3.Connection, image_id: int) -> list[tuple]:
    rows = conn.execute(
        "SELECT tag, category, confidence, passed FROM tags WHERE image_id=?",
        (image_id,),
    ).fetchall()
    return [(r["tag"], r["category"], r["confidence"], bool(r["passed"])) for r in rows]


# ---------- 自愈 ----------


def reconcile(conn: sqlite3.Connection) -> tuple[int, int]:
    """修正崩溃/中断留下的中间状态,返回 (图片条数, 合集条数)。

    图片 pending 行有两种结局:
      - 目标文件已存在 -> 移动其实成功了,补写成 stored
      - 目标不存在     -> 删掉这行。文件若还在 inbox,下一轮会被当成新图
                          正常处理;真被删了也无所谓,这张图从没成功入库过。

    合集 building 行同理:有图片就重算并收尾(否则一次投放会被静默劈成
    两条记录),一张都没有就删行。
    注意 failed_count / skipped_files / skipped_dirs 崩溃后无从重建 ——
    它们是 best-effort。
    """
    fixed_images = 0
    for row in conn.execute("SELECT id, rel_path FROM images WHERE status='pending'"):
        target = config.ROOT / row["rel_path"]
        if target.is_file():
            conn.execute("UPDATE images SET status='stored' WHERE id=?", (row["id"],))
        else:
            conn.execute("DELETE FROM images WHERE id=?", (row["id"],))
        fixed_images += 1
    if fixed_images:
        conn.commit()

    fixed_collections = 0
    for row in conn.execute("SELECT * FROM collections WHERE status='building'"):
        # 判据必须覆盖**整棵子树**:支持嵌套之后,父层可以合法地 0 张直接图片,
        # 只有子合集。按「本层图片数 > 0」判的话,这种纯结构性的父行会被删掉,
        # 而子行的 parent_coll_id 仍指向它 —— 树就断了(删父行不会级联到子行)。
        has_children = False
        if row["coll_id"]:
            has_children = conn.execute(
                "SELECT 1 FROM collections WHERE parent_coll_id=? LIMIT 1",
                (row["coll_id"],)).fetchone() is not None
        if count_collection_images(conn, row["id"]) > 0 or has_children:
            finalize_collection(
                conn, row["id"],
                failed_count=row["failed_count"],
                skipped_files=row["skipped_files"],
                skipped_dirs=row["skipped_dirs"],
            )
        else:
            delete_collection(conn, row["id"])
        fixed_collections += 1

    return fixed_images, fixed_collections


# ---------- 重建索引时的写入 ----------


def reindex_upsert_image(conn: sqlite3.Connection, row: dict) -> tuple[int | None, bool]:
    """重建索引时写入或更新一行,返回 (image_id, 是否新建);image_id 为 None
    表示「这是复制品,应跳过」。

    **锚点必须同时看 rel_path 和 image_id —— 身份是文件名的,不是路径。**
    只按 rel_path 查的话,任何移动/改名(包括布局迁移改掉的目录名)都会让旧行
    的路径失效,于是按新路径插入、撞上 image_id 的 UNIQUE 约束、被当成「重复」跳过。
    结果是索引里全是失效路径,而重建还自称成功。
    """
    existing = find_by_relpath(conn, row["rel_path"])

    if existing is None and row.get("image_id"):
        same_id = conn.execute(
            "SELECT * FROM images WHERE image_id=?", (row["image_id"],)).fetchone()
        if same_id is not None:
            # 旧路径在磁盘上已经没了 → 这是移动/改名,认领这一行并更新路径。
            # 以磁盘实际状态为准,不信任 DB —— 所以迁移改目录名之后,
            # 直接跑一次 reindex 就能自愈,不需要任何专用的路径修复代码。
            if not (config.ROOT / same_id["rel_path"]).exists():
                existing = same_id
            else:
                return None, False   # 旧路径还在 → 用户复制了一份,跳过并告警

    if existing is None:
        return insert_image(conn, row, status="stored"), True

    image_id = int(existing["id"])
    # ⚠ rel_path **必须**在 SET 里 —— 认领一行(id 命中但路径变了)的场景下,
    # 更新路径正是这次 upsert 的全部目的。把它排除掉的话,三分支会静默失效:
    # 分支进了、UPDATE 也跑了,但路径一个字都没改。
    # status 则相反,它由下面的字面量统一处理,不能进 SET —— 否则会生成一个
    # 调用方未必提供的 :status 绑定参数,直接抛 ProgrammingError。
    cols = [c for c in row if c not in ("image_id", "status")]
    sets = ", ".join(f"{c}=:{c}" for c in cols)
    conn.execute(
        f"UPDATE images SET {sets}, status='stored' WHERE id=:id",
        {**row, "id": image_id},
    )
    conn.commit()
    return image_id, False


def collection_by_dir(conn: sqlite3.Connection, dir_rel_path: str):
    return conn.execute(
        "SELECT * FROM collections WHERE dir_rel_path=?", (dir_rel_path,)
    ).fetchone()


def upsert_collection(conn: sqlite3.Connection, row: dict) -> int:
    """按 dir_rel_path upsert 一个合集(重建索引用)。

    重建时**必须显式写 status='finalized'**,否则 list_collections 会把它过滤掉。
    """
    existing = collection_by_dir(conn, row["dir_rel_path"])
    if existing is not None:
        image_id = int(existing["id"])
        cols = [c for c in _COLLECTION_COLUMNS if c not in ("dir_rel_path", "status")]
        sets = ", ".join(f"{c}=:{c}" for c in cols)
        conn.execute(
            f"UPDATE collections SET {sets}, status='finalized' WHERE id=:id",
            {**row, "id": image_id},
        )
        conn.commit()
        return image_id
    return insert_collection(conn, row, status="finalized")


# ---------- meta(版本号) ----------


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()


def counts(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """(图片数, 合集数, tag 行数),只看已入库的。"""
    images = conn.execute(
        "SELECT COUNT(*) FROM images WHERE status='stored'").fetchone()[0]
    colls = conn.execute(
        "SELECT COUNT(*) FROM collections WHERE status='finalized'").fetchone()[0]
    tag_rows = conn.execute(
        "SELECT COUNT(*) FROM tags t JOIN images i ON i.id=t.image_id "
        "WHERE i.status='stored'").fetchone()[0]
    return images, colls, tag_rows
