"""每个文件夹一份、**每台设备各自一份**的只追加索引日志。

## 它解决什么问题

新设备或新进程要建本地缓存时,如果只能逐张图去读 XMP,每张图至少一次
网络往返(拿下载地址 + Range 取字节)。1 万张图就是 2 万次往返、几十分钟。
而 journal 把整个文件夹的 tag 汇总成一个文件,**读一个文件就够了**。

## 三条不可动摇的设计约束

1. **只追加,永不重写。** 追加是唯一一种同步服务能正确合并的操作 ——
   文件只变长,不产生分叉。要"修改"一条记录就再追加一条,以最后一条为准。

2. **每台设备写自己的文件** (`journal_<设备名>.jsonl`)。各写各的文件,
   结构上就不存在"两端追加到同一个文件"这个冲突场景。读取端把全部设备的
   journal 并起来即可。

3. **它是派生缓存,不是真相源。** 真相仍然在每张图的 XMP 里。journal 丢了、
   落后了、被写坏了,程序都能回退到逐图读 XMP 重建 —— 这保证它永远不是单点,
   随时可以整个删掉。

## 为什么不做 gzip

实测 JSONL 压到 26%,一年能省 0.8 GB。但压缩流无法按字节偏移增量读取 ——
而"只取上次之后新增的几 KB"正是 journal 之所以快的关键。空间换时间,这里
选择时间。1 万张图一年约 1.1 GB,可以接受。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from backend.imgfmt import packet
from backend import config
from backend.common import logbook

# 除 ImageTags 自带字段外,journal 行还要多带这几个,否则重建时补不齐
# images 表里的列。
_EXTRA_KEYS = ("p", "sz", "lh", "xk")   # rel_path / size_bytes / library_sha256 / xmp_ok

_GLOB = f"{config.JOURNAL_PREFIX}*{config.JOURNAL_SUFFIX}"


def journal_name(device: str) -> str:
    return f"{config.JOURNAL_PREFIX}_{device}{config.JOURNAL_SUFFIX}"


def journal_path(folder: Path, device: str) -> Path:
    return Path(folder) / journal_name(device)


def found_journals(folder: Path) -> list[Path]:
    return sorted(f for f in Path(folder).glob(_GLOB) if f.is_file())


def device_of(name: str) -> str:
    """从文件名反解设备名,用于汇报。"""
    low = name.casefold()
    if low == config.JOURNAL_LEGACY_NAME:
        return "(旧版统一命名)"
    body = name[len(config.JOURNAL_PREFIX):-len(config.JOURNAL_SUFFIX)].lstrip("_")
    return body or "(未知)"


def to_line(data: packet.ImageTags, rel_path: str, size_bytes: int | None,
            library_sha256: str | None, xmp_ok: int) -> str:
    """把一条记录编码成一行。

    用 ensure_ascii=False:XMP 里那个 JSON 必须 ASCII 是为了让 APP1 段的字节数
    可预测,而 journal 没有这个约束,中文路径直接写更省。
    """
    doc = json.loads(data.to_json())
    doc["p"] = rel_path
    # 图片 id 一律从路径推出来,不用 data 里那个 —— 文件名才是权威,
    # 老图片的 XMP 里可能还写着当初的 short_id。
    doc["image_id"] = Path(rel_path).stem
    doc.pop("uuid", None)   # 老格式留下的键,绝不能跟着写出去
    doc["sz"] = size_bytes
    doc["lh"] = library_sha256
    doc["xk"] = xmp_ok
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":"))


def parse_line(line: str) -> tuple[str, packet.ImageTags, dict] | None:
    """返回 (rel_path, ImageTags, 额外字段)。解析失败返回 None。

    单行解析失败只丢这一条 —— 这正是"一行一条"相对"一个大 JSON 数组"的好处:
    截断或写坏一行,不会让整个文件不可用。
    """
    line = line.strip()
    if not line:
        return None
    try:
        doc = json.loads(line)
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None

    rel = doc.pop("p", None)
    if not rel:
        return None
    extra = {k: doc.pop(k, None) for k in _EXTRA_KEYS if k != "p"}
    data = packet.ImageTags.from_json(json.dumps(doc, ensure_ascii=False))
    if data is None:
        return None
    return rel, data, extra


def _read_file(path: Path, merged: dict[str, tuple]) -> None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for line in text.split("\n"):
        parsed = parse_line(line)
        if parsed is not None:
            rel, data, extra = parsed
            # 最后读到的说了算 —— "只追加、永不重写"的必然推论:改就是再追加一条。
            # 文件名排序保证同一 rel_path 的取舍稳定。
            merged[rel] = (data, extra)


def append(folder: Path, lines: list[str], device: str) -> int:
    """把若干行追加到**本设备**在该文件夹的 journal,返回写入字节数。

    先 fsync 再返回:journal 是缓存,但半行写坏会浪费一次重建。
    """
    lines = [ln for ln in lines if ln]
    if not lines:
        return 0
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    target = journal_path(folder, device)

    # 一次性收编:早期版本用的是统一文件名 journal.jsonl。把它改名成本设备的
    # journal,免得同一批数据长期存两份。两台设备同时抢着改名时只有一个会成功,
    # 另一个的 rename 会因为源文件已消失而失败 —— 忽略即可。
    legacy = folder / config.JOURNAL_LEGACY_NAME
    if legacy.is_file() and not target.exists():
        try:
            os.replace(legacy, target)
        except OSError:
            pass

    blob = ("\n".join(lines) + "\n").encode("utf-8")
    try:
        with open(target, "ab") as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        # journal 是加速层,写不进去不该让整轮处理失败 —— XMP 里那份才是真相
        logbook.record("告警", f"journal 写入失败({folder}): {e}")
        return 0
    return len(blob)


def adopt_legacy(device: str) -> int:
    """把早期版本用统一文件名写的 `journal.jsonl` 收编成本设备的 journal。

    一次性迁移。不收编的话同一批数据会长期存两份 —— 旧的那份**永远不会再被
    写入**,却仍会被读取端读到,白占空间。

    两台设备同时抢着改名时只有一个会成功,另一个忽略即可:内容仍在对方那份
    文件里,读取端照样读得到,不会丢数据。
    """
    if not config.LIBRARY_DIR.is_dir():
        return 0
    moved = 0
    for legacy in sorted(config.LIBRARY_DIR.rglob(config.JOURNAL_LEGACY_NAME)):
        if not legacy.is_file():
            continue
        target = legacy.parent / journal_name(device)
        if target.exists():
            continue
        try:
            os.replace(legacy, target)
            moved += 1
        except OSError:
            pass
    return moved


def purge_entries(folder: Path, rel_paths: set[str]) -> int:
    """把 journal 里指向这些 rel_path 的条目删掉,返回删了几条。

    **这是全项目唯一会重写 journal 的地方。** 「只追加、永不重写」那条
    约束是为了让多设备同时追加时不产生冲突副本 —— 而这里是**删数据**:
    图片已经没了,它在 journal 里的记录就是死条目。留着只有坏处:白占空间、
    让人以为那张图还在、重建索引时白读一遍。

    journal 是**派生缓存**(真相是每张图里的 XMP),所以重写它是安全的 ——
    就算写坏了也只是丢缓存,`--reindex` 能原样重建。为保险仍然用
    临时文件 + 改名,避免中途被杀留下半个文件。

    同一目录下所有设备的 journal 都会被清:那些条目指向的图已经不存在了,
    无论当初是哪台设备写的,留着都是垃圾。
    """
    if not rel_paths:
        return 0

    removed = 0
    for f in found_journals(Path(folder)):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        raw = [l for l in text.splitlines() if l.strip()]
        kept: list[str] = []
        for line in raw:
            try:
                doc = json.loads(line)
            except ValueError:
                kept.append(line)   # 坏行原样保留:不是我们要动的东西
                continue
            if doc.get("p") in rel_paths:
                removed += 1
            else:
                kept.append(line)

        if len(kept) == len(raw):
            continue                 # 这个文件里一条都没删,不动它
        if not kept:
            try:
                f.unlink(missing_ok=True)   # 全删光了就删掉整个文件
            except OSError:
                pass
            continue

        tmp = f.with_name(f.name + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8", errors="replace") as fh:
                fh.write('\n'.join(kept) + '\n')
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, f)
        except OSError:
            tmp.unlink(missing_ok=True)

    return removed

def normalize_ids() -> int:
    """把 journal 里老格式的 `uuid` 换成 `image_id`,返回改了几行。

    老 journal 每行存的是 `uuid` = **文件名第三段**(`482214311c88`),而图片 id
    是**文件名主干**(`20260906-022837_20260928-043650_482214311c88`)——
    **光改键名不够,值也得从路径重新推出来。**

    值一律取 `p`(相对路径)的 stem,不读行里那个 —— 路径是权威,uuid 从来不是。

    journal 是派生缓存(真相在图片的 XMP 里),所以重写它是安全的;万一写坏
    也只是丢缓存,`--reindex` 能原样重建。仍然走临时文件 + 改名,避免中途被杀
    留下半个文件。
    """
    if not config.LIBRARY_DIR.is_dir():
        return 0

    changed = 0
    # rglob 而不是 found_journals:后者是**非递归**的(按目录定位用),
    # 而 journal 散在 library/<日期>/<合集>/ 这些嵌套目录里,要整棵扫。
    for f in sorted(config.LIBRARY_DIR.rglob(_GLOB)):
        if not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        out: list[str] = []
        touched = False
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                doc = json.loads(line)
            except ValueError:
                out.append(line)      # 坏行原样保留
                continue

            rel = doc.get("p")
            want = Path(rel).stem if rel else None
            # 判定条件:老键还在,或者 id 值和路径对不上(说明是 short_id)
            if "uuid" in doc or (want and doc.get("image_id") != want):
                if want:
                    doc["image_id"] = want
                doc.pop("uuid", None)
                touched = True
            out.append(json.dumps(doc, ensure_ascii=False, separators=(",", ":")))

        if not touched:
            continue

        tmp = f.with_name(f.name + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8", errors="replace") as fh:
                fh.write('\n'.join(out) + '\n')
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, f)
            changed += 1
        except OSError:
            tmp.unlink(missing_ok=True)

    return changed


def read_folder(folder: Path, device: str | None = None) -> dict[str, tuple]:
    merged: dict[str, tuple] = {}
    for f in found_journals(Path(folder)):
        _read_file(f, merged)
    return merged


def read_all(device: str | None = None) -> tuple[dict[str, tuple], list[Path], list[str]]:
    """读遍整个 library 的所有 journal(**含其他设备的**)。

    返回 ({rel_path: (data, extra)}, 本设备的冲突副本, 见到的设备名列表)。

    注意:一个文件夹里有多个 journal 现在是**正常**的(每台设备一份),
    所以不再当作冲突报警。只有"文件名以本设备 id 开头、却不等于本设备文件名"
    才是真正的冲突副本(OneDrive 在两端同时写同一文件时产生)。
    """
    merged: dict[str, tuple] = {}
    conflicts: list[Path] = []
    devices: set[str] = set()
    if not config.LIBRARY_DIR.is_dir():
        return merged, conflicts, []

    ours = journal_name(device) if device else None

    for f in sorted(config.LIBRARY_DIR.rglob(_GLOB)):
        if not f.is_file():
            continue
        devices.add(device_of(f.name))
        if ours and f.name != ours and device and f.name.startswith(
                f"{config.JOURNAL_PREFIX}_{device}"):
            conflicts.append(f)
        _read_file(f, merged)

    return merged, conflicts, sorted(devices)
