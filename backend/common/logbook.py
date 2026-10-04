"""追加式问题日志。

用户要求把「问题 + 位置」记到一个单独的 log 文件里,而不是只打到控制台 ——
控制台滚过去就没了,而名称冲突这种事需要事后能翻。

两条硬性约束:
  - 用 UTF-8 显式打开。合集名是用户起的,中日文是常态;不指定编码的话
    Windows 上会走 ANSI 代码页,写日志那一刻抛 UnicodeEncodeError,
    把整轮处理打断。
  - 写日志失败绝不能变成致命错误。日志是旁路,不该有能力搞挂主流程。
"""

from __future__ import annotations

import unicodedata
from datetime import datetime

from backend import config

_TAG_WIDTH = 10  # 标签列的对齐宽度(按显示宽度算)

# 当前 emitter,由入口 bind() 注入。不注入时告警只写文件、不发事件 ——
# 这样单独 import 本模块(比如跑自检)也不会因为没人接收而炸。
_emitter = None


def bind(emitter) -> None:
    """把 emitter 接进来。入口在分发命令前调用一次。"""
    global _emitter
    _emitter = emitter


def _display_width(text: str) -> int:
    """按终端显示宽度算长度:中日韩全角字符占两列。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _line(kind: str, message: str) -> str:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pad = " " * max(0, _TAG_WIDTH - _display_width(kind))
    return f"{stamp}  [{kind}]{pad} {message}\n"


def _write(text: str) -> None:
    try:
        config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(config.COLLECTION_LOG, "a", encoding="utf-8", errors="replace") as f:
            f.write(text)
    except OSError:
        pass  # 日志写不进去就算了,不能因此中断处理


def record(kind: str, message: str) -> None:
    """只写日志(供跳过项、目录改名这类不需要打扰用户的事件)。"""
    _write(_line(kind, message))


def record_warn(kind: str, message: str) -> None:
    """写日志 + 发 warning 事件(名称冲突、失败、残留这类需要立刻看见的)。

    注意这里是 `print` 的替代品 —— IPC 模式下 stdout 是协议通道,
    往那儿打一行中文会把前端的 JSON 解析搞崩。所以走 emitter。
    """
    _write(_line(kind, message))
    if _emitter is not None:
        _emitter.warn(kind, message)
