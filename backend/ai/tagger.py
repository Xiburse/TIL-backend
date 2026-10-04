"""WD14 / WD-v3 tagger 的本地 onnxruntime 推理封装。"""

from __future__ import annotations

import csv
import time
from pathlib import Path

from backend.ai import cuda_bootstrap  # noqa: F401  必须早于 onnxruntime

import numpy as np
import onnxruntime as ort
from PIL import Image, ImageOps

from backend.config import CAT_CHARACTER, CAT_GENERAL, CAT_RATING

DEFAULT_SIZE = 448

# 单张推理明显慢于这个值,就说明没跑在 GPU 上。本机实测 GPU ~85ms、CPU ~1300ms。
SLOW_INFERENCE_MS = 400.0


def load_tag_table(
    csv_path: Path,
) -> tuple[list[str], set[int], set[int], set[int]]:
    """读取 selected_tags.csv,返回 (tag_names, general, character, rating) 四个索引集合。

    ⚠ 这里的索引是 **CSV 的行序**,不是 tag_id 列。
    CSV 里 tag_id 是 9999999 / 9999998 / ... 这种与行序无关的值,而模型的
    输出维度是按行序对齐的。任何「按 tag_id 排序或建索引」的改动都会让
    全部标签整体错位,而且不报错 —— 所以下面用 enumerate,不读 tag_id。
    """
    names: list[str] = []
    general: set[int] = set()
    character: set[int] = set()
    rating: set[int] = set()

    # utf-8-sig 容忍将来可能出现的 BOM;newline="" 是 csv 模块的硬性要求
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        for i, row in enumerate(csv.DictReader(f)):
            names.append(row["name"])
            category = int(row["category"])
            if category == CAT_RATING:
                rating.add(i)
            elif category == CAT_CHARACTER:
                character.add(i)
            elif category == CAT_GENERAL:
                general.add(i)

    return names, general, character, rating


class WDTaggerLocal:
    """绑定到单张 GPU 的 WD 系列 tagger 推理器。"""

    def __init__(
        self,
        model_path: Path,
        csv_path: Path,
        device_id: int = 0,
        use_gpu: bool = True,
    ) -> None:
        model_path = Path(model_path)
        csv_path = Path(csv_path)
        if not model_path.is_file():
            raise FileNotFoundError(f"找不到模型文件: {model_path}")
        if not csv_path.is_file():
            raise FileNotFoundError(f"找不到标签 CSV: {csv_path}")

        self.model_name = model_path.name

        try:
            ort.preload_dlls()
        except AttributeError:
            pass  # 旧版 onnxruntime 没有这个方法

        if use_gpu:
            providers = [
                (
                    "CUDAExecutionProvider",
                    {
                        "device_id": device_id,
                        "arena_extend_strategy": "kNextPowerOfTwo",
                        "gpu_mem_limit": 2 * 1024**3,
                        # 批处理场景用 HEURISTIC:EXHAUSTIVE 每次启动都要重新
                        # benchmark 卷积算法,只对首图有益,还会推高显存峰值。
                        "cudnn_conv_algo_search": "HEURISTIC",
                        "do_copy_in_default_stream": True,
                    },
                ),
                "CPUExecutionProvider",
            ]
        else:
            providers = ["CPUExecutionProvider"]

        self.session = ort.InferenceSession(str(model_path), providers=providers)
        self.providers = self.session.get_providers()

        inp = self.session.get_inputs()[0]
        out = self.session.get_outputs()[0]
        self.input_name = inp.name  # 缓存下来,不必每张图重新查询
        self.output_name = out.name

        # 输入边长。本模型是 NHWC(shape = [N, 448, 448, 3]),所以取 shape[1]。
        # 但换成 NCHW 模型时 shape[1] == 3,拿它当边长会把图缩成 3x3、推理出
        # 垃圾 tag 并永久写进数据库,全程不报错 —— 因此必须做范围校验。
        raw = inp.shape[1] if len(inp.shape) > 1 else None
        self.target_size = raw if isinstance(raw, int) and 224 <= raw <= 1024 else DEFAULT_SIZE

        names, general, character, rating = load_tag_table(csv_path)
        self.tag_names = names
        self.general_indices = general
        self.character_indices = character
        self.rating_indices = rating

        # 标签表与模型输出必须一一对应。错位属于静默的数据损坏,
        # 比直接崩掉危险得多,所以宁可在启动时就报错。
        n_out = out.shape[-1]
        if isinstance(n_out, int) and n_out != len(names):
            raise ValueError(
                f"标签表与模型不匹配:CSV 有 {len(names)} 条,模型输出 {n_out} 维。"
                f"两者必须一一对应,否则打出来的 tag 全是错的。"
            )

        self._timings: list[float] = []

    # ---------- 推理 ----------

    def _preprocess(self, im: Image.Image) -> np.ndarray:
        """等比缩放 + 白底居中补齐 + BGR,返回 NCHW... 实为 [1, H, W, C] 的输入张量。"""
        # 手机竖拍图带 EXIF orientation,不校正就会被旋转 90 度送进模型,
        # 直接拉低 tag 质量(不只是观感问题)
        im = ImageOps.exif_transpose(im) or im
        im = im.convert("RGB")
        im.thumbnail((self.target_size, self.target_size), Image.Resampling.LANCZOS)

        canvas = Image.new("RGB", (self.target_size, self.target_size), (255, 255, 255))
        canvas.paste(
            im,
            ((self.target_size - im.width) // 2, (self.target_size - im.height) // 2),
        )

        arr = np.asarray(canvas, dtype=np.float32)[:, :, ::-1]
        # 上面 [:, :, ::-1] 得到的是负步长视图、内存不连续,ORT 会隐式拷贝。
        # 显式连续化,省掉这类「偶发变慢」的困惑。
        return np.ascontiguousarray(arr)[None]

    def _infer(self, arr: np.ndarray) -> np.ndarray:
        t0 = time.perf_counter()
        probs = self.session.run([self.output_name], {self.input_name: arr})[0][0]
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        self._timings.append(elapsed_ms)
        return probs

    def predict(
        self,
        image_path: Path,
        general_threshold: float,
        character_threshold: float,
        record_floor: float,
        top_n: int,
    ) -> dict:
        """对单张图推理。

        records 里的 tag 名**保留下划线原形**(long_hair),只有在拼 prompt
        和对外展示时才转成空格。WD14 生态里到处都在用下划线形式查询,
        库里存成空格形式的话按 long_hair 就永远查不到了。
        """
        with Image.open(Path(image_path)) as im:
            arr = self._preprocess(im)
        probs = self._infer(arr)

        ratings: dict[str, float] = {}
        passed: list[tuple[str, int, float, bool]] = []
        below: list[tuple[str, int, float, bool]] = []

        for i, prob in enumerate(probs):
            p = float(prob)

            if i in self.rating_indices:
                ratings[self.tag_names[i]] = p  # rating 只记分数,不设阈值
                continue

            is_character = i in self.character_indices
            threshold = character_threshold if is_character else general_threshold
            category = CAT_CHARACTER if is_character else CAT_GENERAL

            if p < record_floor:
                continue

            row = (self.tag_names[i], category, p, p >= threshold)
            (passed if row[3] else below).append(row)

        # 过阈值的 tag 永远优先保留,不会被 TOP_N 截掉 —— 否则会出现
        # prompt 里有、但库里查不到的 tag。
        passed.sort(key=lambda r: r[2], reverse=True)
        below.sort(key=lambda r: r[2], reverse=True)
        room = max(0, top_n - len(passed))
        records = passed + below[:room]

        best_rating = max(ratings.items(), key=lambda kv: kv[1]) if ratings else (None, None)

        return {
            "prompt": ", ".join(name.replace("_", " ") for name, *_ in passed),
            "records": records,
            "ratings": ratings,
            "rating": best_rating[0],
            "rating_score": best_rating[1],
            "tag_count": len(passed),
        }

    # ---------- 速度自检 ----------

    def _blank_input(self) -> np.ndarray:
        blank = np.full((self.target_size, self.target_size, 3), 255, dtype=np.uint8)
        return np.ascontiguousarray(blank[:, :, ::-1].astype(np.float32))[None]

    def warmup(self) -> None:
        """预热一次,并把这次耗时从统计中剔除。

        第一次推理包含 CUDA 上下文初始化,耗时可能是正常值的数倍。放在
        批处理开始前做掉,免得它污染第一张图的耗时、ETA,以及速度哨兵
        —— 否则处理单张图时平均值会虚高,误报「已回退 CPU」。
        """
        self._infer(self._blank_input())
        self._timings.clear()

    def check(self) -> dict:
        """用一张空白图验证加速是否真的生效。"""
        self.warmup()
        self._infer(self._blank_input())
        ms = self._timings[-1]
        return {
            "providers": self.providers,
            "target_size": self.target_size,
            "tags_in_csv": len(self.tag_names),
            "ms": ms,
            "warning": self.speed_warning(),
        }

    def speed_warning(self) -> str | None:
        """耗时异常时给出提示。

        onnxruntime 在 CUDA 加载失败时会静默回退 CPU,而 get_providers()
        仍然列出 CUDAExecutionProvider —— 只看 provider 判断不出来,
        只能靠耗时。
        """
        if not self._timings:
            return None
        avg = sum(self._timings) / len(self._timings)
        if avg > SLOW_INFERENCE_MS:
            return (
                f"单张推理平均 {avg:.0f} ms,远高于 GPU 正常水平(~85 ms),"
                f"很可能已静默回退到 CPU。请检查 cuDNN 的 DLL 是否加载成功。"
            )
        return None

    def average_ms(self) -> float:
        if not self._timings:
            return 0.0
        return sum(self._timings) / len(self._timings)
