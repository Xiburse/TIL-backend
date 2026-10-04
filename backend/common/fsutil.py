"""文件系统小工具:重试与清理。"""

from __future__ import annotations

import os
import time
from pathlib import Path


def retry_os(fn, attempts: int = 3, delay: float = 0.2):
    """对 Windows 上短暂的共享冲突做有界重试。

    OneDrive 上传与 Defender 扫描都会短暂持有同步区文件的句柄,此时
    rename / 删除会抛 WinError 32。这类失败是暂时的,重试即可。
    """
    last = None
    for i in range(attempts):
        try:
            return fn()
        except OSError as e:
            if getattr(e, "winerror", None) != 32:
                raise
            last = e
            time.sleep(delay * (i + 1))
    raise last  # type: ignore[misc]


def discard_staged(*paths: Path | None) -> None:
    """清理 staging 残留(崩溃后 pending 行会被 reconcile 删掉,这些文件会永远留着)。"""
    for p in paths:
        if p is not None:
            try:
                Path(p).unlink(missing_ok=True)
            except OSError:
                pass


def _is_placeholder(path: Path) -> bool:
    """OneDrive 的「仅在线」占位符。

    读它会触发整文件水合 —— 一次全量重建等于把整个图库下载一遍。
    """
    try:
        attrs = getattr(path.stat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attrs & 0x400000) or bool(attrs & 0x1000)  # RECALL_ON_DATA_ACCESS | OFFLINE
