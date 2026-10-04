
"""不依赖模型、不依赖任何外部文件的自检。

新设备上 test.jpg 未必存在,这是唯一能保证段链拼接不回归的防线。
"""

from __future__ import annotations

import io

from backend import config
from backend.imgfmt import jpeg, png, webp
from backend.imgfmt.packet import ImageTags, build_packet, user_tags_of
from backend.imgfmt.readwrite import read_from_bytes


def _minimal_jpeg() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (120, 130, 140)).save(buf, format="JPEG", quality=60)
    return buf.getvalue()


def self_test() -> list[str]:
    """合成一张最小 JPEG 跑完整往返,返回问题列表(空表示通过)。

    不依赖 model.onnx,也不依赖 test.jpg —— 新设备上这两样都未必存在,
    这是唯一能保证段链拼接不回归的防线。
    """
    problems: list[str] = []
    mini = _minimal_jpeg()

    data = ImageTags(
        tags=[("1girl", 0, 0.9912, True), ("low_conf", 0, 0.2, False)],
        rating="general", rating_score=0.9, model="model.onnx",
        gen_threshold=0.35, char_threshold=0.75, record_floor=0.15,
        top_n=100, width=16, height=16, image_id="20260928-044002_20260928-044002_c772aa6ea37f",
    )

    # 1) 纯插入
    first = jpeg._write_jpeg(mini, build_packet(data, []))
    if len(first) < len(mini):
        problems.append("纯插入后文件反而变小了")
    subjects, back = read_from_bytes(first, ".jpg")
    if subjects != ["1girl"]:
        problems.append(f"纯插入:dc:subject 应为 ['1girl'],实为 {subjects}")
    if back is None:
        problems.append("纯插入:ishelf:data 读不回来")
    elif len(back.tags) != 2 or back.gen_threshold != 0.35:
        problems.append("纯插入:ishelf:data 内容不完整")

    # 2) 替换路径 —— 第二次打标时的常态,也是最容易出 bug 的一条
    second = jpeg._write_jpeg(first, build_packet(data, user_tags_of(subjects, back)))
    subj2, _ = read_from_bytes(second, ".jpg")
    if subj2 != ["1girl"]:
        problems.append(f"替换:dc:subject 应为 ['1girl'],实为 {subj2}")

    # 3) 用户自己在别的工具里加的关键词必须被保住
    third = jpeg._write_jpeg(first, build_packet(data, user_tags_of(["dont_delete_me"], back)))
    subj3, _ = read_from_bytes(third, ".jpg")
    if "dont_delete_me" not in subj3:
        problems.append("替换:用户自己的关键词被清掉了")

    # 4) 删除语义:上一轮写过、这一轮不再产出的 tag 不得被加回
    stale = ImageTags(tags=[("stale_tag", 0, 0.99, True), ("1girl", 0, 0.99, True)])
    t1 = jpeg._write_jpeg(mini, build_packet(stale, []))
    s_a, d_a = read_from_bytes(t1, ".jpg")
    fresh = ImageTags(tags=[("1girl", 0, 0.99, True)])
    t2 = jpeg._write_jpeg(t1, build_packet(fresh, user_tags_of(s_a, d_a)))
    s_b, _ = read_from_bytes(t2, ".jpg")
    if "stale_tag" in s_b:
        problems.append("删除语义失效:上一轮的 tag 被合并回来了")

    return problems
