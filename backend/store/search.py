
"""只读查询:按 tag / 合集检索,频次统计。"""

from __future__ import annotations

import sqlite3


def list_collections(
    conn: sqlite3.Connection, tag: str | None = None, min_freq: float | None = None
) -> list:
    """列出合集;给了 tag 就只列含该 tag 的合集(可再按最低频率筛)。"""
    where = ["c.status='finalized'"]
    params: list = []
    join = ""

    if tag:
        join = "JOIN collection_tags ct ON ct.collection_id = c.id AND ct.tag = ?"
        params.append(tag)
        if min_freq:
            # 用整数比较规避浮点边界:count/image_count >= min_freq
            where.append("ct.count * 100 >= c.image_count * ?")
            params.append(int(round(min_freq * 100)))

    # 子合集物理嵌在父目录里,所以按 dir_rel_path 排序天然就是树的先序
    # (library/<日期>/A/ < library/<日期>/A/B/ < library/<日期>/C/)。
    return conn.execute(
        f"SELECT c.* FROM collections c {join} WHERE {' AND '.join(where)} "
        f"ORDER BY c.date_dir, c.dir_rel_path",
        params,
    ).fetchall()


def search_images(
    conn: sqlite3.Connection,
    tags_all: list[str] | None = None,
    tags_any: list[str] | None = None,
    rating: str | None = None,
    date_dir: str | None = None,
    shot_from: str | None = None,
    shot_to: str | None = None,
    min_conf: float = 0.0,
    coll_id: str | None = None,
    collection_key: str | None = None,
    limit: int | None = None,
) -> list:
    """按条件检索已入库的图片。

    tags_all 是「全部命中」(AND),tags_any 是「任一命中」(OR)。
    min_conf 让阈值变成查询参数:当时没过阈值的 tag 只要分数够,这里照样能查。
    """
    where = ["i.status='stored'"]
    params: list = []

    if collection_key:
        where.append("i.collection_id IN (SELECT id FROM collections WHERE name_key=?)")
        params.append(collection_key)

    if coll_id:
        # 按合集 id(文件夹名)过滤 —— 不用数据库自增主键,那个换台设备重建
        # 数据库就变了,同一个数字指的可能是另一个合集。
        where.append("i.collection_id IN "
                     "(SELECT id FROM collections WHERE coll_id=?)")
        params.append(coll_id)

    # confidence < 0 表示分数未知(重建索引时只从 dc:subject 拿到了 tag 名)。
    # 这种 tag 必须能通过任何置信度过滤 —— 否则重建之后它们会永远查不到。
    if tags_all:
        placeholders = ",".join("?" * len(tags_all))
        where.append(
            f"(SELECT COUNT(DISTINCT t.tag) FROM tags t "
            f"WHERE t.image_id=i.id AND t.tag IN ({placeholders}) "
            f"AND (t.confidence < 0 OR t.confidence >= ?)) = ?"
        )
        params.extend(tags_all)
        params.append(min_conf)
        params.append(len(set(tags_all)))

    if tags_any:
        placeholders = ",".join("?" * len(tags_any))
        where.append(
            f"EXISTS (SELECT 1 FROM tags t WHERE t.image_id=i.id "
            f"AND t.tag IN ({placeholders}) "
            f"AND (t.confidence < 0 OR t.confidence >= ?))"
        )
        params.extend(tags_any)
        params.append(min_conf)

    if rating:
        where.append("i.rating=?")
        params.append(rating)

    if date_dir:
        where.append("i.date_dir=?")
        params.append(date_dir)

    if shot_from:
        # 拍摄时间优先用 EXIF,没有就退回 mtime
        where.append("COALESCE(i.shot_at, i.mtime)>=?")
        params.append(shot_from)

    if shot_to:
        where.append("COALESCE(i.shot_at, i.mtime)<=?")
        params.append(shot_to)

    sql = f"SELECT i.* FROM images i WHERE {' AND '.join(where)} ORDER BY i.mtime DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)

    return conn.execute(sql, params).fetchall()


def top_tags(conn: sqlite3.Connection, min_conf: float = 0.0, limit: int = 100) -> list:
    return conn.execute(
        "SELECT t.tag, COUNT(*) AS n FROM tags t "
        "JOIN images i ON i.id=t.image_id "
        "WHERE i.status='stored' AND (t.confidence < 0 OR t.confidence>=?) "
        "GROUP BY t.tag ORDER BY n DESC, t.tag ASC LIMIT ?",
        (min_conf, limit),
    ).fetchall()


def tree_order(rows: list) -> list:
    """把合集排成树序:父在前、同级按 side。

    光按 dir_rel_path 排不够 —— 子合集虽然物理嵌在父目录里(所以父一定在子前),
    但同级的兄弟是按随机 coll_id 命名的,路径排序会把它们的先后打乱,
    而 side 才是它们的真实次序。
    """
    by_parent: dict = {}
    known = {r["coll_id"] for r in rows}
    for r in rows:
        parent = r["parent_coll_id"] if r["parent_coll_id"] in known else None
        by_parent.setdefault(parent, []).append(r)

    out: list = []

    def emit(parent) -> None:
        for r in sorted(by_parent.get(parent, []),
                        key=lambda x: (x["side"] or 0, x["name"] or "")):
            out.append(r)
            emit(r["coll_id"])

    emit(None)
    return out or rows
