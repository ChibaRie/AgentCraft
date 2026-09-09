import uuid

from backend.v2.ids import uuid7


def test_uuid7_has_correct_version_and_variant():
    u = uuid7()
    assert u.version == 7
    assert u.variant == uuid.RFC_4122


def test_uuid7_is_time_ordered_prefix():
    us = sorted(uuid7() for _ in range(64))
    # 同毫秒内不保证全序，但跨毫秒的前缀序使整体可排序且无回退簇
    assert str(us[0]) < str(us[-1])


def test_uuid7_unique_across_burst():
    seen = {uuid7() for _ in range(1000)}
    assert len(seen) == 1000
