"""落盘:暂存、原子放入 library、隔离、合集收尾。"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path

from backend.imgfmt import packet
from backend.imgfmt import readwrite
from backend import config
from backend.common.fsutil import retry_os
from backend.store import db
from backend.pipeline.scanner import CollNode


def stage_write(src: Path, data: packet.ImageTags, filename: str, allow_embed: bool = True):
    """把 src 的内容(带 tag)写到 staging。

    返回 (staged 图片路径, 载体类型, staged 边车路径或 None)。
    **源文件全程不被改动** —— 这是嵌入失败时用户原件毫发无损的保证。
    """
    config.STAGING_DIR.mkdir(parents=True, exist_ok=True)
    staged = config.STAGING_DIR / filename

    if not allow_embed:
        shutil.copyfile(src, staged)
        readwrite.write_sidecar(staged, data)
        return staged, "sidecar", readwrite.sidecar_path(staged)

    kind = readwrite.embed(src, staged, data)
    side = readwrite.sidecar_path(staged) if kind == "sidecar" else None
    return staged, kind, side


def place(staged: Path, side: Path | None, dest_dir: Path, filename: str,
          mtime_ns: int) -> Path:
    """把 staging 里的产物放进 library,返回最终路径。

    **边车场景必须先放边车、再放图片。** 图片与边车是两次独立的 rename,
    不可能原子;反过来先放图片的话,边车失败就会留下一张永远没 tag 的图,
    而数据库认为它有 tag —— 正是这套架构要消灭的漂移。

    mtime 必须显式设回:新写的文件默认是当下时刻,不设的话文件名第一段
    (承诺的 mtime)与真实 mtime 不一致,用户文件管理器里所有图片的时间
    都会变成归档时刻。
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    dst = dest_dir / filename
    os.utime(staged, ns=(mtime_ns, mtime_ns))

    side_dst = None
    if side is not None:
        side_dst = dest_dir / side.name
        retry_os(lambda: os.rename(side, side_dst))

    try:
        retry_os(lambda: os.rename(staged, dst))
    except FileExistsError:
        if side_dst is not None:
            side_dst.unlink(missing_ok=True)
        raise
    return dst


def consume_tree(node: CollNode) -> list[tuple[str, Path]]:
    """后序收尾一棵合集树,返回 [(合集名, 被移进 failed/ 的路径)]。

    不收的话,图片归档后只剩空壳目录,而「同名再投放 = 新合集」—— 下一轮它
    又被识别成新合集,凭空多一条 image_count=0 的记录和一条冲突日志。

    **严格后序**:父目录只有在其全部子目录都消失之后才可能 rmdir 成功。
    **每层独立收尾**:深层有残留只移那一层,祖先层和已归档的图片不受影响 ——
    若整棵一起搬,深层一个 junction 就能把整条祖先链拖走。
    """
    moved: list[tuple[str, Path]] = []
    for child in node.children:
        moved.extend(consume_tree(child))

    try:
        node.path.rmdir()      # 本层已经空了
        return moved
    except OSError:
        pass                   # 还有残留:非图片文件 / 超深的子目录 / junction

    config.FAILED_DIR.mkdir(parents=True, exist_ok=True)
    # 目标名带短 uuid:多层可能出现同名目录,用 __2 退避会产出无法归属的目录
    dst = config.FAILED_DIR / f"{node.name}_{uuid.uuid4().hex[:6]}"
    try:
        os.rename(node.path, dst)
    except OSError:
        shutil.move(str(node.path), str(dst))
    moved.append((node.name, dst))
    return moved


def quarantine(src: Path, reason: str) -> Path:
    """把处理不了的文件挪进 failed/,免得它每一轮都被重新扫到。

    否则一张损坏的图会永远留在 inbox:每轮重新推理、每轮让退出码非零,
    挂到计划任务上就是个永久红灯。
    """
    config.FAILED_DIR.mkdir(parents=True, exist_ok=True)
    dst = config.FAILED_DIR / src.name
    if dst.exists():
        dst = config.FAILED_DIR / f"{src.stem}_{uuid.uuid4().hex[:8]}{src.suffix}"

    try:
        os.rename(src, dst)
    except OSError:
        shutil.move(str(src), str(dst))

    return dst


def write_collection_index(conn, collection_id: int, col_dir: Path) -> None:
    """把合集的频次表写成目录里的 index.json。

    这是**派生缓存**而非真相源 —— 真相在每张图自己的 ishelf:data 里。
    但它让人不用任何工具就能看懂一个文件夹装的是什么。
    """
    col = db.get_collection(conn, collection_id)
    if col is None:
        return
    doc = {
        "v": config.FILE_FORMAT_VERSION,
        "id": col["coll_id"],                     # == 目录名
        "name": col["name"],                      # 文件夹原名
        "side": col["side"],                      # 0=根,1/2/3=父下的第 N 个子合集
        "parent": col["parent_coll_id"],          # 父的 coll_id,根为 null
        "depth": col["depth"],
        # 从**实际目录**推导,不读 DB 里的那一列 —— 那列可能因为改名/迁移而过期,
        # 而 index.json 是写在那个目录里的,它自己知道自己在哪。
        "dir_rel_path": Path(col_dir).relative_to(config.ROOT).as_posix(),
        "date_dir": col["date_dir"], "saved_at": col["saved_at"],
        "finished_at": col["finished_at"], "image_count": col["image_count"],
        "gen_threshold": col["gen_threshold"], "char_threshold": col["char_threshold"],
        "record_floor": config.RECORD_FLOOR, "top_n": config.COLLECTION_TOP_N,
        "failed_count": col["failed_count"],
        "skipped_files": col["skipped_files"], "skipped_dirs": col["skipped_dirs"],
        "tags": [
            {"tag": r["tag"], "category": r["category"], "count": r["count"],
             "count_loose": r["count_loose"], "avg_confidence": round(r["avg_confidence"], 4)}
            for r in db.collection_tag_rows(conn, collection_id)
        ],
    }
    # name 来自用户的文件夹名,NTFS 允许未配对代理项 —— json.dumps 在编码时
    # 会抛 UnicodeEncodeError(它是 ValueError 的子类,不是 OSError),
    # 而这时图片已经归档完了,会把整轮打断。packet.clean_text 已经处理过这类码位。
    doc = json.loads(json.dumps(doc, ensure_ascii=False), strict=False)
    doc["name"] = packet.clean_text(str(doc["name"]))

    text = json.dumps(doc, ensure_ascii=False, indent=2)
    path = Path(col_dir) / config.COLLECTION_INDEX_NAME
    with open(path, "w", encoding="utf-8", errors="replace") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
