from __future__ import annotations

import argparse

import json
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import uuid

from backend.imgfmt import packet
from backend.imgfmt import readwrite
from backend.imgfmt import selftest
from backend import config
from backend.config import EXIT_FATAL, EXIT_OK, EXIT_PARTIAL
from backend.common import fsutil, local_state, logbook
from backend.pipeline import archiver, scanner
from backend.store import db, journal


def build_tagger(args: argparse.Namespace, emit):
    """加载模型。只有真要推理时才调用 —— 模型有 1.2GB,空跑和重建都不该碰它。"""
    from backend.ai import tagger as tagger_mod
    # ↑ 这一行就是模型加载的开关,别为了「整洁」提到模块顶部

    emit.event("model_loading", path=str(config.MODEL_PATH))
    t0 = time.perf_counter()
    t = tagger_mod.WDTaggerLocal(
        config.MODEL_PATH, config.TAGS_CSV, args.device, use_gpu=not args.cpu
    )
    t.warmup()   # 把 CUDA 上下文初始化的耗时挪到批处理开始之前
    seconds = time.perf_counter() - t0
    emit.event("model_ready", seconds=round(seconds, 2), provider=t.providers[0])
    return t


def run_check(args: argparse.Namespace, emit) -> dict:
    """自检:XMP 段链往返(不依赖模型)+ GPU 加速。返回结构化的检查结果。"""
    problems = selftest.self_test()
    emit.event("check_xmp", ok=not problems, problems=problems)
    if problems:
        return {"xmp_ok": False, "problems": problems, "gpu": None}

    t = build_tagger(args, emit)
    info = t.check()
    gpu = {
        "provider": info["providers"][0],
        "target_size": info["target_size"],
        "tags_in_csv": info["tags_in_csv"],
        "ms": round(info["ms"], 1),
        "warning": info["warning"],
    }
    emit.event("check_gpu", **gpu)
    return {"xmp_ok": True, "problems": [], "gpu": gpu}


def build_data(records, meta: dict, args, tagger, image_id: str,
               coll_id: str | None = None, coll_name: str | None = None) -> packet.ImageTags:
    best_rating = None
    best_score = None
    for name, score in (meta.get("ratings") or {}).items():
        if best_score is None or score > best_score:
            best_rating, best_score = name, score
    return packet.ImageTags(
        tags=records,
        rating=best_rating, rating_score=best_score,
        model=tagger.model_name,
        gen_threshold=args.general_threshold,
        char_threshold=args.character_threshold,
        record_floor=config.RECORD_FLOOR, top_n=config.TOP_N,
        source_sha256=meta["source_sha256"], origin_name=meta["origin_name"],
        saved_at=datetime.now().strftime(config.TS_DB_FMT),
        mtime=meta["mtime_dt"].strftime(config.TS_DB_FMT),
        ctime=meta["ctime_dt"].strftime(config.TS_DB_FMT),
        shot_at=meta["shot_at"], width=meta["width"], height=meta["height"],
        image_id=image_id,
        # 合集名写进图片:目录名现在是不可读的 coll_id,原名只剩 index.json
        # 一份,而 index.json 只是派生缓存。带上这两个字段,归属和名字就能
        # 从图片本身重建,保住「图片是真相源」这条不变量。
        coll_id=coll_id, coll_name=coll_name,
    )


def process_one(tagger, conn, src: Path, args, day: str,
                coll_parts: tuple[str, ...] = (), collection_id: int | None = None,
                coll_name: str | None = None, emit=None) -> tuple[str, str | None]:
    """处理单张图,返回 ('ok' | 'dupe' | 'dry', journal 行或 None)。

    第二个返回值由调用方攒起来、按文件夹批量追加进 journal —— 单张追加会有
    N 次 fsync,而 journal 只是加速层,批量写足够安全。

    写盘顺序(崩溃安全的关键):
      INSERT pending → 写 staging → 校验 → 放库 → 算归档哈希 → 删源 → mark_stored
    源文件在目标通过校验之前**一个字节都不动**。
    """
    meta = scanner.collect_meta(src)
    meta["origin_name"] = src.name

    prior = db.find_by_hash(conn, meta["source_sha256"])
    if prior is not None:
        # 复用:只删源,**不再归档** —— 否则重复图会在 library 里存第二份。
        # 也不写 journal:那份图当初入库时已经记过了。
        if not args.dry_run:
            fsutil.retry_os(src.unlink)
        if emit is not None:
            emit.event("image_done", name=src.name, outcome="dupe", tags=None,
                       rel_path=prior["rel_path"], collection=coll_name)
        return "dupe", None

    res = tagger.predict(src, args.general_threshold, args.character_threshold,
                         config.RECORD_FLOOR, config.TOP_N)
    meta["ratings"] = res["ratings"]

    # coll_parts 是从根到本层的 coll_id 链;子合集物理嵌在父目录里
    parts = (day, *coll_parts)
    dest_dir = config.LIBRARY_DIR.joinpath(*parts)

    if args.dry_run:
        name = scanner.make_filename(meta, scanner.new_short_id())
        data = build_data(res["records"], meta, args, tagger, "dryrun",
                          coll_parts[-1] if coll_parts else None, coll_name)
        emit.log(f"    [dry-run] {src.name} -> {'/'.join(parts)}/{name} "
              f"({len(data.passed_names())} 个过阈值 tag)")
        emit.log(f"    [dry-run]   dc:subject = {data.passed_names()[:6]}")
        return "dry", None

    for _ in range(5):
        short_id = scanner.new_short_id()
        filename = scanner.make_filename(meta, short_id)
        image_id = scanner.image_id_of(filename)   # 身份 = 文件名主干
        rel_path = f"{config.LIBRARY_DIR.name}/{'/'.join(parts)}/{filename}"
        data = build_data(res["records"], meta, args, tagger, image_id,
                          coll_parts[-1] if coll_parts else None, coll_name)

        row = {
            "image_id": image_id, "filename": filename, "rel_path": rel_path,
            "date_dir": day, "origin_name": src.name, "ext": meta["ext"],
            "size_bytes": meta["size_bytes"], "source_size_bytes": meta["size_bytes"],
            "width": meta["width"], "height": meta["height"],
            "mtime": meta["mtime_dt"].strftime(config.TS_DB_FMT),
            "ctime": meta["ctime_dt"].strftime(config.TS_DB_FMT),
            "saved_at": data.saved_at, "shot_at": meta["shot_at"],
            "source_sha256": meta["source_sha256"], "library_sha256": None, "xmp_ok": None,
            "collection_id": collection_id,
            "prompt": data.prompt(), "rating": data.rating,
            "rating_score": data.rating_score, "tag_count": len(data.passed_names()),
            "gen_threshold": data.gen_threshold, "char_threshold": data.char_threshold,
            "record_floor": data.record_floor, "model_name": data.model, "error": None,
        }

        try:
            # ⚠ 这个返回值是**数据库行号**,不是图片 id —— 名字必须区分开。
            # 图片 id 是文件名主干(上面的 image_id),行号只在本进程内用来指行。
            row_id = db.insert_image(conn, row)
        except sqlite3.IntegrityError as e:
            # 只有 image_id 撞车才值得换个 id 重来。其他约束冲突(比如漏了
            # NOT NULL 列)重试多少次都一样,必须当场暴露 —— 否则会被重试
            # 循环吞掉,最后只报一句「文件名都被占用」,完全指错方向。
            if "image_id" not in str(e):
                raise
            continue

        staged = side = None
        try:
            staged, kind, side = archiver.stage_write(src, data, filename, not args.no_xmp)
            dst = archiver.place(staged, side, dest_dir, filename, meta["mtime_ns"])
        except FileExistsError:
            db.delete_image(conn, row_id)
            fsutil.discard_staged(staged, side)
            continue
        except Exception:
            db.delete_image(conn, row_id)
            fsutil.discard_staged(staged, side)
            raise

        lib_hash = readwrite.sha256_of(dst)
        rel_path = dst.relative_to(config.ROOT).as_posix()
        xmp_ok = 0 if kind == "sidecar" else 1
        size = dst.stat().st_size
        db.mark_stored(conn, row_id, rel_path, lib_hash, xmp_ok, size)
        db.insert_tags(conn, row_id, res["records"])
        fsutil.retry_os(src.unlink)
        emit.event("image_done", name=src.name, outcome="ok",
                   tags=len(data.passed_names()), rel_path=rel_path,
                   collection=coll_name)
        return "ok", journal.to_line(data, rel_path, size, lib_hash, xmp_ok)

    raise RuntimeError(f"连续 5 次生成的文件名都已被占用: {src.name}")


def process_collection(tagger, conn, node: scanner.CollNode, args, day: str,
                       stats: dict, failures: list[tuple[str, str]], emit,
                       coll_parts: tuple[str, ...] = ()) -> None:
    """处理一棵合集子树。**严格后序** —— 先递归子合集,再处理本层。

    父目录必须等它的全部子目录都消失之后才可能 rmdir 成功,所以子先父后。
    """
    own_parts = (*coll_parts, node.coll_id)
    parent_coll_id = coll_parts[-1] if coll_parts else None
    indent = "  " * (node.depth + 1)

    for child in node.children:
        process_collection(tagger, conn, child, args, day, stats, failures,
                           emit, own_parts)

    emit.event("collection_start", name=node.name, depth=node.depth,
               side=node.side, images=len(node.images),
               children=len(node.children), skipped=node.skipped_total)
    label = f"{'  ' * node.depth}{node.name}"
    emit.log(f"\n{indent}合集 {node.name}:{len(node.images)} 张图片,"
          f"子合集 {len(node.children)} 个,跳过 {node.skipped_total} 项")

    for name in node.skipped_dirs:
        logbook.record_warn("跳过", f"合集 {node.name!r} 的子目录 {name!r} 未处理"
                                    f"(超过深度上限或不可访问)")
    for name in node.skipped_files:
        logbook.record("跳过", f"合集 {node.name!r} 的非图片文件 {name!r} 未处理")
    if config.is_generic_collection_name(node.name):
        logbook.record_warn("提示", f"合集名 {node.name!r} 是通用默认名,"
                                    f"建议改成有意义的名称")

    # 纯结构性节点:本层没有图片、但有子合集。它仍然要有自己的合集行,
    # 否则子行的 parent_coll_id 会指向一个不存在的 id,树就断了。
    structural = not node.images and bool(node.children)
    if not node.images and not node.children:
        if args.dry_run:
            emit.log(f"{indent}[dry-run] 没有图片也没有子合集,将被移入 "
                  f"{config.FAILED_DIR.name}/")
            return
        logbook.record_warn("空合集", f"合集 {node.name!r} 里没有图片也没有子合集,"
                                      f"已移入 {config.FAILED_DIR.name}/")
        for name, dst in archiver.consume_tree(node):
            logbook.record("残留", f"合集 {name!r} 已移入 "
                                   f"{dst.relative_to(config.ROOT).as_posix()}")
        stats["skipped"] += 1
        return

    # 同名在新布局下**完全合法** —— 它们是不同 coll_id 的独立合集。这条提示
    # 是唯一能告诉用户「同一个 inbox 文件夹被重复投放了」的信号。
    existing = db.find_collection_by_name_key(conn, node.name.casefold())
    if existing:
        where = ", ".join(f"#{r['id']}" for r in existing)
        logbook.record_warn("同名合集", f"{node.name!r} 已有 {len(existing)} 个"
                                        f"(#{where});它们是不同合集,本次为"
                                        f"第 {len(existing) + 1} 次投放,来源 {node.path}")

    coll_dir_path = config.LIBRARY_DIR.joinpath(day, *own_parts)

    if args.dry_run:
        for img in node.images:
            try:
                process_one(tagger, conn, img, args, day, own_parts, None,
                                        node.name, emit)
                stats["dry"] += 1
            except Exception as e:
                stats["failed"] += 1
                failures.append((f"{node.name}/{img.name}", f"{type(e).__name__}: {e}"))
        emit.log(f"{indent}[dry-run] 目标目录 "
              f"{coll_dir_path.relative_to(config.ROOT).as_posix()}")
        return

    saved_at = datetime.now().strftime(config.TS_DB_FMT)
    cid = db.insert_collection(conn, {
        "name": node.name, "name_key": node.name.casefold(),
        "coll_id": node.coll_id, "side": node.side,
        "parent_coll_id": parent_coll_id, "depth": node.depth,
        "dir_rel_path": coll_dir_path.relative_to(config.ROOT).as_posix(),
        "date_dir": day, "saved_at": saved_at, "image_count": 0,
        "failed_count": 0, "skipped_files": len(node.skipped_files),
        "skipped_dirs": len(node.skipped_dirs),
        "gen_threshold": args.general_threshold,
        "char_threshold": args.character_threshold,
    })

    failed = 0
    lines: list[str] = []
    for i, img in enumerate(node.images, 1):
        img_label = f"{node.name}/{img.name}"
        try:
            outcome, line = process_one(tagger, conn, img, args, day, own_parts,
                                        cid, node.name, emit)
            stats[outcome] += 1
            note = {"ok": "打标", "dupe": "复用", "dry": "试跑"}[outcome]
            if line:
                lines.append(line)
        except (scanner.FileBusyError, scanner.NotAnImageError) as e:
            failed += 1
            note = "跳过"
            failures.append((img_label, str(e)))
            logbook.record("失败", f"{img_label}: {e}")
            try:
                archiver.quarantine(img, str(e))
            except OSError as qe:
                failures.append((img_label, f"移入 failed/ 也失败: {qe}"))
        except Exception as e:
            failed += 1
            note = "出错"
            failures.append((img_label, f"{type(e).__name__}: {e}"))
            logbook.record("失败", f"{img_label}: {type(e).__name__}: {e}")

        emit.log(f"{indent}  [{i}/{len(node.images)}] {note}  {img.name}")

    n = db.finalize_collection(conn, cid, failed,
                               len(node.skipped_files), len(node.skipped_dirs))
    stats["collections"] += 1
    try:
        archiver.write_collection_index(conn, cid, coll_dir_path)
    except (OSError, ValueError, UnicodeError) as e:
        logbook.record("告警", f"合集 {node.name!r} 的 index.json 写入失败: {e}")

    # journal 攒到合集处理完再一次性追加:逐张追加会有 N 次 fsync,
    # 而它只是加速层,批量写足够安全(丢了也能从 XMP 重建)。
    written = journal.append(coll_dir_path, lines, local_state.device_id())
    note = "(纯结构节点)" if structural else f"频次表记下前 {config.COLLECTION_TOP_N} 条 tag(分母 {n})"
    emit.log(f"{indent}  -> 归档 {n} 张 {note}")
    emit.event("collection_done", name=node.name, depth=node.depth,
               images=n, failed=failed, skipped=node.skipped_total,
               structural=structural)
    if written:
        emit.log(f"{indent}  -> journal({local_state.device_id()})追加 "
              f"{len(lines)} 条 / {written:,} 字节")
