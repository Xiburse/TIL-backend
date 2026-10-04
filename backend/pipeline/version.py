from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from backend import config
from backend.common import local_state, logbook
from backend.store import db

__all__ = ["read_version_files", "write_version_file", "check_version_file"]


def read_version_files() -> tuple[dict | None, list[Path]]:
    """读 library/version.json,返回 (内容, 冲突副本列表)。

    多端都会写这个文件,OneDrive 于是会产生 `version-DESKTOP-XXX.json` 这样的
    冲突副本 —— 那意味着数据分叉,**必须报出来**,静默忽略就等于「tag 悄悄变旧」。
    """
    main = config.LIBRARY_DIR / config.VERSION_NAME
    conflicts = []
    for f in sorted(config.LIBRARY_DIR.glob("version*.json")):
        if f.name != config.VERSION_NAME:
            conflicts.append(f)
    if not main.is_file():
        return None, conflicts
    try:
        return json.loads(main.read_text(encoding="utf-8")), conflicts
    except (OSError, ValueError):
        return None, conflicts


def write_version_file(digest: str, images: int, collections: int, tag_rows: int) -> bool:
    """写归档根的版本号文件。内容没变就不写(减少 OneDrive 冲突面)。"""
    config.LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    doc = {
        "v": config.FILE_FORMAT_VERSION,
        "rev": int(time.time() * 1000),          # 13 位毫秒时间戳
        "digest": digest,
        "device_id": local_state.device_id(),
        "updated_at": datetime.now().strftime(config.TS_DB_FMT),
        "schema_version": db.SCHEMA_VERSION,
        "image_count": images,
        "collection_count": collections,
        "tag_rows": tag_rows,
    }
    path = config.LIBRARY_DIR / config.VERSION_NAME
    if path.is_file():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            if old.get("digest") == digest and old.get("v") == config.FILE_FORMAT_VERSION:
                return False
        except (OSError, ValueError):
            pass
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def check_version_file(conn: sqlite3.Connection) -> None:
    """启动时比对版本号,提示索引是否过期。"""
    doc, conflicts = read_version_files()
    if conflicts:
        logbook.record_warn(
            "版本冲突",
            f"归档根目录有 {len(conflicts)} 个版本号冲突副本"
            f"({', '.join(f.name for f in conflicts)}),说明有另一台设备也在写。"
            f"请人工确认后删掉多余的。",
        )
    if doc is None:
        return
    if doc.get("v") != config.FILE_FORMAT_VERSION:
        # 读到不认识的格式版本:只读不重写。否则老设备会用老格式覆盖新设备
        # 写的文件 —— 这是「多端都写同一份文件」最真实的数据丢失路径。
        logbook.record_warn(
            "格式版本",
            f"library/{config.VERSION_NAME} 的格式版本是 {doc.get('v')},"
            f"本程序认识的是 {config.FILE_FORMAT_VERSION}。"
            f"本次将只读不写,请升级程序。",
        )
        return

    digest, images, _ = local_state.library_digest()
    if db.get_meta(conn, "digest") != digest:
        # 走 logbook 而不是 print —— IPC 模式下 stdout 是协议通道,
        # 混进一行中文会让前端的 JSON 解析直接崩。
        logbook.record_warn(
            "索引过期",
            f"library/ 的内容与本地索引不一致(磁盘 {images} 张),建议重建索引")
