"""目录与配额模型（8 张表）行为测试 — TDD Task 3。

与 Task 2 同模式：pg_fresh 夹具（create_all 建表）+ async_sessionmaker；
断言真实 PostgreSQL 约束行为（CHECK / 主键 / 唯一约束 / 索引反射），不打桩。
"""

from datetime import date

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.v2.models import (
    PROVIDER_STATUSES,
    SLOT_STATES,
    PlatformSlot,
    PlatformStorage,
    ProviderCatalog,
    RateLimitEvent,
    UsageDaily,
    User,
    UserProvider,
    UserQuota,
    UserQuotaUsage,
)

pytestmark = [pytest.mark.usefixtures("pg_fresh")]  # 模型测试统一用 pg_fresh（自动 create_all）


async def _seed_user(maker, email: str):
    """建一个 active 用户并返回其 id（配额/usage/user_providers 的 FK 父行）。"""
    async with maker() as s:
        user = User(email=email, password_hash="h", role="user", status="active")
        s.add(user)
        await s.commit()
        return user.id


async def test_provider_catalog_models_field(pg_fresh):
    """models 为 JSONB list[str]；path_prefix/method/enabled/capabilities 走默认。"""
    maker = async_sessionmaker(pg_fresh.engine, expire_on_commit=False)
    async with maker() as s:
        s.add(ProviderCatalog(
            display_name="OpenAI",
            allowed_host="api.openai.com",
            models=["gpt-4o", "gpt-4o-mini"],
            healthcheck_path="/v1/models",
        ))
        await s.commit()
    async with maker() as s:
        row = (await s.execute(select(ProviderCatalog))).scalar_one()
        assert row.models == ["gpt-4o", "gpt-4o-mini"]
        assert row.path_prefix == "/v1"
        assert row.healthcheck_method == "GET"
        assert row.enabled is True
        assert row.model_capabilities is None


async def test_provider_catalog_model_capabilities_persist(pg_fresh):
    """v0.12.4 接缝：model_capabilities 可空 JSONB，按模型能力如实声明
    （形如 {"gpt-4o": {"input": ["text", "image"]}}；缺失条目由消费方视为纯文本）。"""
    maker = async_sessionmaker(pg_fresh.engine, expire_on_commit=False)
    async with maker() as s:
        s.add(ProviderCatalog(
            display_name="OpenAI Vision",
            allowed_host="api.openai.com",
            models=["gpt-4o", "gpt-4o-mini"],
            model_capabilities={"gpt-4o": {"input": ["text", "image"]}},
            healthcheck_path="/v1/models",
        ))
        await s.commit()
    async with maker() as s:
        row = (await s.execute(select(ProviderCatalog))).scalar_one()
        assert row.model_capabilities == {"gpt-4o": {"input": ["text", "image"]}}


async def test_user_provider_key_fields_and_status_enum(pg_fresh):
    """key_ciphertext/dek_wrapped 非空密文、key_last4 恰 4 字符、key_version=1、
    status 默认 active 且封闭于 active/revoked；is_default 默认 False。"""
    maker = async_sessionmaker(pg_fresh.engine, expire_on_commit=False)
    async with maker() as s:
        user = User(email="byok@example.com", password_hash="h", role="user", status="active")
        s.add(user)
        await s.flush()
        catalog = ProviderCatalog(
            display_name="OpenAI",
            allowed_host="api.openai.com",
            models=["gpt-4o"],
            healthcheck_path="/v1/models",
        )
        s.add(catalog)
        await s.flush()
        s.add(UserProvider(
            user_id=user.id,
            catalog_id=catalog.id,
            model_id="gpt-4o",
            key_ciphertext="ct",
            dek_wrapped="dek",
            key_last4="Ab1!",
        ))
        await s.commit()
        user_id, catalog_id = user.id, catalog.id
    async with maker() as s:
        row = (await s.execute(select(UserProvider))).scalar_one()
        assert row.key_last4 == "Ab1!"
        assert row.key_version == 1
        assert row.status == "active"
        assert row.is_default is False
    # status 封闭枚举（PROVIDER_STATUSES = ("active", "revoked")）
    assert PROVIDER_STATUSES == ("active", "revoked")
    async with maker() as s:
        s.add(UserProvider(
            user_id=user_id, catalog_id=catalog_id, model_id="gpt-4o",
            key_ciphertext="ct", dek_wrapped="dek", key_last4="Xy2@",
            status="expired",
        ))
        with pytest.raises(IntegrityError):
            await s.commit()
    # key_last4 恰 4 字符：3 字符落入 CHECK（ck_user_providers_key_last4_len）
    async with maker() as s:
        s.add(UserProvider(
            user_id=user_id, catalog_id=catalog_id, model_id="gpt-4o",
            key_ciphertext="ct", dek_wrapped="dek", key_last4="abc",
        ))
        with pytest.raises(IntegrityError):
            await s.commit()
    # 5 字符先撞 varchar(4) 宽度（PG 22001 超长，asyncpg 侧不归一为 IntegrityError/DataError）
    async with maker() as s:
        s.add(UserProvider(
            user_id=user_id, catalog_id=catalog_id, model_id="gpt-4o",
            key_ciphertext="ct", dek_wrapped="dek", key_last4="abcde",
        ))
        with pytest.raises(DBAPIError) as excinfo:
            await s.commit()
    assert "value too long for type character varying(4)" in str(excinfo.value)


async def test_user_quota_defaults_match_prd(pg_fresh):
    """配额默认值 5 / 3 / 1 / 1GiB=1073741824。"""
    maker = async_sessionmaker(pg_fresh.engine, expire_on_commit=False)
    user_id = await _seed_user(maker, "quota@example.com")
    async with maker() as s:
        s.add(UserQuota(user_id=user_id))
        await s.commit()
    async with maker() as s:
        row = await s.get(UserQuota, user_id)
        assert row.max_daily_tasks == 5
        assert row.max_active_tasks == 3
        assert row.max_running_tasks == 1
        assert row.max_retained_storage_bytes == 1_073_741_824


async def test_quota_usage_defaults_zero_and_pk_user_id(pg_fresh):
    """计数器默认全 0；user_id 即主键——同用户第二行被拒。"""
    maker = async_sessionmaker(pg_fresh.engine, expire_on_commit=False)
    user_id = await _seed_user(maker, "usage@example.com")
    async with maker() as s:
        s.add(UserQuotaUsage(user_id=user_id))
        await s.commit()
    async with maker() as s:
        row = await s.get(UserQuotaUsage, user_id)
        assert row.active_tasks == 0
        assert row.running_tasks == 0
        assert row.retained_storage_bytes == 0
    async with maker() as s:
        s.add(UserQuotaUsage(user_id=user_id))
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_platform_slot_state_enum_and_slot_no_pk(pg_fresh):
    """state 默认 free 且封闭于 free/leased；slot_no 为主键（重复槽位号被拒）；
    租约扫描索引 ix_platform_slots_state_leased_until (state, leased_until) 真实存在。"""
    maker = async_sessionmaker(pg_fresh.engine, expire_on_commit=False)
    assert SLOT_STATES == ("free", "leased")
    async with maker() as s:
        s.add(PlatformSlot(slot_no=1))
        await s.commit()
    async with maker() as s:
        row = await s.get(PlatformSlot, 1)
        assert row.state == "free"
        assert row.leased_until is None
        assert row.task_id is None
    # 枚举另一合法值 leased 可插入
    async with maker() as s:
        s.add(PlatformSlot(slot_no=2, state="leased"))
        await s.commit()
    async with maker() as s:
        row = await s.get(PlatformSlot, 2)
        assert row.state == "leased"
    # state 封闭枚举：busy 被拒
    async with maker() as s:
        s.add(PlatformSlot(slot_no=3, state="busy"))
        with pytest.raises(IntegrityError):
            await s.commit()
    # slot_no 主键：重复槽位号被拒
    async with maker() as s:
        s.add(PlatformSlot(slot_no=1))
        with pytest.raises(IntegrityError):
            await s.commit()
    async with pg_fresh.engine.connect() as conn:
        indexes = await conn.run_sync(lambda c: inspect(c).get_indexes("platform_slots"))
    idx = {i["name"]: i["column_names"] for i in indexes}
    assert idx["ix_platform_slots_state_leased_until"] == ["state", "leased_until"]


async def test_platform_storage_singleton_row_shape(pg_fresh):
    """singleton=True 即主键——全局仅一行；retained 默认 0，max 默认 60GiB=64424509440。"""
    maker = async_sessionmaker(pg_fresh.engine, expire_on_commit=False)
    async with maker() as s:
        s.add(PlatformStorage())
        await s.commit()
    async with maker() as s:
        row = (await s.execute(select(PlatformStorage))).scalar_one()
        assert row.singleton is True
        assert row.retained_storage_bytes == 0
        assert row.max_retained_storage_bytes == 64_424_509_440
    async with maker() as s:
        s.add(PlatformStorage())
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_usage_daily_unique_per_user_day(pg_fresh):
    """同 (user_id, day) 两行违反 uq_usage_daily_user_day → IntegrityError；不同 day 可再建。"""
    maker = async_sessionmaker(pg_fresh.engine, expire_on_commit=False)
    user_id = await _seed_user(maker, "daily@example.com")
    async with maker() as s:
        s.add(UsageDaily(user_id=user_id, day=date(2026, 9, 9)))
        await s.commit()
    async with maker() as s:
        row = (await s.execute(select(UsageDaily))).scalar_one()
        assert row.tasks_started == 0
    async with maker() as s:
        s.add(UsageDaily(user_id=user_id, day=date(2026, 9, 9)))
        with pytest.raises(IntegrityError):
            await s.commit()
    async with maker() as s:
        s.add(UsageDaily(user_id=user_id, day=date(2026, 9, 10)))
        await s.commit()


async def test_rate_limit_event_index_exists(pg_fresh):
    """反射检查 ix_rate_limit_scope_subject_time (scope, subject_hash, occurred_at) 存在；
    occurred_at 由 server_default=now() 填充。"""
    maker = async_sessionmaker(pg_fresh.engine, expire_on_commit=False)
    async with maker() as s:
        s.add(RateLimitEvent(scope="auth.login", subject_hash="a" * 64))
        await s.commit()
    async with maker() as s:
        row = (await s.execute(select(RateLimitEvent))).scalar_one()
        assert row.occurred_at is not None
    async with pg_fresh.engine.connect() as conn:
        indexes = await conn.run_sync(lambda c: inspect(c).get_indexes("rate_limit_events"))
    idx = {i["name"]: i["column_names"] for i in indexes}
    assert idx["ix_rate_limit_scope_subject_time"] == ["scope", "subject_hash", "occurred_at"]
