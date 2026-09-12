"""内容规范化哈希（裁决 D8）：canonical JSON + SHA-256 + 64KiB 上限。

canonical 与幂等 request_hash 同型（idempotency.py:82 先例）：键排序 + 紧凑分隔符 +
ensure_ascii=False，显式 UTF-8 编码；不引入 unicode 归一（避免与前端展示不一致）。
hash 覆盖 content_json 全量（DB §1:12「审核永不只绑定可变实体 ID」、Eng §5.4:147
「审核结论仅对完全相同的 revision hash 生效」依赖此语义）。请求载荷永不携带 hash
字段——提交侧由本模块计算（S2 §2：防伪造绑定）。
"""

import hashlib
import json

MAX_CONTENT_BYTES = 64 * 1024  # 裁决 D8：canonical 总大小上限（64KiB）


class ContentTooLarge(ValueError):
    """canonical 序列化后超出大小上限（服务层转 400 VALIDATION_ERROR）。"""


def canonical_json(content: dict) -> str:
    return json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_sha256(content: dict) -> str:
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


def assert_content_size(content: dict) -> None:
    size = len(canonical_json(content).encode("utf-8"))
    if size > MAX_CONTENT_BYTES:
        raise ContentTooLarge(f"内容规范化后 {size} 字节，超出上限 {MAX_CONTENT_BYTES} 字节")
