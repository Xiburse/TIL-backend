
"""字节拼接骨架:挖掉若干区间、插入新字节。

各格式共用。位置校验的**正确形式**在这里,格式模块负责各自的结构等价检查。
"""

from __future__ import annotations

from backend.imgfmt.errors import XmpVerifyError


def _splice(d: bytes, insert_at: int, removals: list[tuple[int, int]], new: bytes):
    """挖掉 removals 指定的区间,并在 insert_at 处插入 new。

    返回 (结果, kept, kept_at):
      kept    = d 挖掉 removals 之后的样子
      kept_at = insert_at 在 kept 里的对应偏移
    调用方据此断言 `out == kept[:kept_at] + new + kept[kept_at:]`。
    这条**对纯插入和替换都成立**,是位置校验的正确形式。
    """
    removals = sorted(removals)
    for s, e in removals:
        if s < insert_at < e:
            raise XmpVerifyError("插入点落在将被删除的区间之内")

    pieces: list[tuple[int, int]] = []
    pos = 0
    for s, e in removals:
        if s > pos:
            pieces.append((pos, s))
        pos = max(pos, e)
    if pos < len(d):
        pieces.append((pos, len(d)))

    kept = b"".join(d[a:b] for a, b in pieces)

    # 把 insert_at 映射到 kept 里的偏移。**允许它落在某个保留片段的内部** ——
    # 纯插入时整个文件就是一个片段,而插入点在它中间。
    kept_at = 0
    for a, b in pieces:
        if b <= insert_at:
            kept_at += b - a
        elif a < insert_at:
            kept_at += insert_at - a
            break
        else:
            break

    return kept[:kept_at] + new + kept[kept_at:], kept, kept_at
