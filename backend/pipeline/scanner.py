"""扫描 inbox、解析归档名、读取图片元数据、生成归档名。

**只读不写** —— 落盘的部分在 archiver。
"""

from __future__ import annotations

import os

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from backend import config


_ARCHIVE_NAME_RE = re.compile(r"^(\d{8}-\d{6})_(\d{8}-\d{6})_([0-9a-fA-F]{6,32})$")

# Windows 非法字符 + OneDrive 拒同步的名字


def parse_archive_name(filename: str) -> tuple[str, str, str] | None:
    """从归档名反解 (mtime, ctime, uuid),失败返回 None。

    重建索引时**必须用文件名而不是 os.stat**:文件名里的 mtime 是无时区的
    本地时间,跨时区设备 stat 出来会渲染成不同的字符串,和文件名、
    以及 query.py 里的 COALESCE(shot_at, mtime) 全部对不上。
    用户重命名过的文件不符合命名规则,返回 None 交由调用方容忍。
    """
    m = _ARCHIVE_NAME_RE.match(Path(filename).stem)
    if not m:
        return None
    mt, ct, uid = m.groups()
    return (
        datetime.strptime(mt, config.TS_NAME_FMT).strftime(config.TS_DB_FMT),
        datetime.strptime(ct, config.TS_NAME_FMT).strftime(config.TS_DB_FMT),
        uid.lower(),
    )


CHUNK = 1 << 20

# 归档路径的总长预算。根目录 + 日期 + 合集名 + {mtime}_{ctime}_{uuid12}.ext
# 加起来很容易变长,超过就截断合集名并附短 id。


PATH_BUDGET = 150

# Windows 文件属性位


_ATTR_HIDDEN = 0x2


_ATTR_SYSTEM = 0x4


_ATTR_REPARSE = 0x400


class FileBusyError(Exception):
    """文件在读取期间还在变化(用户还在往 inbox 里拖),本轮跳过。"""


class NotAnImageError(Exception):
    """不是可识别的图片:损坏、截断,或者根本不是图片。"""


@dataclass
class CollNode:
    """inbox 里的一个合集。**可以嵌套** —— 子目录就是子合集。"""

    name: str          # 文件夹原名(目录名以后会被换成 coll_id,所以这里要留住)
    path: Path         # 在 inbox 里的位置
    depth: int = 0
    side: int = 0      # 同级序号:0=根,1/2/3=父下的第 N 个
    images: list[Path] = field(default_factory=list)
    skipped_files: list[str] = field(default_factory=list)
    skipped_dirs: list[str] = field(default_factory=list)
    children: list["CollNode"] = field(default_factory=list)
    coll_id: str | None = None   # 扫描后统一分配

    @property
    def skipped_total(self) -> int:
        return len(self.skipped_files) + len(self.skipped_dirs)

    def walk(self):
        """先序遍历自己与全部后代。"""
        yield self
        for child in self.children:
            yield from child.walk()

    def image_total(self) -> int:
        """整棵子树的图片数。

        进度与「父层是否为空」的判据都必须用它 —— 父层可以合法地 0 张直接
        图片、只有子合集,按本层图片数判会把纯结构性的父层误判成空合集。
        """
        return sum(len(n.images) for n in self.walk())


def assign_coll_ids(nodes: list[CollNode], when: datetime) -> None:
    """给整棵树分配 coll_id(<入库时间>_<uuid12>)。

    必须在**归档之前**做完:子合集要把父的 coll_id 写进自己的 index.json,
    而处理顺序是子先父后,所以父的 id 得先存在。
    """
    stamp = when.strftime(config.TS_NAME_FMT)
    for node in nodes:
        node.coll_id = f"{stamp}_{uuid.uuid4().hex[:12]}"
        assign_coll_ids(node.children, when)


def scan_inbox(inbox: Path, exts: frozenset[str]) -> tuple[list[Path], list[CollNode]]:
    """扫描 inbox,返回 (散图, 根合集列表)。

    第一层的文件 = 散图;第一层的子目录 = 根合集;其下每个非隐藏子目录递归地
    是一个子合集。
    """
    loose: list[Path] = []
    collections: list[CollNode] = []

    if not inbox.is_dir():
        return loose, collections

    for entry in sorted(inbox.iterdir()):
        if _is_hidden(entry):  # 目录同样要判:否则 .git/、junction 会被当成合集
            continue
        if entry.is_dir():
            collections.append(_scan_collection(entry, exts, 0, 0))
        elif entry.is_file() and entry.suffix.lower() in exts:
            loose.append(entry)

    return loose, collections


def _scan_collection(path: Path, exts: frozenset[str], depth: int, side: int) -> CollNode:
    """递归枚举一个合集。

    **每一层都要挡 reparse point** —— junction 指回祖先就会无限递归。_is_hidden
    内部判了 _ATTR_REPARSE,但以前只在第一层用过。
    """
    node = CollNode(name=path.name, path=path, depth=depth, side=side)

    for entry in sorted(path.iterdir()):
        if entry.is_dir():
            if _is_hidden(entry):
                continue          # junction / 隐藏目录:静默跳过,它可能成环
            if depth + 1 > config.MAX_COLL_DEPTH:
                # 每层目录名约 19 字符,不设限迟早撞 Windows 的路径长度上限。
                # 记下来:它会让本层算「有残留」。
                node.skipped_dirs.append(entry.name)
                continue
            node.children.append(
                _scan_collection(entry, exts, depth + 1, len(node.children) + 1))
            continue
        if not entry.is_file():
            continue
        if entry.suffix.lower() in exts and not _is_hidden(entry):
            node.images.append(entry)
        elif not _ignorable(entry.name):
            node.skipped_files.append(entry.name)  # 系统垃圾不记,免得淹没真问题

    return node


def _ignorable(name: str) -> bool:
    """既不是图片、也**不该算「残留」**的文件。

    必须包含我们自己生成的 `_tags.json` / `index.json` —— 否则
    `consume_collection` 会认为这个合集没处理干净,把**整个目录**移进
    failed/。这是个会整批误伤的连锁反应。
    """
    return config.is_managed_file(name) or name.casefold() in config.IGNORED_NAMES


def _is_hidden(path: Path) -> bool:
    """判断隐藏 / 系统文件。

    Windows 上 startswith(".") 基本没用 —— desktop.ini、Thumbs.db 是
    「属性隐藏」而不是点前缀。扩展名白名单已经能挡住它们,这里是第二道防线。
    """
    if path.name.startswith("."):
        return True
    try:
        attrs = getattr(path.stat(), "st_file_attributes", 0)
    except OSError:
        return True
    # reparse point 可能是 junction,递归时会把目录绕成环
    return bool(attrs & (_ATTR_HIDDEN | _ATTR_SYSTEM | _ATTR_REPARSE))


def _created_ts(st: os.stat_result) -> float:
    """文件创建时间。

    ctime 这个名字在 POSIX 上指 inode 变更时间,容易误读;Windows 上
    st_ctime 恰好就是创建时间。优先用语义明确的 st_birthtime。
    """
    birth = getattr(st, "st_birthtime", None)
    return birth if birth is not None else st.st_ctime


def _read_shot_at(im: Image.Image) -> str | None:
    """从 EXIF 读拍摄时间,只入库供搜索(文件名仍按 mtime 命名)。"""
    try:
        exif = im.getexif()
        raw = exif.get(36867) or exif.get(306)  # DateTimeOriginal / DateTime
    except Exception:
        return None
    if not raw:
        return None
    try:  # EXIF 的格式是 "2024:05:01 13:45:00",与 ISO 不同
        return datetime.strptime(str(raw).strip(), "%Y:%m:%d %H:%M:%S").strftime(
            config.TS_DB_FMT
        )
    except ValueError:
        return None


def collect_meta(path: Path) -> dict:
    """采集文件元数据。

    所有文件句柄都必须在返回前关闭 —— Windows 和 POSIX 不同,只要有一个
    句柄没关,后面的 os.rename 就会以 WinError 32 失败。所以哈希读取和
    PIL 读取各用一个独立的 with 块,不交叉。
    """
    st_before = path.stat()

    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            digest.update(chunk)

    # 顺便拿到「文件还在被复制」的检测:拖大文件时它会先以完整大小出现,
    # 内容却还没写完。截断的 JPEG 有时能部分解码成功,于是就给一张残缺图
    # 打了 tag 并归档 —— 比对读取前后的 stat 就能挡住。
    st_after = path.stat()
    if st_before.st_size != st_after.st_size or st_before.st_mtime_ns != st_after.st_mtime_ns:
        raise FileBusyError(f"文件在读取期间发生变化,可能仍在复制: {path.name}")

    width = height = None
    shot_at = None
    try:
        with Image.open(path) as im:
            shot_at = _read_shot_at(im)
            im = ImageOps.exif_transpose(im) or im
            width, height = im.size
    except UnidentifiedImageError as e:
        raise NotAnImageError(f"无法识别的图片格式: {path.name}") from e
    except Image.DecompressionBombError as e:
        raise NotAnImageError(f"像素数超出 Pillow 安全上限: {path.name}") from e
    except OSError as e:
        raise NotAnImageError(f"图片损坏或截断: {path.name} ({e})") from e

    created = _created_ts(st_after)
    return {
        "size_bytes": st_after.st_size,
        "mtime_ns": st_after.st_mtime_ns,  # 归档后要设回,保住「文件名编码 mtime」这条不变量
        "mtime_dt": datetime.fromtimestamp(st_after.st_mtime),
        "ctime_dt": datetime.fromtimestamp(created),
        "width": width,
        "height": height,
        "shot_at": shot_at,
        "source_sha256": digest.hexdigest(),  # 写 XMP **之前**的指纹
        "ext": path.suffix.lower(),
    }


def new_short_id() -> str:
    return uuid.uuid4().hex[: config.SHORT_ID_LEN]


def image_id_of(filename: str) -> str:
    """图片 id = 文件名主干(不含扩展名)。

    这就是这张图在归档文件夹里的名字本身,也是它在 journal 和数据库里的身份。
    **直接取 stem,不去拼什么 short_id** —— 文件名才是权威,short_id 只是它的
    一个片段。用自增主键当身份是不可靠的:换个设备重建数据库,同一个合集/图片
    拿到的数字就变了。
    """
    return Path(filename).stem


def make_filename(meta: dict, short_id: str) -> str:
    """{mtime}_{ctime}_{short_id}{ext}

    mtime 在最前,所以按文件名排序天然等于按图片时间排序。
    """
    mt = meta["mtime_dt"].strftime(config.TS_NAME_FMT)
    ct = meta["ctime_dt"].strftime(config.TS_NAME_FMT)
    return f"{mt}_{ct}_{short_id}{meta['ext']}"
