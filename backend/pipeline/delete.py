"""删除:图片、合集(含整棵子树)。

## 身份用「磁盘上的名字」,不用数据库主键

- **图片 id** = 归档文件名的主干,如 `20260928-044002_20260928-044002_c772aa6ea37f`
- **合集 id** = 合集文件夹名,即 `coll_id`,如 `20260928-205303_a174e15fa7d6`

数据库自增主键**不能当身份**:它由 SQLite 分配,换台设备重建数据库,同一个
合集/图片拿到的数字就变了。同一个 `collection_ids: [5]` 在 A 机器上指 cats、
在 B 机器上可能指 dogs —— 删除是不可逆的,这种错位不能接受。

文件名/文件夹名则是在磁盘上看得见、跨设备稳定、且天然唯一的。

## 删除是不可逆的 —— 这是唯一自洽的语义

library 是**真相源**。只删数据库行的话,下一轮重建索引会把这些图复活 ——
用户明明删过了。所以必须连图片文件(以及它旁边的 `_tags.json` 边车)一起删。

## 顺序:先删文件,再删库行

反过来的话,中途崩溃会留下「库里没有、盘上还在」的孤儿文件,一重建就复活了。
先删文件则崩溃后留下「库里有行、盘上没有」—— **这是一个一眼看得出来的状态**,
`--prune` 一跑就干净,而且**整个命令是幂等的**:把同一批参数重发一遍就能收尾。

## 一次删除会清掉的五处

1. 图片文件 + `_tags.json` 边车
2. journal 里的对应条目
3. `index.json` 里的对应内容(合集存活时重算分母)
4. 数据库里的行(images + tags 级联)
5. 删整个合集时,它的 `index.json` / `journal` 整个文件
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from backend import config
from backend.common import fsutil, logbook
from backend.imgfmt import readwrite
from backend.pipeline import archiver
from backend.store import db, journal


def _find_collection(conn: sqlite3.Connection, coll_id: str):
    """按 coll_id(文件夹名)找合集。合集的身份就是它的目录名。"""
    return conn.execute(
        "SELECT * FROM collections WHERE coll_id=?", (str(coll_id),)).fetchone()


def _subtree(conn: sqlite3.Connection, coll_id: str) -> list:
    """收集一个合集的整棵子树,**深的排在前面**。

    要按深度倒序删:子目录里的东西先消失,父目录才可能 rmdir 成功。
    """
    root = _find_collection(conn, coll_id)
    if root is None:
        return []

    out, stack = [], [root]
    while stack:
        row = stack.pop()
        out.append(row)
        kids = conn.execute(
            "SELECT * FROM collections WHERE parent_coll_id=? ORDER BY side",
            (row["coll_id"],)).fetchall()
        stack.extend(kids)

    out.sort(key=lambda r: -(r["depth"] or 0))
    return out


def _forget_image(conn: sqlite3.Connection, row_id: int) -> None:
    """删一行图片(tags 会随外键级联消失)。row_id 是数据库行号,仅内部使用。"""
    conn.execute("DELETE FROM images WHERE id=?", (row_id,))
    conn.commit()


def _delete_files(rel_path: str) -> list[str]:
    """删图片文件与它的边车。返回「本该消失却没消失」的名字。

    **容忍文件已经不在** —— 删除是幂等的,重跑一遍不该报错。
    """
    leftover: list[str] = []
    path = config.ROOT / rel_path
    for p in (path, readwrite.sidecar_path(path)):
        if not p.exists():
            continue
        try:
            fsutil.retry_os(lambda q=p: q.unlink(missing_ok=True))
        except OSError as e:
            leftover.append(f"{p.name}({e})")
    return leftover


def _refresh_collection(conn: sqlite3.Connection, coll_id: str) -> dict:
    """刷新一个**存活**合集的计数与频次表(删了它里面的图之后)。

    合集的频次分母是「本层图片数」。删掉一张分母就变了 —— 不重算的话
    `index.json` 会一直显示 10/10,而盘上只剩 8 张。
    """
    row = _find_collection(conn, coll_id)
    if row is None:
        return {}
    n = db.count_collection_images(conn, row["id"])
    conn.execute("UPDATE collections SET image_count=? WHERE id=?", (n, row["id"]))
    conn.commit()
    db.rebuild_collection_tags(conn, row["id"])
    try:
        archiver.write_collection_index(conn, row["id"], config.ROOT / row["dir_rel_path"])
    except (OSError, ValueError, UnicodeError) as e:
        logbook.record_warn("告警", f"合集 {row['name']!r} 的 index.json 刷新失败: {e}")
    return {"coll_id": coll_id, "name": row["name"], "image_count": n}


def _purge_collection_dir(conn: sqlite3.Connection, row) -> dict:
    """清掉一个合集目录里属于我们生成的文件,然后尽量 rmdir。

    **只有把目录移走才算完整删除** —— 残留说明用户在里面放过别的东西,
    这时保留原样并报告,绝不硬删用户的文件。
    """
    folder = config.ROOT / row["dir_rel_path"]
    removed: list[str] = []
    if folder.is_dir():
        for f in sorted(folder.iterdir()):
            # 只删我们自己生成的:index.json / journal / 图片 / 边车
            if config.is_managed_file(f.name) or f.suffix.lower() in config.IMAGE_EXTS:
                try:
                    fsutil.retry_os(lambda q=f: q.unlink(missing_ok=True))
                    removed.append(f.name)
                except OSError:
                    pass
        try:
            folder.rmdir()
        except OSError:
            pass

    leftover = []
    if folder.exists():
        leftover = sorted(p.name for p in folder.iterdir())

    return {"removed": removed, "leftover": leftover}


def run_delete(args, emit) -> dict:
    """删除图片与合集。

    参数:
      image_ids  字符串数组,图片 id(= 归档文件名主干,不带扩展名)
      coll_ids   字符串数组,合集 id(= 合集文件夹名)
      dry_run    预演,只报告不真删

    两个数组至少给一个,可以混用。合集会连同**整棵子树**一起删。
    """
    image_ids: list[str] = [str(x) for x in (args.image_ids or [])]
    coll_ids: list[str] = [str(x) for x in (args.coll_ids or [])]

    if not image_ids and not coll_ids:
        raise ValueError(
            "没有指定要删除的东西 —— image_ids 和 coll_ids 至少给一个")

    conn = db.connect(config.DB_PATH)
    try:
        db.init_schema(conn)

        # ---- 展开目标 ----
        # 合集连子树一起删:子合集物理嵌在父目录里,不连带删会留下
        # 「目录没了、库里还有子行」的断链。
        coll_rows: list = []
        seen_coll: set[str] = set()
        for cid in coll_ids:
            for row in _subtree(conn, cid):
                if row["coll_id"] not in seen_coll:
                    seen_coll.add(row["coll_id"])
                    coll_rows.append(row)

        # 图片 = 显式指定的 + 合集子树里的全部图片
        image_rows: list = []
        seen_img: set[int] = set()
        for iid in image_ids:
            r = conn.execute("SELECT * FROM images WHERE image_id=?", (iid,)).fetchone()
            if r is not None and r["id"] not in seen_img:
                seen_img.add(r["id"])
                image_rows.append(r)
        for row in coll_rows:
            for r in conn.execute(
                    "SELECT * FROM images WHERE collection_id=?", (row["id"],)):
                if r["id"] not in seen_img:
                    seen_img.add(r["id"])
                    image_rows.append(r)

        # 存活的合集:被删图片属于它,但它本身不在删除清单里 → 要重算频次
        surviving: set[str] = set()
        for r in image_rows:
            if r["collection_id"] is None:
                continue
            owner = conn.execute("SELECT coll_id FROM collections WHERE id=?",
                                 (r["collection_id"],)).fetchone()
            if owner and owner["coll_id"] not in seen_coll:
                surviving.add(owner["coll_id"])

        # 被删图片所在的目录 —— journal 按目录存,批量清比逐条清快得多
        purged_dirs: dict[str, set[str]] = {}

        report = {
            "images": [], "collections": [],
            "totals": {"images": 0, "collections": 0, "missing": 0, "leftover": 0},
            "dry_run": bool(args.dry_run),
        }

        # ---- 逐个删图片:先文件,后库行 ----
        for r in image_rows:
            item = {"image_id": r["image_id"], "rel_path": r["rel_path"],
                    "collection_id": r["collection_id"]}
            if args.dry_run:
                item["status"] = "would_delete"
                report["images"].append(item)
                report["totals"]["images"] += 1
                continue

            leftover = _delete_files(r["rel_path"])
            _forget_image(conn, r["id"])
            folder = Path(r["rel_path"]).parent.as_posix()
            purged_dirs.setdefault(folder, set()).add(r["rel_path"])
            item["status"] = "deleted" if not leftover else "partial"
            if leftover:
                item["leftover"] = leftover
                report["totals"]["leftover"] += 1
            report["images"].append(item)
            report["totals"]["images"] += 1
            emit.event("image_deleted", image_id=r["image_id"], rel_path=r["rel_path"])

        # ---- 清 journal:被删的图在它里面的记录就是死条目 ----
        journal_removed = 0
        for folder_rel, paths in purged_dirs.items():
            n = journal.purge_entries(config.ROOT / folder_rel, paths)
            if n:
                emit.event("journal_purged", folder=folder_rel, entries=n)
            journal_removed += n
        report["journal_entries_removed"] = journal_removed

        # ---- 存活合集:重算频次,免得 index.json 的分母是错的 ----
        refreshed = [info for cid in sorted(surviving)
                     if (info := _refresh_collection(conn, cid))]
        if refreshed:
            report["refreshed_collections"] = refreshed

        # ---- 删合集(深的在前,这样父目录才能 rmdir)----
        for row in coll_rows:
            item = {"coll_id": row["coll_id"], "name": row["name"]}
            if args.dry_run:
                item["status"] = "would_delete"
                report["collections"].append(item)
                report["totals"]["collections"] += 1
                continue

            files = _purge_collection_dir(conn, row)
            conn.execute("DELETE FROM collections WHERE id=?", (row["id"],))
            conn.commit()
            item["status"] = "deleted" if not files["leftover"] else "partial"
            item["removed"] = files["removed"]
            if files["leftover"]:
                item["leftover"] = files["leftover"]
                report["totals"]["leftover"] += 1
            report["collections"].append(item)
            report["totals"]["collections"] += 1
            emit.event("collection_deleted", coll_id=row["coll_id"], name=row["name"])

        # 指定了但不存在的
        found_images = {r["image_id"] for r in image_rows}
        for iid in image_ids:
            if iid not in found_images:
                report["images"].append({"image_id": iid, "status": "missing"})
                report["totals"]["missing"] += 1
        found_colls = {r["coll_id"] for r in coll_rows}
        for cid in coll_ids:
            if cid not in found_colls:
                report["collections"].append({"coll_id": cid, "status": "missing"})
                report["totals"]["missing"] += 1

        # 删完更新版本号,否则别的设备会以为索引没变
        if not args.dry_run and report["totals"]["images"]:
            from backend.common import local_state
            from backend.pipeline.version import write_version_file
            digest, images, colls = local_state.library_digest()
            _, _, tag_rows = db.counts(conn)
            db.set_meta(conn, "digest", digest)
            db.set_meta(conn, "rev", str(int(time.time() * 1000)))
            write_version_file(digest, images, colls, tag_rows)

        return report
    finally:
        conn.close()
