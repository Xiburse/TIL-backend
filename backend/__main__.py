"""IPC 入口:Rust 侧调用 `python -m backend`。

请求从 stdin 读一行 JSON,响应按统一信封写到 stdout。协议见项目根目录 API.md。

`--config <路径>` 指定配置文件。必须在导入 backend.config 之前生效 ——
配置路径是在模块导入时确定的,所以这里先解析参数、设好环境变量再导入。
"""

from __future__ import annotations

import os
import sys


def _apply_argv() -> None:
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--config" and i + 1 < len(argv):
            os.environ["IMGSHELF_CONFIG"] = argv[i + 1]
        elif a.startswith("--config="):
            os.environ["IMGSHELF_CONFIG"] = a.split("=", 1)[1]


_apply_argv()

from backend import ipc  # noqa: E402  (必须在 _apply_argv 之后)

if __name__ == "__main__":
    sys.exit(ipc.main())
