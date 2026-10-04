
"""WebP RIFF 容器:扫描、写入 XMP、结构校验。

**只在已存在 VP8X 时才嵌入**,绝不合成 —— 那要改 RIFF size、分别解析
VP8/VP8L 两套宽高、flags 位只能 OR 不能覆盖(清掉 animation 位会让动图变
静图)、奇数 chunk 补齐还要计入 size,漏一处后面全部错位。收益不值这个风险。
"""

from __future__ import annotations

from backend.imgfmt.errors import XmpUnsupported, XmpVerifyError
from backend.imgfmt.splice import _splice


def _webp_chunks(d: bytes) -> list[tuple[bytes, int, int]]:
    """返回 [(fourcc, start, padded_end)]。"""
    if d[:4] != b"RIFF" or d[8:12] != b"WEBP":
        raise XmpUnsupported("不是 WebP")

    chunks = []
    i = 12
    while i + 8 <= len(d):
        fourcc = d[i:i + 4]
        ln = int.from_bytes(d[i + 4:i + 8], "little")
        end = i + 8 + ln
        padded = end + (ln & 1)  # chunk 必须偶数对齐,填充字节计入 RIFF size
        if padded > len(d):
            raise XmpUnsupported("WebP chunk 越界")
        chunks.append((fourcc, i, padded))
        i = padded
    return chunks


def _write_webp(d: bytes, packet: bytes) -> bytes:
    chunks = _webp_chunks(d)
    if not any(c[0] == b"VP8X" for c in chunks):
        # 合成 VP8X 要改 RIFF size、分别解析 VP8/VP8L 两套宽高、flags 位只能
        # OR 不能覆盖(清掉 animation 位会让动图变静图)、奇数 chunk 补齐还要
        # 计入 size —— 漏一处后面全部 chunk 错位。风险远高于收益,退边车。
        raise XmpUnsupported("简单 WebP(无 VP8X),不合成 VP8X")

    removals = [(s, e) for f, s, e in chunks if f == b"XMP "]
    # 追加到最后一个 chunk 之后:WebP 规范建议的顺序是
    # VP8X → [ICCP] → 图像数据 → [EXIF] → [XMP],插在 EXIF 前面虽然也能被读出来,
    # 但不符合规范建议的顺序。
    insert_at = chunks[-1][2]
    body = packet + (b"\x00" if len(packet) & 1 else b"")
    new = b"XMP " + len(packet).to_bytes(4, "little") + body

    spliced, kept, kept_at = _splice(d, insert_at, removals, new)
    _verify_webp(d, spliced, kept, kept_at, new)

    # RIFF size = 总长 - 8。这一步会改动前 12 字节里的 4 字节,
    # 所以放在位置校验之后。
    out = bytearray(spliced)
    out[4:8] = (len(out) - 8).to_bytes(4, "little")
    return bytes(out)


def _verify_webp(src: bytes, spliced: bytes, kept: bytes, kept_at: int, new: bytes) -> None:
    if spliced != kept[:kept_at] + new + kept[kept_at:]:
        raise XmpVerifyError("WebP 拼接结果与预期不符")

    def sig(data: bytes):
        return [(c[0], c[0] == b"XMP ") for c in _webp_chunks(data)]

    src_clean = [x for x in sig(src) if not x[1]]
    out_all = sig(spliced)
    if src_clean != [x for x in out_all if not x[1]]:
        raise XmpVerifyError("WebP chunk 结构发生了变化")
    if sum(1 for x in out_all if x[1]) != 1:
        raise XmpVerifyError("WebP 里的 XMP chunk 数量不为 1")


def _extract_webp(d: bytes) -> str | None:
    for fourcc, start, _end in _webp_chunks(d):
        if fourcc == b"XMP ":
            ln = int.from_bytes(d[start + 4:start + 8], "little")
            return d[start + 8:start + 8 + ln].decode("utf-8", "replace")
    return None
