
"""JPEG 段链:扫描、写入 XMP、结构校验。

**绝不按字节搜 marker** —— EXIF 缩略图本身就是一张完整 JPEG,ICC profile 里
也可能出现任意字节,搜 FFD8/FFDB 会命中段内部,产出「头部合法、图像数据被
劈开」的文件。必须按长度走链。

遇 SOS 即停止扫描:多扫描/渐进式 JPEG、出现在 SOS 之后的 DHT,全部落在被
原样复制的尾部里,不需要理解。
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.imgfmt.errors import XmpUnsupported, XmpVerifyError
from backend.imgfmt.splice import _splice


JPEG_XMP_NS = b"http://ns.adobe.com/xap/1.0/\x00"


JPEG_EXT_NS = b"http://ns.adobe.com/xmp/extension/\x00"


@dataclass
class _JpegSeg:
    marker: int
    start: int
    end: int
    is_xmp: bool = False
    is_ext: bool = False


def _scan_jpeg(d: bytes) -> list[_JpegSeg]:
    """按长度走链解析,直到第一个 SOS。

    绝不按字节搜 marker。遇到 SOS 就停 —— 多扫描/渐进式 JPEG、出现在 SOS
    之后的 DHT,全部落在被原样复制的尾部里,不需要理解。
    """
    if len(d) < 4 or d[0:2] != b"\xff\xd8":
        raise XmpUnsupported("不是 JPEG(缺 SOI)")

    segs: list[_JpegSeg] = []
    i = 2
    while True:
        if i >= len(d):
            raise XmpUnsupported("JPEG 里没找到 SOS")
        if d[i] != 0xFF:
            raise XmpUnsupported(f"offset {i} 处不是段标记(0x{d[i]:02x})")

        j = i
        while j < len(d) and d[j] == 0xFF:  # FF 填充字节
            j += 1
        if j >= len(d):
            raise XmpUnsupported("段标记后没有内容")
        marker = d[j]

        if marker in (0xDA, 0xD9):  # SOS / EOI:之后的字节全部原样复制
            segs.append(_JpegSeg(marker, i, len(d)))
            return segs
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:  # 无长度字段
            segs.append(_JpegSeg(marker, i, j + 1))
            i = j + 1
            continue

        if j + 2 >= len(d):
            raise XmpUnsupported("段长度字段越界")
        length = int.from_bytes(d[j + 1:j + 3], "big")
        if length < 2:
            raise XmpUnsupported(f"段长 {length} 非法")
        end = j + 1 + length
        if end > len(d):
            raise XmpUnsupported("段长越界")

        seg = _JpegSeg(marker, i, end)
        if marker == 0xE1:
            payload = d[j + 3:end]
            # EXIF 与 XMP 的 APP1 都是 FFE1,靠命名空间区分,绝不能认错
            seg.is_xmp = payload.startswith(JPEG_XMP_NS)
            seg.is_ext = payload.startswith(JPEG_EXT_NS)
        segs.append(seg)
        i = end


def _write_jpeg(d: bytes, packet: bytes) -> bytes:
    segs = _scan_jpeg(d)

    payload = JPEG_XMP_NS + packet
    if len(payload) + 2 > 0xFFFF:
        raise XmpUnsupported("XMP 超出 APP1 段上限")

    if any(s.is_ext for s in segs):
        # 只删主段会留下孤儿扩展段,读者会读到不一致的 XMP
        raise XmpUnsupported("文件带有 Extended XMP,无法安全替换")

    xmp = [s for s in segs if s.is_xmp]
    removals = [(s.start, s.end) for s in xmp]
    if xmp:
        insert_at = xmp[0].start  # 替换:在原位写入
    else:
        # 纯插入:放在所有前导 APPn 之后、DQT/SOF 之前
        insert_at = 2
        for s in segs:
            if 0xE0 <= s.marker <= 0xEF:
                insert_at = s.end
            else:
                break

    new = b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload
    out, kept, kept_at = _splice(d, insert_at, removals, new)
    _verify_jpeg(d, out, kept, kept_at, new)
    return out


def _verify_jpeg(src: bytes, out: bytes, kept: bytes, kept_at: int, new: bytes) -> None:
    # (a) 位置校验
    if out != kept[:kept_at] + new + kept[kept_at:]:
        raise XmpVerifyError("JPEG 拼接结果与预期不符")

    # (b) 结构等价:非 XMP 段必须逐一对应。
    #     这一段专门抓「误删 EXIF / 丢 ICC / 段错位」—— 光靠 (a) 抓不到,
    #     因为 (a) 只比较剩下的字节,不检查少了什么。
    def sig(data: bytes):
        return [(s.marker, s.is_xmp) for s in _scan_jpeg(data)]

    src_clean = [x for x in sig(src) if not x[1]]
    out_all = sig(out)
    out_clean = [x for x in out_all if not x[1]]
    if src_clean != out_clean:
        raise XmpVerifyError("JPEG 段结构发生了变化(EXIF/ICC 可能被破坏)")
    if sum(1 for x in out_all if x[1]) != 1:
        raise XmpVerifyError("JPEG 里的 XMP 段数量不为 1")

    # 熵编码数据必须逐字节相同
    if src[-2:] != out[-2:] or out[-2:] != b"\xff\xd9":
        raise XmpVerifyError("JPEG 结尾不是 EOI")


def _extract_jpeg(d: bytes) -> str | None:
    for seg in _scan_jpeg(d):
        if seg.is_xmp:
            return d[seg.start + 4 + len(JPEG_XMP_NS):seg.end].decode("utf-8", "replace")
    return None
