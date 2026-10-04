
"""对外接口:读 tag、把 tag 写进图片、边车文件。

**源文件全程不被改动** —— 写出并通过校验后才由调用方删除。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from backend import config
from backend.imgfmt import jpeg, png, webp
from backend.imgfmt.errors import XmpError
from backend.imgfmt.packet import ImageTags, build_packet, parse_packet, user_tags_of


_EXTRACTORS = {".jpg": jpeg._extract_jpeg, ".jpeg": jpeg._extract_jpeg,
               ".png": png._extract_png, ".webp": webp._extract_webp}


_WRITERS = {".jpg": jpeg._write_jpeg, ".jpeg": jpeg._write_jpeg,
            ".png": png._write_png, ".webp": webp._write_webp}


def read_from_bytes(blob: bytes, ext: str) -> tuple[list[str], ImageTags | None]:
    """读出 (dc:subject, 我们上次写的数据)。读不到或格式不认识就返回 ([], None)。"""
    extractor = _EXTRACTORS.get(ext.lower())
    if extractor is None:
        return [], None
    try:
        text = extractor(blob)
    except XmpError:
        return [], None
    if not text:
        return [], None
    return parse_packet(text)


def read(path: Path) -> tuple[list[str], ImageTags | None]:
    path = Path(path)
    try:
        return read_from_bytes(path.read_bytes(), path.suffix)
    except OSError:
        return [], None


def sidecar_path(image_path: Path) -> Path:
    """xxx.gif -> xxx_tags.json"""
    image_path = Path(image_path)
    return image_path.with_name(image_path.stem + config.SIDECAR_SUFFIX)


def write_sidecar(image_path: Path, data: ImageTags) -> Path:
    side = sidecar_path(image_path)
    side.write_text(data.to_json(), encoding="utf-8")
    return side


def read_sidecar(image_path: Path) -> ImageTags | None:
    side = sidecar_path(image_path)
    if not side.is_file():
        return None
    try:
        return ImageTags.from_json(side.read_text(encoding="utf-8"))
    except OSError:
        return None


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _emit(dst: Path, blob: bytes) -> None:
    """落盘并 fsync。

    必须先 fsync 再校验:不 fsync 的话磁盘可能写短了,而内存里的校验照样通过,
    于是把一个截断的文件放进 library。
    """
    with open(dst, "wb") as f:
        f.write(blob)
        f.flush()
        os.fsync(f.fileno())


def embed(src: Path, dst: Path, data: ImageTags) -> str:
    """把 data 写进 src 的内容并落盘到 dst。返回载体类型 'xmp' | 'sidecar'。

    **源文件不会被改动** —— 写出并通过校验后,由调用方自行删除源文件。
    这是个重要的安全性质:嵌入失败或校验不通过时,用户的原件毫发无损。

    嵌入不可行时自动退到 `_tags.json` 边车,**绝不截断、绝不把可疑文件放出去**。
    边车场景下调用方必须**先 rename 边车、再 rename 图片**(两次独立 rename
    不可能原子;反过来会留下一张永远没 tag 的图,而 DB 认为它有)。
    """
    src, dst = Path(src), Path(dst)
    ext = src.suffix.lower()
    raw = src.read_bytes()

    if ext in config.EMBEDDABLE_EXTS and ext in _WRITERS:
        try:
            subjects, previous = read_from_bytes(raw, ext)
            packet = build_packet(data, user_tags_of(subjects, previous))
            _emit(dst, _WRITERS[ext](raw, packet))
            return "xmp"
        except (XmpError, OSError):
            pass  # 落到下面走边车

    _emit(dst, raw)
    write_sidecar(dst, data)
    return "sidecar"
