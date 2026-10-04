"""图片打标流水线入口。

inbox/ 里的**文件**是散图、**子目录**是合集(可嵌套)。tag 会被原生写进图片
文件本身(JPEG/PNG/WebP 的 XMP);装不了 XMP 的格式在旁边放 `_tags.json` 边车。
**图片自带的数据是真相源**,`tags.db` 只是可删可重建的本地索引。

用法(在项目根目录运行):
    python -m backend.main                  # 处理 inbox/
    python -m backend.main --dry-run        # 只推理并打印计划,不动文件不写库
    python -m backend.main --check          # 自检:GPU 加速 + XMP 段链往返
    python -m backend.main --reindex        # 从 library/ 重建本地索引(不需要模型)
    python -m backend.main --verify-library # 巡检 library 的结构完整性

配置见项目根目录的 config.toml。
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

from backend import config
from backend.config import EXIT_FATAL, EXIT_OK, EXIT_PARTIAL
from backend.common import local_state, logbook
from backend.pipeline import archiver, scanner
from backend.pipeline.migrate_layout import migrate_layout
from backend.pipeline.version import (
    check_version_file,
    read_version_files,
    write_version_file,
)
from backend.store import db, journal

# 以下模块**只在对应分支里导入**。重点不是省内存,而是 don't-load-the-model:
# `--reindex` 在没装 model.onnx 的新设备上也要能跑,而 ingest 那条链最终会
# 走到 onnxruntime。ingest 模块本身是安全的(build_tagger 里才导入 tagger),
# 但保持分支内导入能让"谁需要模型"这件事在代码里一眼可见。
#   --reindex        -> reindex
#   --verify-library -> verify
#   --check / 入库    -> ingest

def run_ingest(args, emit) -> dict:

    config.ensure_dirs()
    config.check_layout()

    conn = db.connect(config.DB_PATH)
    try:
        db.init_schema(conn)
        migrate_layout(conn)      # 旧布局 → coll_id 命名 + 嵌套,可重入

        fixed_imgs, fixed_colls = db.reconcile(conn)
        if fixed_imgs or fixed_colls:
            emit.log(f"自愈:修正了 {fixed_imgs} 条图片记录、{fixed_colls} 个合集记录")

        check_version_file(conn)

        loose, collections = scanner.scan_inbox(config.INBOX_DIR, config.IMAGE_EXTS)
        if args.limit:
            budget = args.limit
            loose = loose[:budget]
            budget -= len(loose)
            collections = collections[:budget] if budget > 0 else []

        if not loose and not collections:
            emit.log(f"{config.INBOX_DIR.name}/ 里没有待处理的图片。")
            return {"total": 0, "ok": 0, "dupe": 0, "failed": 0, "collections": 0}

        # coll_id 必须在归档**之前**分配:子合集要把父的 coll_id 写进自己的
        # index.json,而处理顺序是子先父后。
        scanner.assign_coll_ids(collections, datetime.now())

        # 入库路径:这里才导入 ingest。build_tagger 内部再导入 tagger
        # —— 那一行就是模型加载的开关,别为了"整洁"提到模块顶部。
        from backend.pipeline.ingest import build_tagger, process_collection, process_one

        tagger = build_tagger(args, emit)

        stats = {"ok": 0, "dupe": 0, "dry": 0, "failed": 0,
                 "skipped": 0, "collections": 0}
        failures: list[tuple[str, str]] = []
        # 计数必须用**整棵子树**的图片数,否则嵌套下进度与 ETA 都会失真
        total = len(loose) + sum(node.image_total() for node in collections)
        started = time.perf_counter()
        done = 0

        # 散图按日期聚合 journal 行 —— 每张散图各算各的 day,跨零点会分到两天
        loose_lines: dict[str, list[str]] = {}

        for src in loose:
            day = datetime.now().strftime(config.DATE_DIR_FMT)
            try:
                outcome, line = process_one(tagger, conn, src, args, day, emit=emit)
                stats[outcome] += 1
                note = {"ok": "打标", "dupe": "复用", "dry": "试跑"}[outcome]
                if line:
                    loose_lines.setdefault(day, []).append(line)
            except (scanner.FileBusyError, scanner.NotAnImageError) as e:
                stats["failed"] += 1
                failures.append((src.name, str(e)))
                note = "跳过"
                if not args.dry_run:
                    try:
                        archiver.quarantine(src, str(e))
                    except OSError as qe:
                        failures.append((src.name, f"移入 failed/ 也失败: {qe}"))
            except Exception as e:
                stats["failed"] += 1
                failures.append((src.name, f"{type(e).__name__}: {e}"))
                note = "出错"

            done += 1
            elapsed = time.perf_counter() - started
            rate = done / elapsed if elapsed > 0 else 0.0
            eta = (total - done) / rate if rate > 0 else 0.0
            emit.event("progress", done=done, total=total,
                       rate=round(rate, 1), eta_seconds=int(eta), name=src.name)

        for day, lines in loose_lines.items():
            journal.append(config.LIBRARY_DIR / day, lines, local_state.device_id())

        for node in collections:
            day = datetime.now().strftime(config.DATE_DIR_FMT)
            process_collection(tagger, conn, node, args, day, stats, failures, emit)
            done += node.image_total()
            if not args.dry_run:
                # 后序收尾:子目录先消失,父目录才可能 rmdir 成功
                for name, dst in archiver.consume_tree(node):
                    logbook.record("残留", f"合集 {name!r} 有未处理项,已移入 "
                                           f"{dst.relative_to(config.ROOT).as_posix()}")

        elapsed = time.perf_counter() - started
        parts = []
        if loose:
            parts.append(f"{len(loose)} 张散图")
        if collections:
            parts.append(f"{len(collections)} 个合集")
        summary = {
            "total": total, "ok": stats["ok"], "dupe": stats["dupe"],
            "failed": stats["failed"], "collections": stats["collections"],
            "skipped": stats["skipped"], "seconds": round(elapsed, 2),
            "avg_ms": round(tagger.average_ms(), 1),
        }
        emit.event("run_done", **summary)
        emit.log("")
        emit.log(f"共 {total} 张图片({' + '.join(parts) or '无'}):")
        emit.log(f"  {stats['ok']} 张新打标,{stats['dupe']} 张复用已有 tag,"
              f"{stats['failed']} 张失败;耗时 {elapsed:.1f} s")

        if not args.dry_run:
            digest, images, colls = local_state.library_digest()
            _, _, tag_rows = db.counts(conn)
            db.set_meta(conn, "digest", digest)
            db.set_meta(conn, "rev", str(int(time.time() * 1000)))
            if write_version_file(digest, images, colls, tag_rows):
                emit.log(f"  版本号已更新(library/{config.VERSION_NAME})")

        if args.no_xmp:
            emit.log("  ⚠ --no-xmp:tag 只写在 _tags.json 边车里,没有跟随图片本体。")
        if stats["ok"]:
            emit.log(f"平均推理 {tagger.average_ms():.0f} ms/张")
        warning = tagger.speed_warning()
        if warning:
            emit.log(f"⚠ {warning}")

        if failures:
            where = "" if args.dry_run else f"(已移入 {config.FAILED_DIR.name}/)"
            emit.log(f"\n处理失败{where}:")
            for name, why in failures:
                emit.log(f"  - {name}: {why}")
            return {**summary, "failures": [{"name": n, "reason": w} for n, w in failures],
                    "exit_code": EXIT_PARTIAL}
        return {**summary, "failures": [], "exit_code": EXIT_OK}
    finally:
        db.close(conn)

