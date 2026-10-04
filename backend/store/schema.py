
"""数据库结构定义。

**只有 SQL 与列名,没有任何连接或执行逻辑** —— 改表结构时只需要看这一个文件。
执行与迁移在 schema_migrate,日常读写在本包的 db。

⚠ 列声明里不能写 UNIQUE:SQLite 禁止 ALTER TABLE ADD COLUMN 带 UNIQUE,
而老库只能走 ALTER —— 写了会让每次启动都死在同一个地方。唯一性靠 INDEXES。
"""

from __future__ import annotations


SCHEMA_VERSION = 6

# 只放 CREATE TABLE IF NOT EXISTS。迁移用的 ALTER 绝不能写进来 ——
# SQLite 没有 ADD COLUMN IF NOT EXISTS,放这里会导致第一次启动成功后,
# 之后每次启动都报 duplicate column name,整条流水线不可用。
# 注意建表顺序:images 引用 collections,父表要先建。


_SCHEMA = """
CREATE TABLE IF NOT EXISTS collections (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,          -- 文件夹原名(目录名不再含它,所以只存这)
    name_key      TEXT    NOT NULL,          -- name.casefold(),Windows 大小写不敏感
    -- 目录名 == coll_id == <入库时间>_<uuid12>。这里**不能写 UNIQUE** ——
    -- SQLite 禁止 ALTER TABLE ADD COLUMN 带 UNIQUE,而老库只能走 ALTER。
    -- 唯一性由 _INDEXES 里的 idx_coll_coll_id 提供(索引建在迁移之后)。
    coll_id       TEXT,
    side          INTEGER NOT NULL DEFAULT 0,  -- 同级序号:0=根,1/2/3=父下的第 N 个
    parent_coll_id TEXT,                       -- 父的 coll_id,根为 NULL
    depth         INTEGER NOT NULL DEFAULT 0,  -- 0=根
    dir_rel_path  TEXT    NOT NULL UNIQUE,
    date_dir      TEXT    NOT NULL,
    status        TEXT    NOT NULL,          -- building | finalized
    saved_at      TEXT    NOT NULL,
    finished_at   TEXT,
    image_count   INTEGER NOT NULL DEFAULT 0,  -- 本层直接图片数 = 频率分母
    failed_count  INTEGER NOT NULL DEFAULT 0,  -- best-effort,崩溃后无法重建
    skipped_files INTEGER NOT NULL DEFAULT 0,  -- best-effort,同上
    skipped_dirs  INTEGER NOT NULL DEFAULT 0,  -- best-effort,同上
    gen_threshold  REAL,
    char_threshold REAL
);

CREATE TABLE IF NOT EXISTS images (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    status        TEXT    NOT NULL,          -- pending | stored
    -- 图片 id = 归档文件名主干(不含扩展名),也就是磁盘上的名字本身。
    -- **不要用自增主键当身份**:换台设备重建数据库,同一个东西拿到的数字就变了。
    image_id      TEXT    NOT NULL UNIQUE,
    filename      TEXT    NOT NULL,
    rel_path      TEXT    NOT NULL,          -- 相对项目根
    date_dir      TEXT    NOT NULL,          -- 入库日 YYYY-MM-DD
    origin_name   TEXT    NOT NULL,          -- 入库前的文件名
    ext           TEXT    NOT NULL,
    size_bytes    INTEGER NOT NULL,
    width         INTEGER,
    height        INTEGER,
    mtime         TEXT    NOT NULL,          -- 图片修改时间,文件名第一段
    ctime         TEXT    NOT NULL,          -- 文件创建时间,文件名第二段
    saved_at      TEXT    NOT NULL,          -- 实际入库时刻
    shot_at       TEXT,                      -- EXIF 拍摄时间(若有)
    source_sha256  TEXT,                     -- 写 XMP **之前**的源文件指纹(同时进 ishelf:data)
    library_sha256 TEXT,                     -- 归档那份的指纹,入库后立刻算
    source_size_bytes INTEGER,               -- 写 XMP 之前的源文件大小
    xmp_ok         INTEGER,                  -- 1=已嵌入 XMP 0=仅边车/本地库 NULL=未知
    prompt        TEXT    NOT NULL DEFAULT '',
    rating        TEXT,
    rating_score  REAL,
    tag_count     INTEGER NOT NULL DEFAULT 0,
    gen_threshold REAL,
    char_threshold REAL,
    record_floor  REAL,
    model_name    TEXT,
    error         TEXT,
    collection_id INTEGER REFERENCES collections(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS tags (
    image_id   INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    tag        TEXT    NOT NULL,   -- 原始名,保留下划线:long_hair
    category   INTEGER NOT NULL,   -- 0 general / 4 character
    -- 负值 = 置信度未知(重建索引时只从 dc:subject 拿到 tag 名,拿不到分数)。
    -- 用哨兵值而不是 NULL 是为了免去一次表重建;rebuild_collection_tags
    -- 遇到负值会退回按 passed 计数,否则频次表会被算成空。
    confidence REAL    NOT NULL,
    passed     INTEGER NOT NULL,   -- 是否达到当时的阈值
    PRIMARY KEY (image_id, tag)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS collection_tags (
    collection_id  INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    tag            TEXT    NOT NULL,
    category       INTEGER NOT NULL,
    count          INTEGER NOT NULL,  -- 含此 tag 的图片数(按本合集阈值,严格口径)
    count_loose    INTEGER NOT NULL,  -- 同上,但按 RECORD_FLOOR 宽松口径
    avg_confidence REAL    NOT NULL,
    PRIMARY KEY (collection_id, tag)
) WITHOUT ROWID;

"""

# 索引单独放,由 _create_indexes() 在**迁移之后**执行。
# 不能并进 _SCHEMA:idx_images_coll 引用的是 images.collection_id,而 v1 库里
# 还没有这一列,executescript 会在补列之前就报 no such column 直接死掉。


_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_tags_tag      ON tags(tag, image_id);
CREATE INDEX IF NOT EXISTS idx_images_sha    ON images(source_sha256);
CREATE INDEX IF NOT EXISTS idx_images_sha2   ON images(library_sha256);
CREATE INDEX IF NOT EXISTS idx_images_relpath ON images(rel_path);
CREATE INDEX IF NOT EXISTS idx_images_date   ON images(date_dir);
CREATE INDEX IF NOT EXISTS idx_images_mtime  ON images(mtime);
CREATE INDEX IF NOT EXISTS idx_images_origin ON images(origin_name);
CREATE INDEX IF NOT EXISTS idx_images_coll   ON images(collection_id);
CREATE INDEX IF NOT EXISTS idx_ctag_tag      ON collection_tags(tag, collection_id);
CREATE INDEX IF NOT EXISTS idx_coll_name_key ON collections(name_key, saved_at);
-- coll_id 的唯一性靠索引而不是列约束 —— ALTER TABLE 加不了 UNIQUE 列。
CREATE UNIQUE INDEX IF NOT EXISTS idx_coll_coll_id ON collections(coll_id);
CREATE INDEX IF NOT EXISTS idx_coll_tree ON collections(parent_coll_id, side);
"""


_INSERT_COLUMNS = (
    "status", "image_id", "filename", "rel_path", "date_dir", "origin_name", "ext",
    "size_bytes", "width", "height", "mtime", "ctime", "saved_at", "shot_at",
    "source_sha256", "library_sha256", "source_size_bytes", "xmp_ok",
    "prompt", "rating", "rating_score", "tag_count",
    "gen_threshold", "char_threshold", "record_floor", "model_name", "error",
    "collection_id",
)


_COLLECTION_COLUMNS = (
    "name", "name_key", "coll_id", "side", "parent_coll_id", "depth",
    "dir_rel_path", "date_dir", "status", "saved_at",
    "finished_at", "image_count", "failed_count", "skipped_files", "skipped_dirs",
    "gen_threshold", "char_threshold",
)
