
"""XMP 包的数据载体与构造/解析。

含 ImageTags(写进图片的全部数据)、包构造、容错解析,以及字符串安全处理。
**不碰任何具体的图片格式** —— 格式在 jpeg/png/webp。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

from backend import config


@dataclass
class ImageTags:
    """要写进图片的全部数据。既是「本次结果」也是「上次写了什么」的记录。"""

    tags: list[tuple[str, int, float, bool]] = field(default_factory=list)
    rating: str | None = None
    rating_score: float | None = None
    model: str | None = None
    gen_threshold: float | None = None
    char_threshold: float | None = None
    record_floor: float | None = None
    top_n: int | None = None
    source_sha256: str | None = None
    origin_name: str | None = None
    saved_at: str | None = None
    mtime: str | None = None
    ctime: str | None = None
    shot_at: str | None = None
    width: int | None = None
    height: int | None = None
    # 图片 id = 归档文件名的主干(不含扩展名),也就是这张图在磁盘上的名字
    # 本身。它同时是 journal 和数据库里的身份 —— 不用自增主键那种「换个设备
    # 重建就变了」的东西。
    image_id: str | None = None
    # 所属合集。写进图片是为了保住「图片是真相源」这条不变量 —— 合集目录名
    # 现在是不可读的 coll_id,原名只剩 index.json 一份,而 index.json 只是
    # 派生缓存。带上这两个字段,名字和归属就能从图片本身重建。
    coll_id: str | None = None
    coll_name: str | None = None

    def to_json(self) -> str:
        """紧凑单行 JSON。

        ensure_ascii=True 让非 ASCII 走 \\uXXXX —— 既避免编码意外,也让
        packet 的字节数可预测(APP1 有硬上限)。
        """
        doc = {
            "v": config.FILE_FORMAT_VERSION,
            "image_id": self.image_id,
            "model": self.model,
            "gen_th": self.gen_threshold,
            "char_th": self.char_threshold,
            "floor": self.record_floor,
            "top_n": self.top_n,
            "rating": self.rating,
            "rating_score": self.rating_score,
            "origin_name": self.origin_name,
            "saved_at": self.saved_at,
            "mtime": self.mtime,
            "ctime": self.ctime,
            "shot_at": self.shot_at,
            "width": self.width,
            "height": self.height,
            "coll_id": self.coll_id,
            "coll_name": self.coll_name,
            "source_sha256": self.source_sha256,
            # confidence 保留 4 位小数:够用,且显著压小 packet
            "tags": [[t[0], int(t[1]), round(float(t[2]), 4), int(bool(t[3]))] for t in self.tags],
        }
        return json.dumps(doc, ensure_ascii=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, blob: str) -> "ImageTags | None":
        try:
            d = json.loads(blob)
        except (ValueError, TypeError):
            return None
        if not isinstance(d, dict) or d.get("v") != config.FILE_FORMAT_VERSION:
            return None  # 不认识的格式版本:只读不写,交由调用方告警

        tags = []
        for item in d.get("tags") or []:
            try:
                tags.append((str(item[0]), int(item[1]), float(item[2]), bool(item[3])))
            except (IndexError, TypeError, ValueError):
                continue
        return cls(
            tags=tags,
            rating=d.get("rating"), rating_score=d.get("rating_score"),
            model=d.get("model"), gen_threshold=d.get("gen_th"),
            char_threshold=d.get("char_th"), record_floor=d.get("floor"),
            top_n=d.get("top_n"), source_sha256=d.get("source_sha256"),
            origin_name=d.get("origin_name"), saved_at=d.get("saved_at"),
            mtime=d.get("mtime"), ctime=d.get("ctime"), shot_at=d.get("shot_at"),
            width=d.get("width"), height=d.get("height"),
            # 老图片里写的是 "uuid",但那个值是 short_id(文件名第三段),
            # **不是**图片 id(文件名主干)。两者语义不同,不能拿来当身份 ——
            # 宁可留 None,让调用方从文件名推。文件名才是权威。
            image_id=d.get("image_id"),
            coll_id=d.get("coll_id"), coll_name=d.get("coll_name"),
        )

    def passed_names(self) -> list[str]:
        """过阈值的 tag 名。只有这些进 dc:subject —— 否则用户在 Bridge /
        Lightroom 里的关键词列表会被每图上百个没过阈值的噪声词淹没。"""
        return [t[0] for t in self.tags if t[3]]

    def all_names(self) -> set[str]:
        return {t[0] for t in self.tags}

    def prompt(self) -> str:
        return ", ".join(n.replace("_", " ") for n in self.passed_names())


def clean_text(text: str) -> str:
    """剔除 XML 1.0 里无论怎么转义都非法的码位,以及未配对代理项。

    NTFS 允许文件名含未配对代理项,直接 UTF-8 编码会抛 UnicodeEncodeError
    把整轮处理打断 —— 和 logbook 里遇到的是同一类问题。
    """
    out = []
    for ch in text:
        o = ord(ch)
        if o < 0x20 and ch not in "\t\n\r":
            continue
        if 0xD800 <= o <= 0xDFFF or o in (0xFFFE, 0xFFFF):
            continue
        out.append(ch)
    return "".join(out)


def _escape(text: str) -> str:
    return (
        clean_text(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _merge_subjects(user_tags: list[str], ours: list[str]) -> list[str]:
    """用户条目 + 我们的 tag,大小写不敏感去重,保留首次出现的拼写。"""
    seen: set[str] = set()
    out: list[str] = []
    for tag in [*user_tags, *ours]:
        key = tag.casefold()
        if key and key not in seen:
            seen.add(key)
            out.append(tag)
    return out


def user_tags_of(existing_subject: list[str], previous: ImageTags | None) -> list[str]:
    """从现有 dc:subject 里减去我们上次写的,得到用户自己的条目。

    没有这一步,朴素的「读-合并-写」只增不减:我们写进去的 tag 下一轮会被
    当成用户条目原样合并回来,**用户删掉的 tag 会被重新加回,模型升级后
    失效的 tag 永远删不掉**。
    """
    if previous is None:
        return list(existing_subject)
    ours = {t.casefold() for t in previous.all_names()}
    return [t for t in existing_subject if t.casefold() not in ours]


def build_packet(data: ImageTags, user_tags: list[str]) -> bytes:
    subjects = _merge_subjects(user_tags, data.passed_names())
    items = "".join(f"<rdf:li>{_escape(t)}</rdf:li>" for t in subjects)

    desc = (
        f'<rdf:Description rdf:about=""'
        f' xmlns:dc="{config.DC_NS}"'
        f' xmlns:xmp="{config.XMP_NS}"'
        f' xmlns:ishelf="{config.ISHELF_NS}">'
        f"<dc:subject><rdf:Bag>{items}</rdf:Bag></dc:subject>"
        f"<xmp:CreatorTool>{config.CREATOR_TOOL}</xmp:CreatorTool>"
        f"<ishelf:data>{_escape(data.to_json())}</ishelf:data>"
        f"</rdf:Description>"
    )
    # padding 让别的工具能原地改而不重写整个文件(重写会在 OneDrive 产生冲突副本)
    # begin 属性的值是 U+FEFF(BOM),end="w" 表示这个包是可写的
    packet = (
        '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
        f'<rdf:RDF xmlns:rdf="{config.RDF_NS}">{desc}</rdf:RDF>\n'
        "</x:xmpmeta>\n"
        + " " * config.XMP_PADDING + "\n"
        '<?xpacket end="w"?>'
    )
    return packet.encode("utf-8", "replace")


def parse_packet(text: str) -> tuple[list[str], ImageTags | None]:
    """从 XMP 包里取出 (dc:subject 列表, 我们上次写的数据)。

    必须容忍别的工具写过的形态:多个并列的 rdf:Description(每个命名空间
    一个,只取第一个会丢数据)、属性式的 dc:subject、rdf:Seq/Alt 替代 Bag、
    任意命名空间前缀。
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return [], None

    subjects: list[str] = []
    previous: ImageTags | None = None

    for desc in root.iter(f"{{{config.RDF_NS}}}Description"):
        node = desc.find(f"{{{config.DC_NS}}}subject")
        if node is not None:
            for li in node.iter(f"{{{config.RDF_NS}}}li"):
                if li.text:
                    subjects.append(li.text.strip())
        attr = desc.get(f"{{{config.DC_NS}}}subject")
        if attr:
            subjects.extend(attr.split())

        blob = desc.find(f"{{{config.ISHELF_NS}}}data")
        if blob is not None and blob.text:
            previous = ImageTags.from_json(blob.text)

    return [s for s in subjects if s], previous
