"""CUDA / cuDNN 动态库搜索路径引导。

**本模块必须在 `import onnxruntime` 之前被导入。**

背景(本机实测,2026-09):
onnxruntime-gpu 的 CUDAExecutionProvider 依赖 nvidia-* wheel 提供的
CUDA/cuDNN DLL。主库能正常加载,但 cuDNN 9 的 engines 子库
(cudnn_engines_tensor_ir64_9.dll 等)是推理时由 cuDNN frontend
**动态 LoadLibrary** 的,走的是 Windows 标准搜索顺序(含 PATH);
而 os.add_dll_directory 只影响 LoadLibraryEx 的 user-dirs 路径,覆盖不到。

症状很有迷惑性:provider 列表里明明有 CUDAExecutionProvider,但第一个
Conv 节点报 `Could not locate cudnn_engines_tensor_ir64_9.dll`,
onnxruntime 随后静默回退 CPU。表现为单张推理 ~1300ms 而不是 ~85ms,
只看 get_providers() 完全判断不出来。

所以两件事都要做:
  1. 把 DLL 目录前置进 PATH —— 实际解决本问题(实测 1260ms -> 85ms)
  2. os.add_dll_directory     —— 覆盖走 LoadLibraryEx 的加载路径。
     返回值必须持有:句柄被 GC 后目录会从搜索路径中移除。

定位 torch 只用 importlib,不做 `import torch` —— 我们只需要那个路径,
导入 torch 本身要多花数秒启动时间和约 1GB 内存。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

# 持有 os.add_dll_directory 的返回值。丢掉它们会让刚加进去的目录立刻
# 因 GC 而失效。
_keep: list = []


def _candidate_dirs() -> list[str]:
    """列出所有可能含有 CUDA/cuDNN DLL 的目录。"""
    dirs: list[str] = []

    try:
        spec = importlib.util.find_spec("torch")
    except (ImportError, ValueError):
        spec = None
    if spec and spec.submodule_search_locations:
        lib = Path(spec.submodule_search_locations[0]) / "lib"
        if lib.is_dir():
            dirs.append(str(lib))

    nvidia = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    if nvidia.is_dir():
        for root, _subdirs, files in os.walk(nvidia):
            if any(f.lower().endswith(".dll") for f in files):
                dirs.append(root)

    return dirs


def _install() -> list[str]:
    dirs = _candidate_dirs()
    if not dirs:
        return []

    os.environ["PATH"] = os.pathsep.join(dirs) + os.pathsep + os.environ.get("PATH", "")

    for d in dirs:
        try:
            _keep.append(os.add_dll_directory(d))
        except OSError:
            pass  # 目录不可用,交给 PATH 兜底

    return dirs


DLL_DIRS = _install()


# ---------------------------------------------------------------------------
# 顺序断言
#
# 本模块**必须**在 `import onnxruntime` 之前被导入。顺序错了的后果不是报错,
# 而是 cuDNN 加载失败后 onnxruntime **静默回退 CPU** —— 单张推理从 85ms 变成
# 1300ms,慢 15 倍,而且没有任何提示。这种问题只有断言能防住。
# ---------------------------------------------------------------------------
if "onnxruntime" in sys.modules:
    raise RuntimeError(
        "cuda_bootstrap 必须在 import onnxruntime 之前导入。\n"
        "  顺序错了会让 cuDNN 静默加载失败、推理回退到 CPU(85ms -> 1300ms),\n"
        "  而且不会有任何报错。正确做法是让 backend.ai.tagger 的第一行导入它。"
    )
