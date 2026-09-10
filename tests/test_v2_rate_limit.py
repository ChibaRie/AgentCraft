"""持久化滑动窗口限流测试（Task 6）：窗口边界、滑动恢复、HMAC 稳定、组合维度、scope 隔离。

契约出处：API Supplement §7 + task-6-brief。

- DB 行为测试一律走 app role 会话（rate_limit_events 无 RLS，role 保真为本仓纪律）；
  enforce 自管事务并**独立提交**（Task 9 用法：enforce 在业务事务之前独立提交），
  故测试以裸会话直接调用，不套 begin() 块。
- subject_hash 解耦策略：enforce 只接收已哈希主体，DB 行为测试用 hash_token
  派生主体即可，完全不触碰 HMAC 密钥。
- HMAC 密钥注入：测试进程不配置真实 RATE_LIMIT_HMAC_KEY（conftest 仅设
  ALLOW_INSECURE_SECRETS=true，V1-only 模式下该键默认空串）；hmac_subject 用例经
  monkeypatch.setenv 注入一次性 b64url 32B 密钥材料——env var 优先级高于 .env
  （pydantic-settings 语义），Settings() 每次现读，注入即时生效且用例间互不污染。
"""

import asyncio
import base64
import hashlib
import hmac
import re

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text

from backend.v2 import rate_limit
from backend.v2.db import build_engine, session_factory
from backend.v2.models import RateLimitEvent
from backend.v2.rate_limit import LIMITS, enforce, hmac_subject
from backend.v2.security import hash_token
from tests.conftest import APP_ROLE, PgDb
from tests.test_v2_runtime import make_v2_runtime

# 32 字节确定性密钥材料（b64url 43 字符）；仅测试注入，非任何真实密钥
_KEY_MATERIAL = base64.urlsafe_b64encode(bytes(range(32))).decode()


def _subject(tag: str) -> str:
    """DB 行为测试的主体派生（enforce 契约只要求已哈希 64 位串，与 HMAC 密钥无关）。"""
    return hash_token(f"rl-{tag}")


@pytest.fixture
def hmac_key(monkeypatch):
    """注入 RATE_LIMIT_HMAC_KEY 环境变量（用例结束自动还原）。"""
    monkeypatch.setenv("RATE_LIMIT_HMAC_KEY", _KEY_MATERIAL)
    return _KEY_MATERIAL


@pytest.fixture
async def v2_runtime(pg: PgDb):
    """app/admin 双 role 运行时（复用 test_v2_runtime 构建器；不挂 FastAPI override）。"""
    rt = make_v2_runtime(pg)
    yield rt
    rt.close()


async def _count(pg: PgDb, scope: str | None = None, subject_hash: str | None = None) -> int:
    """superuser 直查计数（绕开被测会话状态；rate_limit_events 无 RLS）。"""
    stmt = select(func.count()).select_from(RateLimitEvent)
    if scope is not None:
        stmt = stmt.where(RateLimitEvent.scope == scope)
    if subject_hash is not None:
        stmt = stmt.where(RateLimitEvent.subject_hash == subject_hash)
    async with pg.engine.connect() as conn:
        return (await conn.execute(stmt)).scalar_one()


# ---------- LIMITS 注册表（Phase 2 全量 scope 钉死）----------


def test_limits_registry_pins_phase2_scopes():
    assert LIMITS == {
        "login": (10, 900),
        "invitation_accept": (5, 3600),
        "email_verify_resend": (3, 3600),
        "password_reset_request": (3, 3600),
        "deletion_cancel_invalid": (10, 3600),
        "deletion_cancel_attempt": (10, 3600),
        "mfa_failure": (10, 900),
        "password_change_totp": (5, 900),
        "provider_test": (10, 3600),
    }


# ---------- hmac_subject（纯函数；密钥经环境变量注入）----------


def test_hmac_subject_matches_hmac_sha256_reference(hmac_key):
    """钉死原语：HMAC-SHA256(key, msg=f"{kind}:{value}" utf-8) hexdigest。"""
    key = base64.urlsafe_b64decode(_KEY_MATERIAL)
    expected = hmac.new(key, b"email:user@example.com", hashlib.sha256).hexdigest()
    assert hmac_subject("email", "user@example.com") == expected


def test_hmac_subject_is_stable_and_distinct(hmac_key):
    a1 = hmac_subject("email", "user@example.com")
    # 稳定：同输入同输出（64 位小写 hex，入库 String(64)）
    assert a1 == hmac_subject("email", "user@example.com")
    assert re.fullmatch(r"[0-9a-f]{64}", a1)
    # 区分度：值不同 → 哈希不同；kind 不同 → 哈希不同（明文不可由哈希还原）
    assert hmac_subject("email", "other@example.com") != a1
    assert hmac_subject("ip", "user@example.com") != a1
    assert hmac_subject("user", "user@example.com") != a1


def test_hmac_subject_rejects_unknown_kind(hmac_key):
    with pytest.raises(ValueError):
        hmac_subject("token", "whatever")  # kind 仅限 email/ip/user


@pytest.mark.parametrize(
    "raw",
    [
        "",  # 未配置（V1-only 测试环境的默认态）
        "not-b64url!!!",  # 乱串：解码结果 ≠ 32 字节
        base64.urlsafe_b64encode(b"too-short").decode(),  # 9 字节 ≠ 32
    ],
)
def test_hmac_subject_rejects_bad_key_material(monkeypatch, raw):
    monkeypatch.setenv("RATE_LIMIT_HMAC_KEY", raw)
    with pytest.raises(ValueError):
        hmac_subject("email", "user@example.com")


# ---------- enforce：DB 行为（app role 会话；enforce 自管事务）----------


async def test_window_boundary_allows_limit_then_429_with_retry_after(pg: PgDb, v2_runtime):
    """第 limit 次内放行、第 limit+1 次 429；Retry-After 存在且 >= 1；拒绝路径不写行。"""
    s = _subject("boundary")
    async with v2_runtime.app_factory() as db:
        for _ in range(10):  # login=(10, 900)
            await enforce(db, scope="login", subjects=[s])  # enforce 独立提交
    assert await _count(pg, "login") == 10

    with pytest.raises(HTTPException) as exc_info:
        async with v2_runtime.app_factory() as db:
            await enforce(db, scope="login", subjects=[s])
    exc = exc_info.value
    assert exc.status_code == 429
    assert exc.detail == {"code": "TOO_MANY_REQUESTS", "message": "请求过于频繁，请稍后重试"}
    retry_after = int(exc.headers["Retry-After"])
    assert 1 <= retry_after <= 900
    # 拒绝不计事件：仍为 10 行（不写第 11 行）
    assert await _count(pg, "login") == 10


async def test_sliding_recovery_after_window_expiry(pg: PgDb, v2_runtime):
    """窗口滑走后放行；机会主义 DELETE 清掉该 scope 窗口外旧行。

    occurred_at 经 superuser 直接 SQL 拨动（bound params），不 sleep。
    """
    s = _subject("slide")
    async with v2_runtime.app_factory() as db:
        for _ in range(3):  # email_verify_resend=(3, 3600)
            await enforce(db, scope="email_verify_resend", subjects=[s])
    with pytest.raises(HTTPException):
        async with v2_runtime.app_factory() as db:
            await enforce(db, scope="email_verify_resend", subjects=[s])

    # 拨到窗口外（now - 3601s > 窗口 3600s）
    async with pg.engine.begin() as conn:
        result = await conn.execute(
            text(
                "UPDATE rate_limit_events SET occurred_at = now() - make_interval(secs => :age) "
                "WHERE scope = :scope"
            ),
            {"age": 3601, "scope": "email_verify_resend"},
        )
        assert result.rowcount == 3

    # 滑动恢复：重新放行
    async with v2_runtime.app_factory() as db:
        await enforce(db, scope="email_verify_resend", subjects=[s])  # 不抛 429

    # 机会主义清理：3 行旧行已删，只剩本请求新写入的 1 行
    assert await _count(pg, "email_verify_resend") == 1


async def test_combined_subjects_write_one_row_per_subject(pg: PgDb, v2_runtime):
    """组合维度一次调用每 subject 各写一行；任一 subject 达限即整组 429（ANY 语义）。"""
    email_h = _subject("combined-email")
    ip_h = _subject("combined-ip")
    async with v2_runtime.app_factory() as db:
        await enforce(db, scope="login", subjects=[email_h, ip_h])
    assert await _count(pg, "login", email_h) == 1
    assert await _count(pg, "login", ip_h) == 1
    assert await _count(pg, "login") == 2

    # email_h 补满到 10 次（login 限值）
    async with v2_runtime.app_factory() as db:
        for _ in range(9):
            await enforce(db, scope="login", subjects=[email_h])
    assert await _count(pg, "login", email_h) == 10

    # 携带全新 ip 的组合请求仍被 email_h 拖入 429，且 fresh ip 不落行
    fresh_ip = _subject("fresh-ip")
    with pytest.raises(HTTPException) as exc_info:
        async with v2_runtime.app_factory() as db:
            await enforce(db, scope="login", subjects=[fresh_ip, email_h])
    assert exc_info.value.status_code == 429
    assert await _count(pg, "login", fresh_ip) == 0
    assert await _count(pg, "login") == 11  # 10 + ip 首行，拒绝未追加


async def test_scope_isolation(pg: PgDb, v2_runtime):
    """login 达限不影响其他 scope 计数（scope 维度完全隔离）。"""
    s = _subject("iso")
    async with v2_runtime.app_factory() as db:
        for _ in range(10):
            await enforce(db, scope="login", subjects=[s])
    with pytest.raises(HTTPException):
        async with v2_runtime.app_factory() as db:
            await enforce(db, scope="login", subjects=[s])
    # 另一 scope 同主体照常放行并落行
    async with v2_runtime.app_factory() as db:
        await enforce(db, scope="invitation_accept", subjects=[s])
    assert await _count(pg, "invitation_accept") == 1


async def test_enforce_rejects_unregistered_scope(pg: PgDb, v2_runtime):
    """未注册 scope（含 Phase 3+ 占位名）拒绝服务而非静默放行。"""
    async with v2_runtime.app_factory() as db:
        with pytest.raises(ValueError):
            await enforce(db, scope="task_create", subjects=[_subject("ph")])
    assert await _count(pg, "task_create") == 0


async def test_enforce_rejects_empty_subjects(pg: PgDb, v2_runtime):
    """空 subjects 是调用方编程错误：拒绝而非写 0 行假成功。"""
    async with v2_runtime.app_factory() as db:
        with pytest.raises(ValueError):
            await enforce(db, scope="login", subjects=[])
    assert await _count(pg, "login") == 0


# ---------- 并发串行化（review round 1 回归点）----------


async def test_concurrent_same_subject_exactly_one_pass(pg: PgDb, monkeypatch):
    """同主体并发 enforce 恰好一胜一 429，且该主体只落 1 行。

    修复前 count-then-insert 竞态可令并发首请求双胜（两连接共享 0 行计数，
    Eng §1.4 违例）；修复后咨询锁串行化使后进者必然看见先进者已提交的事件行。
    两会话各自建引擎/建连（独立 TCP 连接，无池内复用干扰）。
    """
    monkeypatch.setitem(rate_limit.LIMITS, "login", (1, 900))
    subject = _subject("concurrent")

    engines = [build_engine(pg.role_url(*APP_ROLE)) for _ in range(2)]
    factories = [session_factory(eng) for eng in engines]

    async def _attempt(factory) -> str:
        async with factory() as db:
            try:
                await enforce(db, scope="login", subjects=[subject])
            except HTTPException as exc:
                return "rejected" if exc.status_code == 429 else f"unexpected-{exc.status_code}"
            else:
                return "ok"

    try:
        outcomes = list(await asyncio.gather(_attempt(factories[0]), _attempt(factories[1])))
    finally:
        for eng in engines:
            await eng.dispose()

    assert sorted(outcomes) == ["ok", "rejected"]
    assert await _count(pg, "login", subject) == 1
