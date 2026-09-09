"""身份与访问模型（6 张表）行为测试 — TDD Task 2。

统一走 pg_fresh 夹具（create_all 建表，迁移在 Task 6 才存在）；
断言真实 PostgreSQL 约束行为（CHECK / 部分唯一索引 / FK 级联），不打桩。
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.v2.models import (
    AccountActionToken,
    EmailOutbox,
    Invitation,
    Session,
    User,
    UserEntitlement,
)

pytestmark = [pytest.mark.usefixtures("pg_fresh")]  # 模型测试统一用 pg_fresh（自动 create_all）

EXPIRES_SOON = datetime.now(timezone.utc) + timedelta(hours=1)


async def test_user_roundtrip_with_enum_status(pg_fresh):
    maker = async_sessionmaker(pg_fresh.engine, expire_on_commit=False)
    async with maker() as s:
        s.add(User(email="a@example.com", password_hash="h", role="user", status="pending"))
        await s.commit()
    async with maker() as s:
        row = (await s.execute(select(User))).scalar_one()
        assert row.id.version == 7 and row.status == "pending"


async def test_user_role_and_status_constraints(pg_fresh):
    maker = async_sessionmaker(pg_fresh.engine, expire_on_commit=False)
    async with maker() as s:
        s.add(User(email="b@example.com", password_hash="h", role="root", status="pending"))
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_one_active_entitlement_unique(pg_fresh):
    """同 user 同 entitlement 两行未 revoked → 违反部分唯一索引 one_active_entitlement；
    revoked_at 置值后该行退出谓词范围 → 可再建新 active 行。"""
    maker = async_sessionmaker(pg_fresh.engine, expire_on_commit=False)
    async with maker() as s:
        user = User(email="ent@example.com", password_hash="h", role="user", status="active")
        s.add(user)
        await s.flush()
        s.add(UserEntitlement(user_id=user.id, entitlement="expert_author"))
        await s.commit()
        user_id = user.id
    async with maker() as s:
        s.add(UserEntitlement(user_id=user_id, entitlement="expert_author"))
        with pytest.raises(IntegrityError):
            await s.commit()
    async with maker() as s:
        row = (await s.execute(select(UserEntitlement))).scalar_one()
        row.revoked_at = datetime.now(timezone.utc)
        await s.commit()
    async with maker() as s:
        s.add(UserEntitlement(user_id=user_id, entitlement="expert_author"))
        await s.commit()
        active = (
            await s.execute(select(UserEntitlement).where(UserEntitlement.revoked_at.is_(None)))
        ).scalars().all()
        assert len(active) == 1


async def test_invitation_one_open_email(pg_fresh):
    """同 email 两个未消费未撤销邀请 → 违反部分唯一索引 invitations_one_open_email；
    撤销首个邀请后 → 新邀请可再建。"""
    maker = async_sessionmaker(pg_fresh.engine, expire_on_commit=False)
    async with maker() as s:
        s.add(Invitation(token_hash="a" * 64, email="open@example.com", expires_at=EXPIRES_SOON))
        await s.commit()
    async with maker() as s:
        s.add(Invitation(token_hash="b" * 64, email="open@example.com", expires_at=EXPIRES_SOON))
        with pytest.raises(IntegrityError):
            await s.commit()
    async with maker() as s:
        row = (
            await s.execute(select(Invitation).where(Invitation.token_hash == "a" * 64))
        ).scalar_one()
        row.revoked_at = datetime.now(timezone.utc)
        await s.commit()
    async with maker() as s:
        s.add(Invitation(token_hash="c" * 64, email="open@example.com", expires_at=EXPIRES_SOON))
        await s.commit()


async def test_account_action_token_purpose_enum(pg_fresh):
    """purpose 不在封闭枚举 → CHECK violation；合法枚举值可正常插入。"""
    maker = async_sessionmaker(pg_fresh.engine, expire_on_commit=False)
    async with maker() as s:
        user = User(email="tok@example.com", password_hash="h", role="user", status="pending")
        s.add(user)
        await s.flush()
        s.add(AccountActionToken(
            user_id=user.id, purpose="wrong", token_hash="t" * 64, expires_at=EXPIRES_SOON,
        ))
        with pytest.raises(IntegrityError):
            await s.commit()
    # commit 失败整体回滚（user 未持久化），新会话重建用户后验证合法 purpose
    async with maker() as s:
        user = User(email="tok@example.com", password_hash="h", role="user", status="pending")
        s.add(user)
        await s.flush()
        s.add(AccountActionToken(
            user_id=user.id, purpose="email_verify", token_hash="t" * 64, expires_at=EXPIRES_SOON,
        ))
        await s.commit()


async def test_email_outbox_lease_fields(pg_fresh):
    """state 默认 pending；lease_owner / lease_expires_at 可空；user_id 可空。"""
    maker = async_sessionmaker(pg_fresh.engine, expire_on_commit=False)
    async with maker() as s:
        s.add(EmailOutbox(purpose="invitation", payload_ciphertext="ct"))
        await s.commit()
    async with maker() as s:
        row = (await s.execute(select(EmailOutbox))).scalar_one()
        assert row.state == "pending"
        assert row.lease_owner is None
        assert row.lease_expires_at is None
        assert row.user_id is None
        assert row.attempts == 0


async def test_session_fk_cascade(pg_fresh):
    """删除 user → sessions 随 ondelete=CASCADE 级联消失。"""
    maker = async_sessionmaker(pg_fresh.engine, expire_on_commit=False)
    async with maker() as s:
        user = User(email="sess@example.com", password_hash="h", role="user", status="active")
        s.add(user)
        await s.flush()
        s.add(Session(
            token_hash="s" * 64,
            user_id=user.id,
            csrf_hash="c" * 64,
            expires_at=EXPIRES_SOON,
        ))
        await s.commit()
        user_id = user.id
    async with maker() as s:
        await s.execute(delete(User).where(User.id == user_id))
        await s.commit()
    async with maker() as s:
        remaining = (await s.execute(select(Session))).scalars().all()
        assert remaining == []
