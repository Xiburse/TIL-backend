"""本地状态:设备标识、图库内容摘要。

放在 common 而不是 pipeline —— 它是**本地状态**而非流水线逻辑,
journal 那边将来也要用它,放 pipeline 会形成反向依赖。
"""

from __future__ import annotations

import hashlib
import re
import socket
import uuid
from pathlib import Path

from backend import config


_DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _device_tag() -> str:
    """主机名不一定能当文件名用(可能含空格、冒号、中文),先洗一遍。"""
    host = "".join(c if (c.isalnum() or c in "-_") else "_" for c in socket.gethostname())
    return f"{host or 'device'}-{uuid.uuid4().hex[:8]}"


_DEVICE_CACHE: str | None = None


def device_id() -> str:
    """本机标识。**必须存同步区之外**,否则多端会共用同一个 id。

    它会被拼进 journal 的文件名,所以必须只含文件名安全字符。
    结果缓存在进程内 —— 它被每个文件夹的写入调用一次,不该反复读盘。
    """
    global _DEVICE_CACHE
    if _DEVICE_CACHE:
        return _DEVICE_CACHE
    try:
        if config.DEVICE_ID_PATH.is_file():
            value = config.DEVICE_ID_PATH.read_text(encoding="utf-8").strip()
            if value:
                cleaned = _sanitize_tag(value)
                if cleaned != value:   # 老 id 含非法字符:洗一遍覆盖,保证文件名可用
                    config.DEVICE_ID_PATH.write_text(cleaned, encoding="utf-8")
                _DEVICE_CACHE = cleaned
                return cleaned
    except OSError:
        pass

    value = _device_tag()
    try:
        config.DEVICE_ID_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.DEVICE_ID_PATH.write_text(value, encoding="utf-8")
    except OSError:
        pass
    _DEVICE_CACHE = value
    return value


def _sanitize_tag(value: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in value)


def library_digest() -> tuple[str, int, int]:
    """图书馆内容摘要 + (图片数, 合集数)。

    摘要用 `(相对路径, 大小, mtime_ns)` 三元组算 —— 都只需要 stat,很便宜,
    但任何外部改动(哪怕只改 tag)都会改动 size 或 mtime,所以能可靠反映
    「我的索引是不是过期了」。

    只用图片数量当信号是不行的:删图会让数量下降(取 max 反而把过期的
    当成最新)、而只改 tag 不改数量则完全无感。
    """
    h = hashlib.sha256()
    images = collections = 0
    if not config.LIBRARY_DIR.is_dir():
        return h.hexdigest(), 0, 0

    def feed(f: Path) -> None:
        nonlocal images
        st = f.stat()
        rel = f.relative_to(config.ROOT).as_posix()
        h.update(f"{rel}|{st.st_size}|{st.st_mtime_ns}\n".encode("utf-8", "replace"))
        images += 1

    def walk(folder: Path) -> None:
        """递归。**必须递归** —— 只走两层的话,嵌套子合集里的增删改对 digest
        完全隐形,另一台设备就会打印「内容一致」而索引其实已经过期。"""
        nonlocal collections
        for entry in sorted(folder.iterdir()):
            try:
                if entry.is_dir():
                    # 用 coll_id 正则判定,而不是「任意子目录都算一个合集」——
                    # 后者会把 OneDrive 的垃圾目录也算进去
                    if config.COLL_DIR_RE.match(entry.name):
                        collections += 1
                        walk(entry)
                elif entry.is_file() and entry.suffix.lower() in config.IMAGE_EXTS:
                    feed(entry)
            except OSError:
                continue  # OneDrive 占位符可能 stat 失败

    for day in sorted(config.LIBRARY_DIR.iterdir()):
        # 必须是 YYYY-MM-DD 才算日期目录 —— 否则 library/versions/ 之类
        # 也会被当成一个日期目录
        if day.is_dir() and _DATE_DIR_RE.match(day.name):
            walk(day)

    return h.hexdigest(), images, collections
