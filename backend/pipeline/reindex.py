from __future__ import annotations

import argparse
import hashlib

import json
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from backend.imgfmt import readwrite
from backend import config
from backend.config import EXIT_FATAL, EXIT_OK, EXIT_PARTIAL
from backend.common import fsutil
from backend.common import local_state, logbook
from backend.pipeline import archiver, scanner
from backend.pipeline.migrate_layout import migrate_layout
from backend.pipeline.version import write_version_file
from backend.store import db, journal


def run_reindex(args, emit) -> dict:
    """从 library/ 重建本地索引。**不需要模型** —— 新设备上往往没有那个 1.2GB 文件。"""
    conn = db.connect(config.DB_PATH)
    try:
        db.init_schema(conn)
        migrate_layout(conn)

        placeholders, bytes_ = [], 0
        for f in config.LIBRARY_DIR.rglob(f"*"):
            if f.is_file() and f.suffix.lower() in config.IMAGE_EXTS and fsutil._is_placeholder(f):
                placeholders.append(f)
                try:
                    bytes_ += f.stat().st_size
                except OSError:
                    pass
        if placeholders and not args.yes:
            emit.log(f"注意:library/ 里有 {len(placeholders)} 个 OneDrive 占位符"
                  f"(仅在线),读取它们将下载约 {bytes_ / 1e9:.2f} GB。")
            emit.log("确认请加 --yes;否则将跳过这些文件。")
            return {"ok": False}

        # 先读遍所有 journal。命中就走快路径(完全不碰图片文件),没命中的
        # 才回退逐图读 XMP —— 这正是 journal 存在的意义:新设备读几个文件
        # 就能建好缓存,而不是为每张图各发一次网络请求。
        device = local_state.device_id()
        moved = journal.adopt_legacy(device)
        if moved:
            emit.log(f"已把 {moved} 个旧命名的 journal 收编为 journal_{device}.jsonl")

        entries: dict[str, tuple] = {}
        if not args.from_images:
            entries, conflicts, devices = journal.read_all(device)
            if conflicts:
                logbook.record_warn(
                    "journal 冲突",
                    f"本设备的 journal 有 {len(conflicts)} 个冲突副本"
                    f"({', '.join(f.name for f in conflicts[:3])}),"
                    f"说明两端在离线状态下同时写了同一个文件。读取端已自动合并,"
                    f"但建议人工确认后删掉多余的。")
            if devices:
                emit.log(f"发现 {len(devices)} 台设备的 journal:{', '.join(devices)}")
            if entries:
                emit.log(f"journal 命中 {len(entries)} 条记录(免去逐图读取)")
        else:
            emit.log("--from-images:忽略 journal,逐图读 XMP(最慢但最权威)")

        seen: set[str] = set()
        stats = {"images": 0, "collections": 0, "skipped": 0, "failed": 0,
                 "from_journal": 0, "from_xmp": 0}
        # 回退读 XMP 的那些,顺手把 journal 补上(按文件夹聚合,最后一次追加)
        backfill: dict[Path, list[str]] = {}

        now = datetime.now().strftime(config.TS_DB_FMT)

        def walk_tree(folder: Path, day: str, parent_coll_id: str | None,
                      depth: int) -> None:
            """递归遍历一层日期目录下的合集树。

            **身份从目录名推导**(COLL_DIR_RE 匹配即合集),index.json 只用来补
            name/side/parent。这样 index.json 丢了或读不出来,也不会让整棵子树
            不进索引 —— 它是派生缓存,不该成为唯一真相源。
            """
            ordinal = 0
            for entry in sorted(folder.iterdir()):
                if not entry.is_dir() or not config.COLL_DIR_RE.match(entry.name):
                    continue          # 不是 coll_id 目录:不递归,也不建行
                if scanner._is_hidden(entry) or fsutil._is_placeholder(entry):
                    continue
                ordinal += 1
                head = _read_collection_index(entry) or {}
                side = head.get("side")
                if not isinstance(side, int):
                    # index.json 缺失/坏了:按兄弟顺序推导
                    side = ordinal if parent_coll_id else 0
                name = head.get("name") or entry.name
                if not head.get("name"):
                    logbook.record("告警", f"合集目录 {entry.name} 缺 index.json,"
                                           f"原名无法恢复,暂用目录名代替")
                cid = db.upsert_collection(conn, {
                    "name": str(name), "name_key": str(name).casefold(),
                    "coll_id": entry.name, "side": side,
                    "parent_coll_id": parent_coll_id, "depth": depth,
                    "dir_rel_path": entry.relative_to(config.ROOT).as_posix(),
                    "date_dir": head.get("date_dir") or day,
                    "saved_at": head.get("saved_at") or now,
                    "finished_at": head.get("finished_at"),
                    "image_count": 0, "failed_count": 0,
                    "skipped_files": 0, "skipped_dirs": 0,
                    "gen_threshold": head.get("gen_threshold"),
                    "char_threshold": head.get("char_threshold"),
                })
                n = 0
                for img in sorted(entry.iterdir()):
                    if img.is_file() and img.suffix.lower() in config.IMAGE_EXTS:
                        if _reindex_image(conn, img, day, cid, seen, stats,
                                          entries, backfill):
                            n += 1
                conn.execute("UPDATE collections SET image_count=? WHERE id=?", (n, cid))
                conn.commit()
                db.rebuild_collection_tags(conn, cid)
                stats["collections"] += 1
                # index.json 是派生缓存 —— 重建时一并刷新,免得它带着陈旧字段
                # (比如迁移改了目录名之后的 dir_rel_path)一直留在那儿。
                try:
                    archiver.write_collection_index(conn, cid, entry)
                except (OSError, ValueError, UnicodeError) as e:
                    logbook.record("告警", f"{entry.name} 的 index.json 刷新失败: {e}")
                walk_tree(entry, day, entry.name, depth + 1)

        for day in sorted(config.LIBRARY_DIR.iterdir()):
            if not day.is_dir() or not local_state._DATE_DIR_RE.match(day.name):
                continue
            for entry in sorted(day.iterdir()):
                if (entry.is_file() and entry.suffix.lower() in config.IMAGE_EXTS):
                    _reindex_image(conn, entry, day.name, None, seen, stats,
                                   entries, backfill)
            walk_tree(day, day.name, None, 0)

        backfilled = 0
        for folder, lines in backfill.items():
            if journal.append(folder, lines, device):
                backfilled += len(lines)
        if backfilled:
            emit.log(f"已回填 journal {backfilled} 条 —— 下一台设备重建时就不必再读这些图片了")

        if args.prune:
            # 先算再问:递归遍历器里任何一处 continue 漏掉一个子树,都会让
            # 那一整棵的图片行被判成「已不存在」而删掉。删数据必须显式确认。
            doomed = [r["id"] for r in conn.execute(
                "SELECT id, rel_path FROM images WHERE status='stored'")
                if r["rel_path"] not in seen]
            if doomed:
                emit.log(f"\n--prune:将删除 {len(doomed)} 行(library 里找不到对应文件)")
                if not args.yes:
                    emit.log("  已跳过。确认无误请加 --yes。")
                else:
                    for i in doomed:
                        conn.execute("DELETE FROM images WHERE id=?", (i,))
                    conn.commit()
                    stats["skipped"] += len(doomed)

        digest, images, colls = local_state.library_digest()
        _, _, tag_rows = db.counts(conn)
        db.set_meta(conn, "digest", digest)
        db.set_meta(conn, "rev", str(int(time.time() * 1000)))
        write_version_file(digest, images, colls, tag_rows)

        emit.log(f"\n重建完成:索引 {stats['images']} 张图片、{stats['collections']} 个合集"
              f"(磁盘上 {images} 张)")
        emit.log(f"  来源:journal {stats['from_journal']} 条,"
              f"回退读 XMP {stats['from_xmp']} 条"
              f"({stats['from_journal'] + stats['from_xmp']} 中 "
              f"{stats['from_journal'] * 100 // max(1, stats['from_journal'] + stats['from_xmp'])}% 免读文件)")
        if stats["failed"]:
            emit.log(f"  {stats['failed']} 个文件读不出 tag")
        return {"ok": True}
    finally:
        db.close(conn)


def _read_collection_index(cdir: Path) -> dict | None:
    f = cdir / config.COLLECTION_INDEX_NAME
    if not f.is_file():
        return None
    try:
        doc = json.loads(f.read_text(encoding="utf-8"))
        return doc if doc.get("v") == config.FILE_FORMAT_VERSION else None
    except (OSError, ValueError):
        return None


def _reindex_image(conn, img: Path, day: str,
                   cid: int | None, seen: set[str], stats: dict,
                   entries: dict[str, tuple] | None = None,
                   backfill: dict[Path, list[str]] | None = None) -> bool:
    """把一张已归档的图片读回索引。返回是否成功。

    优先走 journal(不碰图片文件);journal 没命中、或文件被外部改过时才回退
    去读 XMP。journal 只是加速层,回退路径保证它丢了/落后了也能完整重建。
    """
    rel = img.relative_to(config.ROOT).as_posix()
    seen.add(rel)

    if fsutil._is_placeholder(img):
        stats["failed"] += 1
        return False

    parsed = scanner.parse_archive_name(img.name)
    if parsed is None:
        logbook.record("告警", f"{rel} 不符合归档命名规则,时间无法恢复")
        mtime = ctime = uid = None
    else:
        mtime, ctime, uid = parsed

    try:
        actual_size = img.stat().st_size
    except OSError:
        stats["failed"] += 1
        return False

    hit = (entries or {}).get(rel)
    if hit is not None and hit[1].get("sz") == actual_size:
        # journal 命中。用它顺便补齐纯读 XMP 拿不到的那几列
        # (文件大小、归档哈希、载体类型)。
        data, jextra = hit
        size, lib_hash = actual_size, jextra.get("lh")
        xmp_ok = jextra.get("xk")
        if xmp_ok is None:
            xmp_ok = 0 if readwrite.sidecar_path(img).is_file() else 1
        subjects: list[str] = []
        stats["from_journal"] += 1
    else:
        # 回退:逐图读 XMP。**大小对不上说明文件被外部改过**(比如有人在
        # Lightroom 里加了关键词),这时必须读文件,否则外部编辑永远看不见。
        stats["from_xmp"] += 1
        try:
            blob = img.read_bytes()
        except OSError:
            stats["failed"] += 1
            return False
        lib_hash = hashlib.sha256(blob).hexdigest()
        size = actual_size
        xmp_ok = 0 if readwrite.sidecar_path(img).is_file() else 1
        subjects, data = readwrite.read_from_bytes(blob, img.suffix)
        if data is None:
            data = readwrite.read_sidecar(img)
        # 回填 journal:这台设备既然已经从图片读出来了,就顺手记下来,
        # 免得下一台设备再做一遍同样的事。只追加,不重写。
        if data is not None and backfill is not None:
            backfill.setdefault(img.parent, []).append(
                journal.to_line(data, rel, size, lib_hash, xmp_ok))

    if data is not None:
        # dc:subject 里除了我们写的,还可能有人在别的工具里手工加的关键词。
        # 那些必须一并入库、且可被 --tag 查到,否则「我在 Lightroom 里加的
        # 标签程序看不见」就成了静默的数据丢失。置信度用 -1 哨兵表示未知。
        ours = data.all_names()
        extra = [(t, config.CAT_GENERAL, -1.0, True)
                 for t in subjects if t not in ours]
        records = data.tags + extra
        source_sha = data.source_sha256
        origin = data.origin_name
        saved = data.saved_at
        mtime = data.mtime or mtime
        ctime = data.ctime or ctime
        width, height, shot = data.width, data.height, data.shot_at
        rating, score = data.rating, data.rating_score
        prompt = data.prompt()
        tag_count = len(data.passed_names())
    elif subjects:
        # 只有 dc:subject(别的工具写的,或我们的 data 被剥掉)。
        # 置信度用 -1 哨兵表示未知,rebuild_collection_tags 会退回按 passed 计数。
        records = [(t, config.CAT_GENERAL, -1.0, True) for t in subjects]
        source_sha = origin = saved = width = height = shot = None
        rating = score = None
        prompt = ", ".join(t.replace("_", " ") for t in subjects)
        tag_count = len(subjects)
    else:
        stats["failed"] += 1
        return False

    row = {
        # 身份 = 文件名主干,不从 XMP 读 —— 文件名在磁盘上看得见、跨设备稳定,
        # 而 XMP 里那个字段可能是老版本写的 short_id。
        "status": "stored", "image_id": scanner.image_id_of(img.name),
        "filename": img.name, "rel_path": rel,
        "date_dir": day, "origin_name": origin or img.name, "ext": img.suffix.lower(),
        "size_bytes": size, "source_size_bytes": None,
        "width": width, "height": height,
        "mtime": mtime or datetime.now().strftime(config.TS_DB_FMT),
        "ctime": ctime or mtime or datetime.now().strftime(config.TS_DB_FMT),
        "saved_at": saved or datetime.now().strftime(config.TS_DB_FMT),
        "shot_at": shot, "source_sha256": source_sha, "library_sha256": lib_hash,
        "xmp_ok": xmp_ok,
        "prompt": prompt, "rating": rating, "rating_score": score,
        "tag_count": tag_count, "collection_id": cid,
        # 从 ishelf:data 恢复,否则重建后这两个值丢失,频次表会回落到
        # config 默认阈值 —— 当初若用 --general-threshold 0.5 跑的,
        # 重建后的频次口径就悄悄变了。
        "gen_threshold": data.gen_threshold if data else None,
        "char_threshold": data.char_threshold if data else None,
        "record_floor": data.record_floor if data else None,
        "model_name": data.model if data else None,
    }
    try:
        image_id, _ = db.reindex_upsert_image(conn, row)
    except sqlite3.IntegrityError:
        # id 撞车:用户在资源管理器里把文件复制到了别处
        logbook.record_warn("重复", f"{rel} 的 id 与已有记录冲突,已跳过")
        stats["skipped"] += 1
        return False

    if image_id is None:
        # 同 id 的行还在磁盘上的老路径里 → 用户复制了一份,这是两个不同的东西
        logbook.record_warn("重复", f"{rel} 与库中已有记录同 id、且原文件仍在,"
                                    f"按复制品跳过")
        stats["skipped"] += 1
        return False

    conn.execute("DELETE FROM tags WHERE image_id=?", (image_id,))
    db.insert_tags(conn, image_id, records)
    stats["images"] += 1
    return True
