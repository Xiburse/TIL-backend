"""输出收口:进程里所有的对外输出都从这里走。

## 为什么必须收口

IPC 模式下 **stdout 是协议通道,不是给人看的**。任何一行不是 JSON 的输出,
前端解析时就会炸。所以连 `print()` 调试、traceback 都不能直接往 stdout 写 ——
错误必须包成 `{"t":"result","ok":false,...}` 从协议里出去。

## 统一信封

每一行 stdout 都是一个 JSON 对象,**形状只有两种**:

    {"t":"event", "event":"<名字>", "data":{...}}     # 进度、日志、告警
    {"t":"result","ok":true,  "data":{...}}           # 最后一行,永远
    {"t":"result","ok":false, "error":{"code":...,"message":...}}

消费者**一直读行,直到 `t == "result"`**。非流式命令只会产生一行 result;
流式命令(入库、重建、巡检)在它之前会有若干 event。这样前端不需要知道
每条命令是流式还是非流式 —— 读法完全一样。
"""

from __future__ import annotations

import sys
from typing import Any, TextIO


class Emitter:
    """把事件与结果写成一行一条的 JSON。"""

    def __init__(self, stream: TextIO | None = None) -> None:
        self._out = stream if stream is not None else sys.stdout

    # ---- 底层 ----

    def _write(self, obj: dict) -> None:
        import json

        # 每行都要 flush。不 flush 的话 stdout 会缓冲,前端的进度条会一顿
        # 一顿地跳 —— 这是流式协议最常见的坑。
        self._out.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
        self._out.flush()

    # ---- 对外 ----

    def event(self, _name: str, **data: Any) -> None:
        """发一个进度/日志事件。

        第一个参数**故意叫 _name** —— 调用方经常要传一个业务字段也叫
        `name`(比如图片名),同名会撞成 "got multiple values"。
        """
        self._write({"t": "event", "event": _name, "data": data})

    def log(self, message: str) -> None:
        """原来的 print —— 现在是事件,不再是人类可读的一行。"""
        self._write({"t": "event", "event": "log", "data": {"message": message}})

    def warn(self, kind: str, message: str) -> None:
        """告警。前端一般会单列一个告警面板。"""
        self._write({"t": "event", "event": "warning",
                     "data": {"kind": kind, "message": message}})

    def result_ok(self, data: Any = None) -> None:
        """最后一行:成功。"""
        self._write({"t": "result", "ok": True, "data": data})

    def result_err(self, code: str, message: str, **extra: Any) -> None:
        """最后一行:失败。code 是给程序判断的,message 是给人的。"""
        err = {"code": code, "message": message}
        err.update(extra)
        self._write({"t": "result", "ok": False, "error": err})


class NullEmitter(Emitter):
    """把输出全丢掉。给不关心事件、只想要返回值的调用方用。"""

    def _write(self, obj: dict) -> None:
        pass
