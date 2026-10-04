
"""图片格式处理的异常。

单独一个文件,是为了让各格式模块(jpeg/png/webp)不必反向依赖 packet。
"""

from __future__ import annotations


class XmpError(Exception):
    """基础异常。"""


class XmpUnsupported(XmpError):
    """该文件不适合嵌入 —— 调用方应改用 _tags.json 边车。"""


class XmpVerifyError(XmpError):
    """拼接后校验不通过。绝不能把这种文件放出去。"""
