"""UUIDv7（RFC 9562）：48bit unix_ms + ver7 + rand_a(12) + var10 + rand_b(62)。"""

import os
import time
import uuid


def uuid7() -> uuid.UUID:
    ts = time.time_ns() // 1_000_000 & ((1 << 48) - 1)
    r = int.from_bytes(os.urandom(10), "big")  # 80 bits
    value = (ts << 80) | (0x7 << 76) | ((r >> 68) << 64) | (0x2 << 62) | (r & ((1 << 62) - 1))
    return uuid.UUID(int=value)
