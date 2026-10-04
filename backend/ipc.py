"""IPC 层:从 stdin 读一个 JSON 请求,把结果按统一信封写到 stdout。

## 为什么是 stdin/stdout 而不是本地 HTTP

开端口在 Windows 上会弹防火墙授权(首次体验很差),还要处理端口占用和进程
生命周期。stdio 是现成的、零依赖的、天然按行分帧的。

## 统一信封

**请求**(stdin,一行 JSON):

    {"cmd": "search", "args": {"tags_all": ["1girl"], "limit": 50}}

**响应**(stdout,一行或多行 JSON):

    {"t":"event", "event":"progress", "data":{...}}     # 进度,仅流式命令
    {"t":"result","ok":true,  "data":{...}}
    {"t":"result","ok":false, "error":{"code":"...","message":"..."}}

消费者**一直读行,直到 `t == "result"`**。非流式命令只产生一行 result;
流式命令(ingest / reindex / verify / check)在它之前会有若干 event。
**前端不需要知道哪条命令是流式的 —— 读法完全一样。**
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import traceback

from backend import config
from backend.common import emitter as emitter_mod
from backend.common import logbook
from backend.store import db, journal, search


# ---------------------------------------------------------------------------
# 参数:每条命令都接受同一套可选参数,缺省从 config.toml 取
# ---------------------------------------------------------------------------

_COMMON_DEFAULTS = {
    # 分页与运行控制(入库类命令用)
    "limit": None, "offset": 0, "dry_run": False, "cpu": False, "device": 0,
    "no_xmp": False, "yes": False, "prune": False, "from_images": False,
    "general_threshold": None, "character_threshold": None,
    # 检索(查询类命令用)
    "tags_all": None, "tags_any": None, "rating": None,
    "date": None, "shot_from": None, "shot_to": None,
    "min_conf": 0.0, "coll_id": None, "collection_key": None,
    # 单点与合集
    "image_id": None, "name": None, "tag": None, "min_freq": None,
    # 删除(数组形式,一次可以删多个)
    "image_ids": None, "coll_ids": None,
}


def _args(raw: dict) -> argparse.Namespace:
    """把请求里的 args 变成一个和原来 CLI 一样形状的 Namespace。

    所有命令共用同一套键,缺的用默认值补齐 —— 这样 Rust 侧不用为每条命令
    记不同的参数表。
    """
    merged = dict(_COMMON_DEFAULTS)
    for k, v in (raw or {}).items():
        merged[k] = v
    if merged["general_threshold"] is None:
        merged["general_threshold"] = config.GENERAL_THRESHOLD
    if merged["character_threshold"] is None:
        merged["character_threshold"] = config.CHARACTER_THRESHOLD
    return argparse.Namespace(**merged)


# ---------------------------------------------------------------------------
# 输出形状:所有返回图片的命令共用同一个 image 对象
# ---------------------------------------------------------------------------


def _image(r) -> dict:
    return {
        "image_id": r["image_id"],
        "rel_path": r["rel_path"],
        "abs_path": str(config.ROOT / r["rel_path"]),
        "filename": r["filename"],
        "origin_name": r["origin_name"],
        "mtime": r["mtime"],
        "ctime": r["ctime"],
        "shot_at": r["shot_at"],
        "width": r["width"],
        "height": r["height"],
        "rating": r["rating"],
        "rating_score": r["rating_score"],
        "tag_count": r["tag_count"],
        "prompt": r["prompt"],
        "collection_id": r["collection_id"],
        "xmp_ok": r["xmp_ok"],
    }


def _collection(r) -> dict:
    """合集的身份是 coll_id(文件夹名)。

    **不返回数据库自增主键** —— 它由 SQLite 分配,换台设备重建数据库后同一个
    合集会拿到不同的数字。前端拿它做事迟早出错,所以干脆不暴露。
    """
    return {
        "coll_id": r["coll_id"],
        "name": r["name"],
        "side": r["side"],
        "parent_coll_id": r["parent_coll_id"],
        "depth": r["depth"],
        "dir_rel_path": r["dir_rel_path"],
        "date_dir": r["date_dir"],
        "image_count": r["image_count"],
        "gen_threshold": r["gen_threshold"],
        "char_threshold": r["char_threshold"],
    }


def _conn(ro: bool = True):
    if ro:
        try:
            return db.connect_ro(config.DB_PATH)
        except sqlite3.OperationalError:
            return db.connect(config.DB_PATH)   # WAL 只读打开的兜底
    return db.connect(config.DB_PATH)


# ---------------------------------------------------------------------------
# 各命令
# ---------------------------------------------------------------------------


def cmd_status(a, emit) -> dict:
    """库的整体状态。前端启动时第一个调它,用来决定显示什么。"""
    exists = config.DB_PATH.is_file()
    out = {
        "db_exists": exists,
        "root": str(config.ROOT),
        "library": str(config.LIBRARY_DIR),
        "inbox": str(config.INBOX_DIR),
        "model_exists": config.MODEL_PATH.is_file(),
        "schema_version": None,
        "layout_version": config.LAYOUT_VERSION,
        "device_id": None,
        "images": 0, "collections": 0, "tags": 0,
        "inbox_pending": 0,
        "index_stale": False,
    }
    if exists:
        conn = _conn()
        try:
            out["schema_version"] = db.schema_version(conn)
            out["images"], out["collections"], out["tags"] = db.counts(conn)
            out["device_id"] = db.get_meta(conn, "device_id")
            out["index_stale"] = db.get_meta(conn, "digest") is None
        finally:
            conn.close()
    try:
        out["inbox_pending"] = len(
            [p for p in config.INBOX_DIR.iterdir() if not p.name.startswith(".")])
    except OSError:
        pass
    return out


def cmd_search(a, emit) -> dict:
    """按 tag / 分级 / 日期 / 合集检索图片。"""
    conn = _conn()
    try:
        rows = search.search_images(
            conn,
            tags_all=a.tags_all, tags_any=a.tags_any, rating=a.rating,
            date_dir=a.date, shot_from=a.shot_from, shot_to=a.shot_to,
            min_conf=a.min_conf or 0.0,
            coll_id=a.coll_id, collection_key=a.collection_key,
            limit=a.limit,
        )
        return {"count": len(rows), "images": [_image(r) for r in rows]}
    finally:
        conn.close()


def cmd_image_detail(a, emit) -> dict:
    """单张图的完整 tag 列表(含置信度)。"""
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM images WHERE image_id=?", (a.image_id,)).fetchone()
        if row is None:
            return {"image": None}
        tags = conn.execute(
            "SELECT tag, category, confidence, passed FROM tags "
            "WHERE image_id=? ORDER BY confidence DESC", (row["id"],)).fetchall()
        return {
            "image": _image(row),
            "tags": [{"tag": t["tag"], "category": t["category"],
                      "confidence": round(t["confidence"], 4),
                      "passed": bool(t["passed"])} for t in tags],
        }
    finally:
        conn.close()


def cmd_top_tags(a, emit) -> dict:
    conn = _conn()
    try:
        rows = search.top_tags(conn, a.min_conf or 0.0, a.limit or 100)
        return {"tags": [{"tag": r["tag"], "count": r["n"]} for r in rows]}
    finally:
        conn.close()


def cmd_collections(a, emit) -> dict:
    """合集树(按 side 排序,不是按随机 coll_id)。"""
    conn = _conn()
    try:
        rows = search.list_collections(conn, a.tag, a.min_freq)
        return {"collections": [_collection(r) for r in search.tree_order(rows)]}
    finally:
        conn.close()


def cmd_collection_tags(a, emit) -> dict:
    """某个合集的 tag 频次表。同名可能有多个,全部返回。"""
    conn = _conn()
    try:
        if a.coll_id:
            rows = conn.execute("SELECT * FROM collections WHERE coll_id=?",
                                (a.coll_id,)).fetchall()
            cols = list(rows)
        else:
            cols = db.find_collection_by_name_key(conn, (a.name or "").casefold())
        out = []
        for c in cols:
            rows = db.collection_tag_rows(conn, c["id"])
            total = c["image_count"] or 0
            out.append({
                **_collection(c),
                "tags": [{
                    "tag": r["tag"], "category": r["category"],
                    "count": r["count"], "count_loose": r["count_loose"],
                    "freq": round(r["count"] / total, 4) if total else 0.0,
                    "avg_confidence": round(r["avg_confidence"], 4),
                } for r in rows],
            })
        return {"collections": out}
    finally:
        conn.close()


def cmd_delete(a, emit) -> dict:
    """删除图片 / 合集(含整棵子树)。**不可逆**。

    `image_ids` 给图片 id 数组(= 归档文件名主干,不带扩展名),
    `coll_ids` 给合集 id 数组(= 合集文件夹名)。合集会连同整棵子树
    一起删。支持 `dry_run` 预演。
    """
    from backend.pipeline.delete import run_delete
    return run_delete(a, emit)


def cmd_ingest(a, emit) -> dict:
    """处理 inbox/。**流式**:每张图发 image_done,每个合集发 start/done。"""
    from backend.main import run_ingest
    return run_ingest(a, emit)


def cmd_reindex(a, emit) -> dict:
    from backend.pipeline.reindex import run_reindex
    return run_reindex(a, emit)


def cmd_verify(a, emit) -> dict:
    from backend.pipeline.verify import run_verify_library
    return run_verify_library(a, emit)


def cmd_check(a, emit) -> dict:
    from backend.pipeline.ingest import run_check
    return run_check(a, emit)


COMMANDS = {
    "status": cmd_status,
    "search": cmd_search,
    "image_detail": cmd_image_detail,
    "top_tags": cmd_top_tags,
    "collections": cmd_collections,
    "collection_tags": cmd_collection_tags,
    "delete": cmd_delete,
    "ingest": cmd_ingest,
    "reindex": cmd_reindex,
    "verify": cmd_verify,
    "check": cmd_check,
}


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main() -> int:
    emit = emitter_mod.Emitter()
    logbook.bind(emit)

    raw_line = sys.stdin.readline()
    if not raw_line.strip():
        emit.result_err("bad_request", "stdin 里没有请求。需要一行 JSON。")
        return 2

    import json

    try:
        req = json.loads(raw_line)
    except ValueError as e:
        emit.result_err("bad_request", f"请求不是合法 JSON:{e}")
        return 2

    cmd = req.get("cmd")
    fn = COMMANDS.get(cmd)
    if fn is None:
        emit.result_err("unknown_command",
                        f"不认识命令 {cmd!r}", available=sorted(COMMANDS))
        return 2

    args = _args(req.get("args"))
    try:
        config.ensure_dirs()
        config.check_layout()
        # 任何命令之前先把 schema 补到当前版本 —— 前端第一个调的是 status,
        # 而状态查询本身不写库。不在这儿迁移的话,老库会一直显示过期的
        # schema_version,而且第一条读命令就可能撞上缺列。
        # 只在库已存在时做:全新安装不该因为问了一句状态就凭空建出一个库。
        if config.DB_PATH.is_file():
            _c = db.connect(config.DB_PATH)
            try:
                db.init_schema(_c)
            finally:
                _c.close()
            # journal 里老格式的 `uuid` 换成 `image_id`(值也要从路径重推)。
            # 幂等:全换完之后再跑就是零改动。
            journal.normalize_ids()
        data = fn(args, emit)
        emit.result_ok(data)
        return 0
    except SystemExit as e:
        # ensure_dirs / check_layout 用 SystemExit 报配置错误
        emit.result_err("config_error", str(e))
        return 1
    except Exception as e:
        # traceback 绝不能往 stdout 写 —— 那是协议通道。走 stderr。
        traceback.print_exc(file=sys.stderr)
        emit.result_err(type(e).__name__, str(e))
        return 1
