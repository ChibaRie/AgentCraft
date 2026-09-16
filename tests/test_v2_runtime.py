import pytest
from sqlalchemy import text

from backend.main import app

# FastAPI 依赖统一定义于 runtime.py，api/v2 仅 re-export
from backend.v2.runtime import V2Runtime, client_ip, get_v2_runtime
from tests.conftest import ADMIN_ROLE, APP_ROLE, PgDb


def make_v2_runtime(pg: PgDb) -> V2Runtime:
    """以 app/admin 角色 DSN 构建测试运行时（RLS 真实生效）。"""
    from backend.v2.db import build_engine, session_factory

    app_engine = build_engine(pg.role_url(*APP_ROLE))
    admin_engine = build_engine(pg.role_url(*ADMIN_ROLE))
    return V2Runtime(
        app_factory=session_factory(app_engine),
        admin_factory=session_factory(admin_engine),
        engines=(app_engine, admin_engine),
    )


@pytest.fixture
async def v2_runtime(pg):
    rt = make_v2_runtime(pg)
    app.dependency_overrides[get_v2_runtime] = lambda: rt
    yield rt
    app.dependency_overrides.pop(get_v2_runtime, None)
    rt.close()  # 同步 dispose 双引擎连接池（sync_engine.dispose）


async def test_owner_session_sets_guc_within_transaction(pg, v2_runtime):
    from backend.v2.runtime import owner_session

    uid = "11111111-1111-7111-8111-111111111111"
    # 种子必须走 superuser：0003 后 users 有 RLS，admin role 无 INSERT policy（42501）
    async with pg.engine.begin() as seed:
        await seed.execute(
            text(
                "INSERT INTO users (id, email, password_hash, role, status) "
                "VALUES (:i, 'e@x.test', 'h', 'user', 'active')"
            ),
            {"i": uid},
        )
    async with owner_session(v2_runtime, uid) as db:
        row = (await db.execute(text("SELECT email FROM users"))).scalar_one()
        assert row == "e@x.test"  # RLS 放行本行


def test_unconfigured_runtime_returns_503(client):
    # /api/health 为带 runtime 门的 V2 探针（Phase 8 T13 cutover：V1 无门探针
    # 随 V1 面删除，本探针自实现期 /api/v2/health 前缀切契约路径接位）
    resp = client.get("/api/health")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "SERVICE_UNAVAILABLE"


def test_client_ip_prefers_socket_peer_with_unknown_fallback():
    from starlette.requests import Request as StarletteRequest

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "query_string": b"",
    }
    assert client_ip(StarletteRequest({**scope, "client": ("203.0.113.7", 51000)})) == (
        "203.0.113.7"
    )
    # 直连 ASGI / 测试客户端可能缺 client（None）→ 兜底 "unknown"
    assert client_ip(StarletteRequest({**scope, "client": None})) == "unknown"
