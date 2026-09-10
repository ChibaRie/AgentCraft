"""幂等引擎测试（Task 5）：凭据脱敏哈希、重放/冲突/过期清理、表头校验、事务契约。

契约出处：API Supplement §7 + task-5-brief。subject_user/subject_token 为纯哈希
派生（idempotency_records 无外键），DB 场景无需种子 users 行；幂等行为测试一律
走 app role 会话（runtime.app_factory）——该表无 RLS，但 role 保真为本仓纪律。
"""

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, update

from backend.errors import AgentCraftError, ErrorCode
from backend.v2 import idempotency
from backend.v2.idempotency import (
    CREDENTIAL_FIELDS,
    request_hash,
    require_key_header,
    subject_token,
    subject_user,
)
from backend.v2.ids import uuid7
from backend.v2.models import IdempotencyRecord
from backend.v2.security import hash_token
from tests.conftest import PgDb
from tests.test_v2_runtime import make_v2_runtime

ROUTE = "POST /api/v2/auth/register"


def _subject() -> str:
    """随机 subject（纯哈希派生，无需 users 行）。"""
    return subject_user(uuid7())


@pytest.fixture
async def v2_runtime(pg: PgDb):
    """app/admin 双 role 运行时（复用 test_v2_runtime 构建器；不挂 FastAPI override）。"""
    rt = make_v2_runtime(pg)
    yield rt
    rt.close()


def _starlette_request(headers: list[tuple[str, str]]):
    from starlette.requests import Request as StarletteRequest

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v2/auth/register",
        "headers": [(k.lower().encode("ascii"), v.encode("utf-8")) for k, v in headers],
        "query_string": b"",
    }
    return StarletteRequest(scope)


# ---------- 纯函数：凭据脱敏与规范化哈希 ----------


async def test_credential_fields_are_excluded_from_hash():
    assert idempotency.request_hash({"password": "a", "email": "x@y.z"}) == (
        idempotency.request_hash({"password": "b", "email": "x@y.z"})
    )
    assert idempotency.request_hash({"email": "x@y.z"}) != (
        idempotency.request_hash({"email": "other@y.z"})
    )


def test_request_hash_is_canonical_and_deterministic():
    # 键序无关（sort_keys）
    assert request_hash({"a": 1, "b": 2}) == request_hash({"b": 2, "a": 1})
    # None → 规范化 "null" 参与哈希
    assert request_hash(None) == hashlib.sha256(b"null").hexdigest()
    # ensure_ascii=False：非 ASCII 按 UTF-8 字节而非 \u 转义参与哈希
    assert request_hash({"note": "中文"}) == hashlib.sha256('{"note":"中文"}'.encode()).hexdigest()
    # 确定性：同 payload 两次哈希一致
    assert request_hash({"x": 1}) == request_hash({"x": 1})
    # 全部凭据字段同占位符——凭据值永不参与哈希
    for field in CREDENTIAL_FIELDS:
        assert request_hash({field: "secret-A"}) == request_hash({field: "secret-B"})


def test_subject_helpers_hash_server_side_inputs():
    uid = uuid7()
    assert subject_user(uid) == hash_token(str(uid))
    assert subject_token("tok-abc") == hash_token("tok-abc")
    # 不同 subject 派生不同哈希
    assert subject_user(uuid7()) != subject_user(uuid7())


# ---------- require_key_header（FastAPI 依赖，直构 Request）----------


def test_require_key_header_missing_rejected():
    for headers in ([], [("Idempotency-Key", "")]):  # 缺失与空串同判
        with pytest.raises(HTTPException) as exc_info:
            require_key_header(_starlette_request(headers))
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == {
        "code": "VALIDATION_ERROR",
        "message": "缺少 Idempotency-Key 头",
    }


def test_require_key_header_over_100_chars_rejected():
    # key 列 VARCHAR(100)：入库前拒绝，防 22001 溢出变 500
    with pytest.raises(HTTPException) as exc_info:
        require_key_header(_starlette_request([("Idempotency-Key", "k" * 101)]))
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["code"] == "VALIDATION_ERROR"
    assert "100" in exc_info.value.detail["message"]


def test_require_key_header_returns_key_within_cap():
    assert require_key_header(_starlette_request([("Idempotency-Key", "k" * 100)])) == "k" * 100


# ---------- DB 行为（app role 会话；idempotency_records 无 RLS）----------


async def test_replay_regardless_of_state(pg: PgDb, v2_runtime):
    """命中即原样重放 response——与业务状态无关（此处无需任何业务行）。"""
    subject = _subject()
    req = request_hash({"email": "x@y.z", "password": "p"})
    response = {"id": "11111111-1111-7111-8111-111111111111", "status": "created"}

    async with v2_runtime.app_factory() as db:
        async with db.begin():
            first = await idempotency.begin(
                db,
                subject_hash=subject,
                route=ROUTE,
                key="k-replay",
                req_hash=req,
                own_transaction=False,  # 调用方事务内使用（旧嵌入形态）
            )
            assert first is None
            await idempotency.store(
                db,
                subject_hash=subject,
                route=ROUTE,
                key="k-replay",
                req_hash=req,
                status_code=201,
                response_json=response,
            )

    async with v2_runtime.app_factory() as db:
        async with db.begin():
            replay = await idempotency.begin(
                db,
                subject_hash=subject,
                route=ROUTE,
                key="k-replay",
                req_hash=req,
                own_transaction=False,
            )
            assert replay == {"status_code": 201, "response_json": response}


async def test_credential_only_difference_replays(pg: PgDb, v2_runtime):
    """同 key 不同 password → 哈希一致 → 重放不冲突（凭据脱敏的 DB 级证据）。"""
    subject = _subject()
    async with v2_runtime.app_factory() as db:
        async with db.begin():
            await idempotency.store(
                db,
                subject_hash=subject,
                route=ROUTE,
                key="k-cred",
                req_hash=request_hash({"email": "x@y.z", "password": "first"}),
                status_code=200,
                response_json={"ok": True},
            )
            replay = await idempotency.begin(
                db,
                subject_hash=subject,
                route=ROUTE,
                key="k-cred",
                req_hash=request_hash({"email": "x@y.z", "password": "changed"}),
                own_transaction=False,
            )
            assert replay is not None
            assert replay["response_json"] == {"ok": True}


async def test_conflict_on_different_body(pg: PgDb, v2_runtime):
    """同 (subject, route, key) 非凭据字段不同 → 409 IDEMPOTENCY_CONFLICT。"""
    subject = _subject()
    async with v2_runtime.app_factory() as db:
        async with db.begin():
            await idempotency.store(
                db,
                subject_hash=subject,
                route=ROUTE,
                key="k-conflict",
                req_hash=request_hash({"email": "x@y.z"}),
                status_code=200,
                response_json={"ok": True},
            )
            with pytest.raises(AgentCraftError) as exc_info:
                await idempotency.begin(
                    db,
                    subject_hash=subject,
                    route=ROUTE,
                    key="k-conflict",
                    req_hash=request_hash({"email": "other@y.z"}),
                    own_transaction=False,
                )
    assert exc_info.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert exc_info.value.http_status == 409


async def test_expired_row_not_replayed_and_cleaned(pg: PgDb, v2_runtime):
    """过期行不复用（即使 request_hash 一致）：begin 未命中且同事务清理过期行。

    final review 起生产形态的 begin 自持提交式事务（own_transaction=True 默认）；
    本测试保留显式 ``db.begin()`` 嵌入形态，故传 ``own_transaction=False``——
    清理 DELETE 留在调用方事务，随该块 commit 收口（清理持久性由
    test_begin_own_transaction_persists_expired_cleanup 单独钉死）。
    """
    subject = _subject()
    req = request_hash({"email": "x@y.z"})
    async with v2_runtime.app_factory() as db:
        async with db.begin():
            await idempotency.store(
                db,
                subject_hash=subject,
                route=ROUTE,
                key="k-expired",
                req_hash=req,
                status_code=200,
                response_json={"ok": True},
            )

    # 人为把 expires_at 拨到过去
    async with v2_runtime.app_factory() as db:
        async with db.begin():
            result = await db.execute(
                update(IdempotencyRecord)
                .where(IdempotencyRecord.key == "k-expired")
                .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
            )
            assert result.rowcount == 1

    async with v2_runtime.app_factory() as db:
        async with db.begin():
            replay = await idempotency.begin(
                db,
                subject_hash=subject,
                route=ROUTE,
                key="k-expired",
                req_hash=req,
                own_transaction=False,
            )
            assert replay is None  # 未命中 → 重新执行（本测试即断言不重放）

    # 机会主义清理：过期行已被同事务 DELETE
    async with v2_runtime.app_factory() as db:
        n = (
            await db.execute(
                select(func.count())
                .select_from(IdempotencyRecord)
                .where(IdempotencyRecord.key == "k-expired")
            )
        ).scalar_one()
    assert n == 0


async def test_store_does_not_commit_and_unique_violation_conflicts(pg: PgDb, v2_runtime):
    """store 不 commit（随调用方事务收口）；同 key 二次 store → 唯一索引 → 409。

    IntegrityError 后会话处于 pending-rollback：异常必须穿出事务块（由
    begin() 块自动回滚），故 pytest.raises 包住整个 with 而非块内捕。
    """
    subject = _subject()
    req = request_hash({"email": "x@y.z"})
    req_other = request_hash({"email": "other@y.z"})

    async with v2_runtime.app_factory() as db:
        async with db.begin():
            await idempotency.store(
                db,
                subject_hash=subject,
                route=ROUTE,
                key="k-tx",
                req_hash=req,
                status_code=200,
                response_json={"n": 1},
            )
            # store 不 commit：未提交数据对另一连接不可见（READ COMMITTED）
            async with v2_runtime.app_factory() as other:
                n = (
                    await other.execute(
                        select(func.count())
                        .select_from(IdempotencyRecord)
                        .where(IdempotencyRecord.key == "k-tx")
                    )
                ).scalar_one()
                assert n == 0

    async with v2_runtime.app_factory() as db:
        n = (
            await db.execute(
                select(func.count())
                .select_from(IdempotencyRecord)
                .where(IdempotencyRecord.key == "k-tx")
            )
        ).scalar_one()
    assert n == 1  # 随调用方 begin() 块提交落库

    # 已提交后并发同 key 的失败方：flush 触发唯一索引 IntegrityError → 转 409
    with pytest.raises(AgentCraftError) as exc_info:
        async with v2_runtime.app_factory() as db:
            async with db.begin():
                await idempotency.store(
                    db,
                    subject_hash=subject,
                    route=ROUTE,
                    key="k-tx",
                    req_hash=req_other,  # 与 req 无关：唯一索引按 (subject, route, key)
                    status_code=200,
                    response_json={"n": 2},
                )
    assert exc_info.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert exc_info.value.http_status == 409

    # 原 response 未被覆盖，仍可重放
    async with v2_runtime.app_factory() as db:
        async with db.begin():
            replay = await idempotency.begin(
                db,
                subject_hash=subject,
                route=ROUTE,
                key="k-tx",
                req_hash=req,
                own_transaction=False,
            )
            assert replay == {"status_code": 200, "response_json": {"n": 1}}


# ---------- final review 修复：清理持久化 + store 过期冲突自愈 ----------


async def test_begin_own_transaction_persists_expired_cleanup(pg: PgDb, v2_runtime):
    """生产形态（专用会话、无显式 begin 块）调用 begin：未命中路径的机会主义清理
    随 begin 自持的提交式事务落库。

    修复前 begin 的 DELETE 依赖会话关闭时的隐式收口，而专用会话 autobegin 的
    事务在 ``app_factory`` 上下文退出时被 ROLLBACK——清理从不持久，过期行永久
    残留。本测试在生产调用形态下断言过期行真的从 DB 消失（修复前此处 n == 1）。
    """
    subject = _subject()
    req = request_hash({"email": "x@y.z"})
    async with v2_runtime.app_factory() as db:
        async with db.begin():
            await idempotency.store(
                db,
                subject_hash=subject,
                route=ROUTE,
                key="k-prod-cleanup",
                req_hash=req,
                status_code=200,
                response_json={"ok": True},
            )

    # 人为把 expires_at 拨到过去（独立已提交事务）
    async with v2_runtime.app_factory() as db:
        async with db.begin():
            result = await db.execute(
                update(IdempotencyRecord)
                .where(IdempotencyRecord.key == "k-prod-cleanup")
                .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
            )
            assert result.rowcount == 1

    # 生产形态：专用会话、无显式 begin() 块——begin 自持提交式事务
    async with v2_runtime.app_factory() as db:
        replay = await idempotency.begin(
            db, subject_hash=subject, route=ROUTE, key="k-prod-cleanup", req_hash=req
        )
        assert replay is None  # 已过期 → 未命中（不重放）

    # 清理已持久：过期行真的从 DB 消失
    async with v2_runtime.app_factory() as db:
        n = (
            await db.execute(
                select(func.count())
                .select_from(IdempotencyRecord)
                .where(IdempotencyRecord.key == "k-prod-cleanup")
            )
        ).scalar_one()
    assert n == 0


async def test_store_self_heals_expired_conflict(pg: PgDb, v2_runtime):
    """store 的同事务 belt-and-braces：INSERT 前清扫本 (subject, route, key) 的
    过期残留行——INSERT 成功替换，绝不 409。

    修复前：过期行残留时 store 的 flush 命中唯一索引 ``idempotency_route_key``
    → 409 IDEMPOTENCY_CONFLICT 永久化（重放窗口过期后同 key 永不可再用）。本
    流程刻意不先调 begin（覆盖 begin 纪律失效的场景），清扫只能由 store 自己完成。
    """
    subject = _subject()
    req = request_hash({"email": "x@y.z"})
    async with v2_runtime.app_factory() as db:
        async with db.begin():
            await idempotency.store(
                db,
                subject_hash=subject,
                route=ROUTE,
                key="k-selfheal",
                req_hash=req,
                status_code=200,
                response_json={"old": True},
            )

    # 人为把 expires_at 拨到过去（独立已提交事务）
    async with v2_runtime.app_factory() as db:
        async with db.begin():
            result = await db.execute(
                update(IdempotencyRecord)
                .where(IdempotencyRecord.key == "k-selfheal")
                .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
            )
            assert result.rowcount == 1

    # 业务形态：全新事务内直接 store 同 (subject, route, key) → 成功（不 409）
    async with v2_runtime.app_factory() as db:
        async with db.begin():
            await idempotency.store(
                db,
                subject_hash=subject,
                route=ROUTE,
                key="k-selfheal",
                req_hash=req,
                status_code=201,
                response_json={"new": True},
            )

    # 过期行被替换：仅剩新记录，载荷为新值
    async with v2_runtime.app_factory() as db:
        rows = (
            await db.execute(
                select(IdempotencyRecord.status_code, IdempotencyRecord.response_json).where(
                    IdempotencyRecord.key == "k-selfheal"
                )
            )
        ).all()
    assert len(rows) == 1
    assert rows[0].status_code == 201
    assert rows[0].response_json == {"new": True}
