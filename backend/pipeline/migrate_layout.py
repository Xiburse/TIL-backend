from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import uuid

from backend import config
from backend.common import local_state, logbook
from backend.store import db, journal


MIGRATION_MAP = config.LOCAL_DIR / "layout_migration.json"


def _write_map(mapping: dict) -> None:
    """把迁移映射落到 _local/(同步区之外)并 fsync。

    它是**可重入的唯一锚点** —— 中途被杀之后,靠它知道每个旧目录该改成哪个
    coll_id 才对。放 library 里会被多端争抢,所以只能放这。
    """
    config.LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    with open(MIGRATION_MAP, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())


def _patch_index_for_migration(folder: Path, info: dict) -> None:
    """在改目录名**之前**,先把新字段写进 index.json。

    顺序很重要:先写文件再改名。反过来的话,改名成功而写文件失败,就成了
    「目录名是 coll_id 但 index.json 还是旧的」,而目录名才是身份来源。
    """
    path = folder / config.COLLECTION_INDEX_NAME
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = {"v": config.FILE_FORMAT_VERSION}
    doc.update({"id": info["coll_id"], "side": info["side"],
                "parent": info["parent"], "depth": info["depth"]})
    if not doc.get("name"):
        doc["name"] = info["name"]
    with open(path, "w", encoding="utf-8", errors="replace") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())


def _repair_journal(folder: Path, old_rel: str, new_rel: str) -> int:
    """journal 里的 p 字段是旧路径 —— **追加**修正版,绝不重写。

    这正是 journal 自己规定的姿势:「要修改就再追加一条」。删掉旧文件是错的:
    违反只追加约束,而且多设备下离线的另一端同步后会把文件带回来。
    """
    lines: list[str] = []
    for f in journal.found_journals(folder):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.split("\n"):
            if not line.strip():
                continue
            try:
                doc = json.loads(line)
            except ValueError:
                continue
            # ⚠ 比的是**目录前缀**,不是相等:journal 里的 p 是图片的完整路径
            # (旧目录/文件名),而 old_rel 只到目录。写成相等判断的话一条都
            # 匹配不上,而且不会报错 —— 静默丢掉整条快路径。
            p = doc.get("p") or ""
            if p.startswith(old_rel + "/"):
                doc["p"] = new_rel + p[len(old_rel):]
                lines.append(json.dumps(doc, ensure_ascii=False, separators=(",", ":")))
    if lines:
        journal.append(folder, lines, local_state.device_id())
    return len(lines)


def migrate_layout(conn: sqlite3.Connection) -> int:
    """旧布局(合集目录名 == 原名)→ 新布局(目录名 == coll_id)。可重入。

    每步的判据都是「目标已存在就跳过」,所以中途被杀之后再跑一次能得到
    完全相同的结果。
    """
    if db.get_meta(conn, "layout_version") == str(config.LAYOUT_VERSION):
        return 0
    if not config.LIBRARY_DIR.is_dir():
        db.set_meta(conn, "layout_version", str(config.LAYOUT_VERSION))
        return 0

    mapping: dict = {}
    if MIGRATION_MAP.is_file():
        try:
            mapping = json.loads(MIGRATION_MAP.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            mapping = {}

    migrated = 0
    for day in sorted(config.LIBRARY_DIR.iterdir()):
        if not day.is_dir() or not local_state._DATE_DIR_RE.match(day.name):
            continue
        for entry in sorted(day.iterdir()):
            if not entry.is_dir() or config.COLL_DIR_RE.match(entry.name):
                continue          # 已经是新布局了
            if not (entry / config.COLLECTION_INDEX_NAME).is_file():
                continue          # 没有 index.json 的不当老式合集(可能是垃圾目录)

            old_rel = entry.relative_to(config.ROOT).as_posix()
            info = mapping.get(old_rel)
            if info is None:
                info = {
                    "coll_id": f"{datetime.now():%Y%m%d-%H%M%S}_{uuid.uuid4().hex[:12]}",
                    "side": 0, "parent": None, "depth": 0, "name": entry.name,
                }
                mapping[old_rel] = info
                _write_map(mapping)   # 先落锚点,再做任何改动

            _patch_index_for_migration(entry, info)

            target = day / info["coll_id"]
            if not target.exists():
                os.rename(entry, target)
            new_rel = target.relative_to(config.ROOT).as_posix()

            conn.execute(
                "UPDATE collections SET coll_id=?, side=?, parent_coll_id=?, depth=?, "
                "dir_rel_path=? WHERE dir_rel_path=?",
                (info["coll_id"], info["side"], info["parent"], info["depth"],
                 new_rel, old_rel))
            conn.commit()
            _repair_journal(target, old_rel, new_rel)
            migrated += 1

    db.set_meta(conn, "layout_version", str(config.LAYOUT_VERSION))
    if migrated:
        logbook.record_warn("布局迁移",
                            f"已把 {migrated} 个合集迁移到新布局(目录名改为 coll_id)")
        # 迁移自己收尾:rel_path 的修复完全交给 reindex 的三分支自愈,
        # 不写任何专用的路径修补代码,也不留给用户记着去跑。
        # 函数内导入:reindex 在模块级导入本模块,模块级互相导入会成环。
        from backend.pipeline import reindex as _reindex
        _reindex.run_reindex(argparse.Namespace(from_images=False, prune=False, yes=True))
    return migrated
