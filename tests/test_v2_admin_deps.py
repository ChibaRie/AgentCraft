"""admin 门依赖与壳原语测试（Phase 7 T1）。

覆盖（brief ≥14 例清单）：D2 三重门矩阵（非 admin FORBIDDEN / TOTP 未配置 /
12h 过期回拨 13h / 1h 内放行 / NULL）、require_admin_reason 边界、admin 幂等
三段壳（store→重放 / 异 key miss / subject 隔离 / 异 request_hash 409）、
admin_kill_switch scope 登记钉、check_code_style permissions 对齐 0002 种子、
seed_admin_user / admin_client / seed_v2_admin 冒烟、main.py 挂载结构钉。
0009 迁移真实生效断言在 tests/test_v2_rls.py（RLS 行为面），head 钉在
tests/test_v2_migrations.py。

测试方式裁决（沿 test_v2_session_service 先例）：直调依赖——手工构造 scope 内联
method/Cookie/X-CSRF-Token 的 Starlette Request，显式传 runtime 与 admin_db。
不走临时 APIRouter + TestClient（pytest-asyncio 每用例独立事件循环，TestClient
portal 与 asyncpg 连接池跨循环冲突），且三重门逐门精确断言。密钥材料经
admin_env（tests/v2_admin_helpers 三件套）注入。
"""

import base64
import json
import re
import uuid as _uuid
from pathlib import Path

import pyotp
import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.requests import Request as StarletteRequest

from backend.api.v2.admin._deps import (
    admin_idem_begin,
    admin_idem_store,
    get_v2_admin_auth,
    require_admin_reason,
)
from backend.engine.platform_tools import PLATFORM_TOOLS
from backend.errors import AgentCraftError, ErrorCode
from backend.utils.crypto import decrypt_text, make_keyring
from backend.v2 import idempotency, login_service, session_service
from backend.v2.rate_limit import LIMITS, hmac_subject
from backend.v2.runtime import owner_session
from backend.v2.security import hash_token
from backend.v2.session_service import create_session
from tests import v2_admin_helpers as _vah
from tests.v2_admin_helpers import _KEY_MATERIAL, admin_client, seed_admin_user

# admin_env 夹具发现形态：pytest 经模块命名空间发现夹具，但「import + 参数同名」
# 会触发 ruff F811（参数遮蔽未使用的 import）——以赋值别名引入（T2+ 同型照抄）。
admin_env = _vah.admin_env

_ROOT = Path(__file__).resolve().parents[1]
_A = "11111111-1111-7111-8111-111111111111"
_B = "22222222-2222-7222-8222-222222222222"


# ---------- 直调依赖助手（test_v2_session_service 同款）----------


def _request(method: str = "GET", token: str | None = None, csrf: str | None = None):
    headers = []
    if token is not None:
        headers.append((b"cookie", f"{session_service.COOKIE_NAME}={token}".encode()))
    if csrf is not None:
        headers.append((b"x-csrf-token", csrf.encode()))
    scope = {
        "type": "http",
        "method": method,
        "path": "/api/admin/_probe",
        "headers": headers,
        "query_string": b"",
    }
    return StarletteRequest(scope)


async def _resolve_ctx(rt, token: str, *, method: str = "GET", csrf: str | None = None):
    """完整认证链（get_v2_auth：cookie→CSRF→状态门→滑动刷新）→ V2AuthContext。"""
    async with rt.admin_factory() as admin_db:
        return await session_service.get_v2_auth(
            _request(method, token=token, csrf=csrf), runtime=rt, admin_db=admin_db
        )


async def _new_mfa_session(rt, user_id: str) -> tuple[str, str]:
    """owner 事务内建 mfa_verified 会话（命中 sessions_app_insert WITH CHECK）。"""
    async with owner_session(rt, user_id) as db:
        return await create_session(
            db,
            user_id=_uuid.UUID(user_id),
            device_label="AgentCraft-AdminDepTest/1.0",
            mfa_verified=True,
        )


async def _seed_admin_with_mfa_session(pg, rt, email: str, *, configure_mfa: bool = True):
    admin_id = await seed_admin_user(pg, email, configure_mfa=configure_mfa)
    token, csrf = await _new_mfa_session(rt, admin_id)
    return admin_id, token, csrf


async def _rewind_mfa_verified(pg, user_id: _uuid.UUID, *, hours: int) -> None:
    """superuser 回拨会话 mfa_verified_at（sessions owner-RLS，superuser 绕过）。"""
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE sessions SET mfa_verified_at = now() - make_interval(secs => :s) "
                "WHERE user_id = :u"
            ),
            {"s": hours * 3600, "u": user_id},
        )


# ---------- D2 三重门矩阵 ----------


async def test_non_admin_403_forbidden_role_gate_first(pg, admin_env):
    """非 admin（未配 TOTP + MFA 会话齐全——若 MFA 门先行会误报 ADMIN_MFA_REQUIRED，
    本形态即门序① role 门先于②的证据）→ 403 FORBIDDEN；code 字符串与 A9 字面
    （login_service._ADMIN_DISABLE_FORBIDDEN_DETAIL）逐字节一致；FORBIDDEN 已登记
    ErrorCode 注册表。"""
    rt = admin_env
    uid = str(_uuid.uuid4())
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash, role, status, mfa_secret_enc) "
                "VALUES (:i, 'nonadmin@x.test', 'h', 'user', 'active', NULL)"
            ),
            {"i": uid},
        )
    token, _csrf = await _new_mfa_session(rt, uid)
    ctx = await _resolve_ctx(rt, token)
    with pytest.raises(HTTPException) as excinfo:
        await get_v2_admin_auth(ctx)
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail["code"] == "FORBIDDEN"
    # A9 字面逐字节一致（V1 约定形状；注册表字面同一来源）
    assert excinfo.value.detail["code"] == login_service._ADMIN_DISABLE_FORBIDDEN_DETAIL["code"]
    assert ErrorCode.FORBIDDEN.value == "FORBIDDEN"


async def test_admin_with_fresh_mfa_passes_gate(pg, admin_env):
    """正方向：admin + TOTP 已配置 + mfa_verified 新鲜会话 → 门原样透传 ctx
    （零拷贝零副作用——后续服务直接消费）。"""
    admin_id, token, _ = await _seed_admin_with_mfa_session(pg, admin_env, "gate-ok@x.test")
    ctx = await _resolve_ctx(admin_env, token)
    passed = await get_v2_admin_auth(ctx)
    assert passed is ctx
    assert str(passed.user.id) == admin_id


async def test_admin_without_totp_403_admin_mfa_required(pg, admin_env):
    """D2②：TOTP 未配置（mfa_secret_enc NULL，configure_mfa=False 播种形态）→
    403 ADMIN_MFA_REQUIRED——未配置者永不过门（即使会话带 mfa_verified 印）。"""
    _admin_id, token, _ = await _seed_admin_with_mfa_session(
        pg, admin_env, "no-totp@x.test", configure_mfa=False
    )
    ctx = await _resolve_ctx(admin_env, token)
    with pytest.raises(HTTPException) as excinfo:
        await get_v2_admin_auth(ctx)
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail["code"] == "ADMIN_MFA_REQUIRED"


async def test_admin_mfa_13h_old_403(pg, admin_env):
    """D2③：mfa_verified_at 回拨 13h（> 12h 窗口）→ 403 ADMIN_MFA_REQUIRED。
    回拨后重新解析 ctx——门读的是刷新前快照，须含回拨后的值。"""
    admin_id, token, _ = await _seed_admin_with_mfa_session(pg, admin_env, "gate-13h@x.test")
    await _rewind_mfa_verified(pg, _uuid.UUID(admin_id), hours=13)
    ctx = await _resolve_ctx(admin_env, token)
    with pytest.raises(HTTPException) as excinfo:
        await get_v2_admin_auth(ctx)
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail["code"] == "ADMIN_MFA_REQUIRED"


async def test_admin_mfa_within_1h_passes(pg, admin_env):
    """D2③ 边界内侧：mfa_verified_at 回拨 1h（< 12h 窗口）→ 放行。"""
    admin_id, token, _ = await _seed_admin_with_mfa_session(pg, admin_env, "gate-1h@x.test")
    await _rewind_mfa_verified(pg, _uuid.UUID(admin_id), hours=1)
    ctx = await _resolve_ctx(admin_env, token)
    assert await get_v2_admin_auth(ctx) is ctx
    assert str(ctx.user.id) == admin_id


async def test_admin_mfa_verified_null_403(pg, admin_env):
    """D2③：mfa_verified_at NULL（常规登录会话——admin 面不接受未过 MFA 的持有期）
    → 403 ADMIN_MFA_REQUIRED。"""
    admin_id, token, _ = await _seed_admin_with_mfa_session(pg, admin_env, "gate-null@x.test")
    async with pg.engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET mfa_verified_at = NULL WHERE user_id = :u"),
            {"u": _uuid.UUID(admin_id)},
        )
    ctx = await _resolve_ctx(admin_env, token)
    with pytest.raises(HTTPException) as excinfo:
        await get_v2_admin_auth(ctx)
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail["code"] == "ADMIN_MFA_REQUIRED"


# ---------- require_admin_reason（D11：壳层独立实现）----------


def test_require_admin_reason_missing_or_blank_400():
    """None/空串/纯空白 → 400 ADMIN_REASON_REQUIRED（AgentCraftError 载体）。"""
    for bad in (None, "", "   ", "\t\n"):
        with pytest.raises(AgentCraftError) as excinfo:
            require_admin_reason(bad)
        assert excinfo.value.code is ErrorCode.ADMIN_REASON_REQUIRED
        assert excinfo.value.http_status == 400


def test_require_admin_reason_boundary_2000():
    """≤2000 放行且原样返回（归一化不在此做——reason 原样参与 request_hash）；
    2001 → 400 VALIDATION_ERROR（HTTPException 载体，未登记字面同 V1 约定形状）。"""
    ok = "r" * 2000
    assert require_admin_reason(ok) == ok
    with pytest.raises(HTTPException) as excinfo:
        require_admin_reason("r" * 2001)
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail["code"] == "VALIDATION_ERROR"


# ---------- admin 幂等三段壳（T8a reports/tasks 壳同构）----------


async def test_admin_idem_shell_store_replay_miss_and_conflict(pg, admin_env):
    """段一 begin（app 裸会话）/段二 store（调用方 admin 事务，不 commit 随其收口）
    同构钉：命中原样重放（status_code+payload）；异 key 未命中 None；异 admin_id
    主体隔离（subject=subject_user(admin_id)）；同 key 异 request_hash（reason
    参与哈希）→ 409 IDEMPOTENCY_CONFLICT。"""
    rt = admin_env
    request = _request("POST")
    req_hash = idempotency.request_hash({"reason": "处置理由"})
    async with rt.admin_factory() as db:  # 段二：admin 事务内 store
        async with db.begin():
            await admin_idem_store(
                db,
                request=request,
                admin_id=_A,
                key="k-1",
                req_hash=req_hash,
                status_code=201,
                payload={"data": {"ok": True}},
            )
    replay = await admin_idem_begin(  # 段一：命中原样重放
        rt, request=request, admin_id=_A, key="k-1", req_hash=req_hash
    )
    assert isinstance(replay, JSONResponse)
    assert replay.status_code == 201
    assert json.loads(replay.body) == {"data": {"ok": True}}

    assert (  # 异 key：未命中
        await admin_idem_begin(rt, request=request, admin_id=_A, key="k-2", req_hash=req_hash)
        is None
    )
    assert (  # 异 admin_id：subject 隔离，互不可见
        await admin_idem_begin(rt, request=request, admin_id=_B, key="k-1", req_hash=req_hash)
        is None
    )
    with pytest.raises(AgentCraftError) as excinfo:  # 同 key 异 request_hash → 409
        await admin_idem_begin(
            rt,
            request=request,
            admin_id=_A,
            key="k-1",
            req_hash=idempotency.request_hash({"reason": "别的理由"}),
        )
    assert excinfo.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert excinfo.value.http_status == 409


# ---------- 登记钉（scope / permissions / 挂载）----------


def test_admin_kill_switch_scope_registered(monkeypatch):
    """scope 登记钉（同 D12 形态）：admin_kill_switch = 10 次/h/用户，"user" 主体
    类别合法（hmac_subject 64 hex；密钥材料用例内注入——neutralize_v2_env 钉空）。
    路由挂接归 T6——本钉只看 LIMITS 登记表（未登记 scope 的 enforce 拒绝服务而非
    静默放行）。"""
    monkeypatch.setenv("RATE_LIMIT_HMAC_KEY", base64.urlsafe_b64encode(bytes(range(32))).decode())
    assert LIMITS["admin_kill_switch"] == (10, 3600)
    assert len(hmac_subject("user", "victim-user-id")) == 64


def test_check_code_style_permissions_match_0002_seed():
    """platform_tools 描述符 permissions 与 0002:51 种子 JSONB 一致（T1 随车项 D11；
    internal.py 当前不消费本字段——纯元数据钉，零行为变化）。种子原文从迁移源码
    解析（text() 冒号转义还原），两侧任一漂移即红。"""
    seed_sql = (
        _ROOT / "backend" / "alembic_v2" / "versions" / "0002_seed_catalogs_and_slots.py"
    ).read_text(encoding="utf-8")
    match = re.search(r"'check_code_style', '1',\s*'(.*?)'::jsonb", seed_sql, re.S)
    assert match, "0002 check_code_style 种子行结构变化，需同步更新本钉"
    assert PLATFORM_TOOLS[("check_code_style", "1")].permissions == json.loads(
        match.group(1).replace(r"\:", ":")
    )


def test_admin_router_mounted_in_main():
    """挂载结构钉：main.py 以 prefix="/api/admin" 挂聚合 router。T1 为空骨架
    （无路由面，404 兜底）——首个 admin 路由落地（T2）时补行为面钉。"""
    source = (_ROOT / "backend" / "main.py").read_text(encoding="utf-8")
    assert 'app.include_router(admin_api_router, prefix="/api/admin")' in source


# ---------- 测试助手 / bootstrap 冒烟 ----------


async def test_seed_admin_user_smoke(pg, admin_env):
    """seed_admin_user：admin/active 行 + 信封可解回 TOTP secret + email 小写归一；
    configure_mfa=False → mfa_secret_enc NULL（D2② 负例形态）。"""
    admin_id = await seed_admin_user(pg, "Smoke@Example.com")
    async with pg.engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT email, role, status, mfa_secret_enc FROM users "
                        "WHERE id = CAST(:i AS uuid)"
                    ),
                    {"i": admin_id},
                )
            )
            .mappings()
            .one()
        )
    assert row["email"] == "smoke@example.com"  # 小写归一（login 按小写查）
    assert row["role"] == "admin"
    assert row["status"] == "active"
    _, keyring = make_keyring(f"primary:{_KEY_MATERIAL}")
    secret = decrypt_text(
        json.loads(row["mfa_secret_enc"]),
        aad=login_service.mfa_secret_aad(admin_id),
        keyring=keyring,
    )
    assert len(secret) >= 16
    assert pyotp.TOTP(secret).now().isdigit()  # 信封解出的 secret 可产码

    bare_id = await seed_admin_user(pg, "bare-admin@x.test", configure_mfa=False)
    async with pg.engine.connect() as conn:
        bare = (
            (
                await conn.execute(
                    text("SELECT role, mfa_secret_enc FROM users WHERE id = CAST(:i AS uuid)"),
                    {"i": bare_id},
                )
            )
            .mappings()
            .one()
        )
    assert bare["role"] == "admin"
    assert bare["mfa_secret_enc"] is None


async def test_admin_client_smoke(pg, admin_env):
    """admin_client：MFA 验证会话（mfa_verified_at 非空，门③前置）落库、cookie
    jar 持 ac_session、X-CSRF-Token 预置；cookie 会话可过完整认证链 + admin 门。"""
    rt = admin_env
    client, csrf, admin_id = await admin_client(pg, rt, email="client-smoke@x.test")
    try:
        token = client.cookies.get(session_service.COOKIE_NAME)
        assert token and csrf
        assert client.headers["X-CSRF-Token"] == csrf
        async with pg.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT token_hash, mfa_verified_at FROM sessions WHERE user_id = :u"),
                        {"u": _uuid.UUID(admin_id)},
                    )
                )
                .mappings()
                .one()
            )
        assert row["mfa_verified_at"] is not None
        assert row["token_hash"] == hash_token(token)  # 库存哈希 ↔ jar 明文
        ctx = await _resolve_ctx(rt, token, method="POST", csrf=csrf)  # 认证链正方向
        assert str(ctx.user.id) == admin_id
        assert await get_v2_admin_auth(ctx) is ctx  # 三重门放行
    finally:
        await client.aclose()


async def test_seed_v2_admin_script_smoke(pg):
    """seed_v2_admin 冒烟（D9）：seed_admin 落 role='admin'/active/mfa_secret_enc
    NULL 行（email 小写归一）；重复执行幂等跳过（exists），不改动既有行。"""
    from tools.seed_v2_admin import seed_admin

    dsn = pg.url()
    assert await seed_admin(dsn, "Bootstrap-Admin@Example.com", "pw-123456") == "seeded"
    async with pg.engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT role, status, mfa_secret_enc FROM users "
                        "WHERE email = 'bootstrap-admin@example.com'"
                    )
                )
            )
            .mappings()
            .one()
        )
    assert row["role"] == "admin"
    assert row["status"] == "active"
    assert row["mfa_secret_enc"] is None
    # 幂等：存在即跳过
    assert await seed_admin(dsn, "bootstrap-admin@example.com", "pw-999999") == "exists"
    async with pg.engine.connect() as conn:
        password_hash = (
            await conn.execute(
                text("SELECT password_hash FROM users WHERE email = 'bootstrap-admin@example.com'")
            )
        ).scalar_one()
    assert password_hash  # 既有行未被第二次调用改动（哈希仍在，未抛错即未重写）


def test_seed_v2_admin_argparse_and_dsn_resolution(monkeypatch):
    """argparse 简洁形态 + DSN 解析：缺省读 V2_DATABASE_URL、--dsn 覆盖、双缺失
    SystemExit(2) fail fast。"""
    from tools.seed_v2_admin import _parse_args, _resolve_dsn

    args = _parse_args(["ops@example.com", "--password", "pw-123456"])
    assert args.email == "ops@example.com"
    assert args.password == "pw-123456"
    assert args.dsn is None
    monkeypatch.setenv("V2_DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/db")
    assert _resolve_dsn(args) == "postgresql+asyncpg://u:p@h:5432/db"
    overridden = _parse_args(
        ["ops@example.com", "--password", "pw", "--dsn", "postgresql+asyncpg://s:s@h/db"]
    )
    assert _resolve_dsn(overridden) == "postgresql+asyncpg://s:s@h/db"
    monkeypatch.delenv("V2_DATABASE_URL", raising=False)
    with pytest.raises(SystemExit):
        _resolve_dsn(args)  # 双缺失 → fail fast
    with pytest.raises(SystemExit):
        _parse_args([])  # 缺位置参数
