
"""PNG chunk:扫描、写入 XMP、CRC 校验。

iTXt 的字节布局差一个 NUL 就是坏文件:keyword 之后是**连续 5 个 0x00**
(终止符 + flag + method + 空 language 终止符 + 空 translated 终止符)再跟文本。
"""

from __future__ import annotations

import zlib

from backend.imgfmt.errors import XmpUnsupported, XmpVerifyError
from backend.imgfmt.splice import _splice


PNG_XMP_KEYWORD = b"XML:com.adobe.xmp\x00"


def _png_chunks(d: bytes) -> list[tuple[bytes, int, int, int]]:
    """返回 [(type, start, end, data_len)]。"""
    if d[:8] != b"\x89PNG\r\n\x1a\n":
        raise XmpUnsupported("不是 PNG")

    chunks = []
    i = 8
    while i + 8 <= len(d):
        ln = int.from_bytes(d[i:i + 4], "big")
        ctype = d[i + 4:i + 8]
        end = i + 12 + ln
        if end > len(d):
            raise XmpUnsupported("PNG chunk 越界")
        chunks.append((ctype, i, end, ln))
        if ctype == b"IEND":
            return chunks
        i = end
    raise XmpUnsupported("PNG 里没找到 IEND")


def _png_chunk(ctype: bytes, data: bytes) -> bytes:
    body = ctype + data
    return len(data).to_bytes(4, "big") + body + zlib.crc32(body).to_bytes(4, "big")


def _png_xmp_keyword(data: bytes) -> bool:
    """标准 iTXt 与 ImageMagick 的 Raw profile type xmp。

    两种必须一起删干净,否则两份 XMP 并存、不同工具读不同那份,用户会看到
    「tag 自己变回去了」。
    """
    return data.startswith(PNG_XMP_KEYWORD) or data.startswith(b"Raw profile type xmp")


def _png_itxt(packet: bytes) -> bytes:
    # keyword + NUL + flag(0) + method(0) + 空 language 的 NUL
    # + 空 translated keyword 的 NUL + 文本
    # 即 keyword 之后是**连续 5 个 0x00** —— 少一个就是坏文件
    return b"XML:com.adobe.xmp\x00" + b"\x00\x00\x00\x00" + packet


def _write_png(d: bytes, packet: bytes) -> bytes:
    chunks = _png_chunks(d)
    if chunks[0][0] != b"IHDR":
        raise XmpUnsupported("IHDR 不是第一个 chunk")

    removals = []
    for ctype, start, end, ln in chunks:
        if ctype in (b"iTXt", b"tEXt", b"zTXt"):
            if _png_xmp_keyword(d[start + 8:start + 8 + ln]):
                removals.append((start, end))

    # IEND 永远是最后一个 chunk,插在它前面就不会落进 IDAT 序列中间
    insert_at = chunks[-1][1]
    new = _png_chunk(b"iTXt", _png_itxt(packet))

    out, kept, kept_at = _splice(d, insert_at, removals, new)
    _verify_png(d, out, kept, kept_at, new)
    return out


def _verify_png(src: bytes, out: bytes, kept: bytes, kept_at: int, new: bytes) -> None:
    if out != kept[:kept_at] + new + kept[kept_at:]:
        raise XmpVerifyError("PNG 拼接结果与预期不符")

    # 自己走一遍 chunk 并校验每个 CRC32 —— 不依赖 Pillow
    for ctype, start, end, ln in _png_chunks(out):
        expect = int.from_bytes(out[end - 4:end], "big")
        if zlib.crc32(out[start + 4:end - 4]) != expect:
            raise XmpVerifyError(f"PNG chunk {ctype!r} 的 CRC32 校验失败")

    def sig(data: bytes):
        return [(c[0], _png_xmp_keyword(data[c[1] + 8:c[1] + 8 + c[3]])
                 if c[0] in (b"iTXt", b"tEXt", b"zTXt") else False)
                for c in _png_chunks(data)]

    src_clean = [x for x in sig(src) if not x[1]]
    out_all = sig(out)
    if src_clean != [x for x in out_all if not x[1]]:
        raise XmpVerifyError("PNG chunk 结构发生了变化")
    if sum(1 for x in out_all if x[1]) != 1:
        raise XmpVerifyError("PNG 里的 XMP chunk 数量不为 1")


def _extract_png(d: bytes) -> str | None:
    for ctype, start, end, ln in _png_chunks(d):
        if ctype in (b"iTXt", b"tEXt"):
            data = d[start + 8:start + 8 + ln]
            if data.startswith(PNG_XMP_KEYWORD):
                # keyword + 5 个 NUL 之后才是文本
                return data[len(PNG_XMP_KEYWORD) + 4:].decode("utf-8", "replace")
            if data.startswith(b"Raw profile type xmp"):
                return None  # ImageMagick 的十六进制变体:我们只负责删干净
    return None
