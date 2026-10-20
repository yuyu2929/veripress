"""规范化 JSON 与内容寻址。

所有被签名的内容必须先经过 canonical() 序列化——否则同一份数据
不同语言/库序列化出的字节不同，签名就无法跨端验证。
规则：键排序、无空白、UTF-8、ensure_ascii=False。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical(obj: Any) -> bytes:
    """规范化序列化为 bytes（签名的唯一输入形态）。

    allow_nan=False：Python 的 json 默认会把 NaN/Infinity 写成 `NaN`，
    那不是合法 JSON，别的语言解析不了，同一份内容在两端会得到不同的哈希。
    宁可当场报错，也不要签出一个只有本机能验证的名字。
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def content_hash(obj: Any) -> str:
    """内容的 SHA-256 寻址哈希（hex）。"""
    return hashlib.sha256(canonical(obj)).hexdigest()


def short(h: str) -> str:
    """哈希短显示形式。"""
    return h[:12] if h else ""
