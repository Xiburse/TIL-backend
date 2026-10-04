"""配置层:读项目根目录的 `config.toml`,并集中代码专用常量。

每个模块顶部都显式 `from backend.config import ...` —— 一眼能看出它依赖哪些配置。

## 哪些进配置、哪些留代码

**进 config.toml**:路径、阈值、上限、XMP padding、通用合集名。这些是使用者
会想改的。

**留在本文件**:XMP 命名空间、格式版本号、文件名后缀(`_tags.json` /
`index.json` / `version.json` / `journal_*`)、`COLL_DIR_RE`、category 取值。
改这些会**破坏与已有归档的兼容** —— 老图片里写的是旧值,新代码认不出来。
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from pathlib import Path

# ---------------------------------------------------------------------------
# 项目根
#
# 本文件在 <root>/backend/config.py,所以根是上两级。
#
# ⚠ 这个值**必须**等于 <root>,绝不能漂到 <root>/backend ——
#   tags.db 里每一行的 rel_path、_local/layout_migration.json 的 key、
#   version.json 的 digest,全部以它为基准。漂了的后果不是报错,而是:
#     · digest 全变 → 每台设备都误报「索引过期」
#     · reindex 把所有行判成「磁盘上不存在」
#     · 然后 --prune 把这些行删光
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent

# 配置文件的位置。默认在代码旁边(开发/便携场景);装成应用之后代码在
# Program Files(普通用户不可写),这时由调用方通过环境变量指定用户目录下的
# 配置。**必须在 import config 之前设好**,所以入口先解析参数再导入本模块。
CONFIG_PATH = Path(os.environ.get("IMGSHELF_CONFIG") or (ROOT / "config.toml"))

# 代码专用常量:改这些会破坏与已有归档的兼容,所以不放进配置文件
SIDECAR_SUFFIX = "_tags.json"         # 装不了 XMP 的图:xxx.gif -> xxx_tags.json
COLLECTION_INDEX_NAME = "index.json"  # 合集目录下的频次表(派生缓存)
VERSION_NAME = "version.json"         # 归档根目录下的版本号文件
JOURNAL_PREFIX = "journal"            # 每设备一份的只追加索引:journal_<设备>.jsonl
JOURNAL_SUFFIX = ".jsonl"
JOURNAL_LEGACY_NAME = "journal.jsonl"  # 旧命名,仍会被读取

# 参与处理的图片后缀(小写,含点)
IMAGE_EXTS = frozenset({
    ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff",
})

# 能原生嵌入 XMP 的格式。WebP 还有额外条件(必须已带 VP8X),见 imgfmt
EMBEDDABLE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp"})

XMP_NS = "http://ns.adobe.com/xap/1.0/"
XMP_EXT_NS = "http://ns.adobe.com/xmp/extension/"
EXIF_NS = "Exif\x00\x00"
RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
DC_NS = "http://purl.org/dc/elements/1.1/"
ISHELF_NS = "http://ns.imgshelf.local/1.0/"
CREATOR_TOOL = "imgshelf"

# 进程退出码:0 全成功 / 2 部分失败(进了 failed/)/ 1 致命错误
EXIT_OK = 0
EXIT_FATAL = 1
EXIT_PARTIAL = 2

CAT_GENERAL = 0
CAT_CHARACTER = 4
CAT_RATING = 9

TS_NAME_FMT = "%Y%m%d-%H%M%S"      # 文件名里的时间戳
TS_DB_FMT = "%Y-%m-%dT%H:%M:%S"    # 数据库里的时间戳
DATE_DIR_FMT = "%Y-%m-%d"          # 日文件夹名
SHORT_ID_LEN = 12                  # uuid4().hex 的前缀长度,48 bit
PATH_BUDGET = 150                  # 归档路径的总长预算

# ⚠ 不要因为新增字段就 bump 它。这个值同时是 XMP 里 ishelf:data 的解析判据
# (ImageTags.from_json 严格判等)、index.json 的 v、version.json 的 v。
# 一旦 bump,每一张已有图片都会被判为「读不懂」→ source_sha256 / 阈值 / 模型名
# 全部丢失 → 去重失效(重投同一张照片会重跑 GPU 并在库里存第二份),
# 而且合集频次的口径会静默改变。加字段不需要动它。
FILE_FORMAT_VERSION = 1

# 归档**布局**的版本,独立于上面的数据格式版本。结构变化时 bump。
LAYOUT_VERSION = 2

# 合集目录名 == 合集的 id:<入库时间>_<uuid12>
COLL_DIR_RE = re.compile(r"^\d{8}-\d{6}_[0-9a-f]{12}$")

# 系统垃圾:计入「跳过」但不打日志,免得淹没真问题
IGNORED_NAMES = frozenset({
    ".ds_store", "thumbs.db", "desktop.ini", ".localized", ".spotlight-v100",
})


# ---------------------------------------------------------------------------
# 读配置
# ---------------------------------------------------------------------------

# 允许出现的节与键。**多余的一律报错** —— 见文件头的说明。
_SPEC: dict[str, set[str]] = {
    "paths": {"inbox", "library", "failed", "local", "models"},
    "model": {"file", "tags"},
    "thresholds": {"general", "character", "record_floor"},
    "limits": {"image_top_n", "collection_top_n", "max_coll_depth"},
    "xmp": {"padding"},
    "collections": {"generic_names"},
}


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH, "rb") as f:
            doc = tomllib.load(f)
    except FileNotFoundError:
        raise SystemExit(
            f"找不到配置文件:{CONFIG_PATH}\n"
            f"(它应该和 backend/ 并列放在项目根目录)"
        ) from None
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"配置文件语法错误:{CONFIG_PATH}\n  {e}") from None

    problems: list[str] = []
    for section, keys in _SPEC.items():
        got = doc.get(section)
        if got is None:
            problems.append(f"缺少 [{section}] 节")
            continue
        if not isinstance(got, dict):
            problems.append(f"[{section}] 必须是一个表")
            continue
        for k in got:
            if k not in keys:
                problems.append(f"[{section}] 里有未知的键 {k!r} —— 可用的是 {sorted(keys)}")
        for k in keys:
            if k not in got:
                problems.append(f"[{section}] 缺少 {k!r}")
    for k in doc:
        if k not in _SPEC:
            problems.append(f"未知的节 [{k}] —— 可用的是 {sorted(_SPEC)}")

    if problems:
        raise SystemExit(
            "config.toml 有问题:\n  " + "\n  ".join(problems) +
            "\n\n(键名写错不会静默用默认值,这是有意的:阈值一旦写进图片就改不回来了)"
        )
    return doc


_CFG = _load_config()


def _path(section: str, key: str) -> Path:
    """相对路径**相对配置文件所在目录**解析,不相对 CWD。

    否则 `python D:\\x\\backend\\main.py` 会在当前工作目录下新建一个空的
    library,而「从任意目录调用结果一致」这条承诺就静默失效了。
    """
    raw = str(_CFG[section][key]).strip()
    p = Path(raw)
    return p if p.is_absolute() else (ROOT / p)


# ---- 路径 ----
INBOX_DIR = _path("paths", "inbox")
LIBRARY_DIR = _path("paths", "library")
FAILED_DIR = _path("paths", "failed")
LOCAL_DIR = _path("paths", "local")
MODELS_DIR = _path("paths", "models")

MODEL_PATH = MODELS_DIR / str(_CFG["model"]["file"])
TAGS_CSV = MODELS_DIR / str(_CFG["model"]["tags"])

DB_PATH = LOCAL_DIR / "tags.db"
LOGS_DIR = LOCAL_DIR / "logs"
COLLECTION_LOG = LOGS_DIR / "collections.log"
DEVICE_ID_PATH = LOCAL_DIR / "device_id"
STAGING_DIR = LOCAL_DIR / "staging"

# ---- 阈值与上限 ----
GENERAL_THRESHOLD = float(_CFG["thresholds"]["general"])
CHARACTER_THRESHOLD = float(_CFG["thresholds"]["character"])
RECORD_FLOOR = float(_CFG["thresholds"]["record_floor"])
TOP_N = int(_CFG["limits"]["image_top_n"])
COLLECTION_TOP_N = int(_CFG["limits"]["collection_top_n"])
MAX_COLL_DEPTH = int(_CFG["limits"]["max_coll_depth"])
XMP_PADDING = int(_CFG["xmp"]["padding"])
GENERIC_COLLECTION_NAMES: tuple[str, ...] = tuple(
    str(x).casefold() for x in _CFG["collections"]["generic_names"])


# ---------------------------------------------------------------------------
# 启动自检
# ---------------------------------------------------------------------------


def _migrate_local_state() -> None:
    """把老版本的 tags.db / logs 从项目根挪进 _local/(一次性)。

    挪进 _local/ 是为了将来 library/ 放进 OneDrive 时 DB 能被整体排除 ——
    同步服务只能按顶层**文件夹**排除,根目录下的单个文件排除不掉。
    """
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    old_db = ROOT / "tags.db"
    if old_db.is_file() and not DB_PATH.is_file():
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(old_db) + suffix)
            if src.is_file():
                src.replace(Path(str(DB_PATH) + suffix))


def _clean_staging() -> None:
    """清空写盘暂存区。

    暂存区里的东西都是「还没放好的」:正常流程会把它们 rename 进 library,
    rename 成功之后暂存区就该是空的。留着东西只有一种可能 —— 上次跑的时候
    被杀了(用户点取消、断电、进程被任务管理器结束)。

    清掉是安全的:源文件还在 inbox,重跑一遍就会重新生成。不清的话这些
    半成品会一直堆在那儿,而且文件名看着像正经归档件,容易误导人。
    """
    if not STAGING_DIR.is_dir():
        return
    for f in STAGING_DIR.iterdir():
        try:
            if f.is_file():
                f.unlink()
        except OSError:
            pass


def ensure_dirs() -> None:
    """建立所有工作目录(幂等),并清掉上次被杀留下的暂存残file。"""
    _migrate_local_state()
    for d in (INBOX_DIR, LIBRARY_DIR, FAILED_DIR, LOGS_DIR, STAGING_DIR, MODELS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    _clean_staging()


def is_managed_file(name: str) -> bool:
    """判断是不是我们自己生成的辅助文件。

    这些既不是待处理的图片,也**绝不算「残留」** —— 否则扫描器会把
    xxx_tags.json 当成多余文件,收尾逻辑据此认为合集没处理干净,
    把**整个合集目录**移进 failed/。这是个会整批误伤的连锁反应。
    """
    low = name.casefold()
    if low.endswith(SIDECAR_SUFFIX) or low.endswith(".part"):
        return True
    if low in ("index.json", "desktop.ini", "thumbs.db", ".ds_store"):
        return True
    # journal_<设备>.jsonl,以及它自己的 OneDrive 冲突副本
    if low.startswith(JOURNAL_PREFIX) and low.endswith(JOURNAL_SUFFIX):
        return True
    # version.json 及其冲突副本
    return low.startswith("version") and low.endswith(".json")


def is_generic_collection_name(name: str) -> bool:
    key = name.casefold()
    return any(key.startswith(base) for base in GENERIC_COLLECTION_NAMES)


def sync_roots() -> list[Path]:
    """探测本机的 OneDrive 同步根。"""
    roots = []
    for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        value = os.environ.get(var)
        if value:
            p = Path(value)
            if p.is_dir():
                roots.append(p)
    return roots


def _under(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def check_layout() -> None:
    """启动自检:目录不互相嵌套,本地状态不落在同步区里。

    如果 library 落在 inbox 里面,已归档的图片下一轮又会被当成待处理图重新
    打标,而且每跑一轮就在 library 下再套一层日期目录,无限膨胀。
    """
    named = {
        "inbox": INBOX_DIR, "library": LIBRARY_DIR,
        "failed": FAILED_DIR, "local": LOCAL_DIR,
    }
    for name, path in named.items():
        if path == ROOT:
            raise SystemExit(f"配置错误:paths.{name} 不能是项目根目录({ROOT})")
    for a_name, a in named.items():
        for b_name, b in named.items():
            if a_name == b_name:
                continue
            if a == b:
                raise SystemExit(f"配置错误:{a_name} 与 {b_name} 指向同一个目录({a})")
            if a in b.parents:
                raise SystemExit(
                    f"配置错误:{b_name}({b})位于 {a_name}({a})之内,会导致重复处理")

    # 本地状态绝不能落在同步区内。这是本项目最危险的误配:WAL 模式下
    # tags.db 是三个必须一致的文件,被独立上传必然损坏;多端并发还会整文件
    # last-writer-wins 静默丢掉整轮成果。library 放同步区则正是本项目的目标。
    for root in sync_roots():
        for name, path in (("local", LOCAL_DIR), ("staging", STAGING_DIR)):
            if _under(path, root):
                raise SystemExit(
                    f"配置错误:paths.{name}({path})落在 OneDrive 同步区 {root} 之内。\n"
                    f"  tags.db 在 WAL 模式下是三个必须一致的文件,被同步服务独立\n"
                    f"  上传会损坏,多端并发还会静默丢数据。请把它移出同步区。"
                )


def setup_console() -> None:
    """让控制台和重定向输出都按 UTF-8 写。

    不设的话 `python -m backend.main > run.log` 时 stdout 会退化成 ANSI 代码页
    且 errors='strict',一旦打印中日文的合集名就抛 UnicodeEncodeError,
    把整轮处理直接打断。合集名是用户自己起的,非 ASCII 是常态。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
