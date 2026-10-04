from __future__ import annotations

import argparse

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


def run_verify_library(args, emit) -> dict:
    """巡检 library 的结构完整性。同步刚结束、或怀疑 OneDrive 出问题时跑。"""
    from PIL import Image

    bad = 0
    total = 0
    for f in sorted(config.LIBRARY_DIR.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in config.IMAGE_EXTS:
            continue
        total += 1
        rel = f.relative_to(config.ROOT).as_posix()
        if fsutil._is_placeholder(f):
            emit.log(f"  占位符(仅在线,未下载): {rel}")
            continue
        try:
            with Image.open(f) as im:
                im.load()   # 真解码 —— 只 open 不 load 的话 JPEG 连熵数据都不碰
        except Exception as e:
            bad += 1
            emit.log(f"  损坏: {rel} — {type(e).__name__}: {e}")
            continue
        if not readwrite.read(f)[0] and not readwrite.sidecar_path(f).is_file():
            bad += 1
            emit.log(f"  无 tag: {rel}")

    emit.log(f"\n巡检 {total} 个文件,{bad} 个有问题。")
    return {"bad": bad, "checked": total}
