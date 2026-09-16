"""/internal/provider-grant 端点测试（Phase 8 T5b，Sup §10.10 D16① 六钉）。

- 钉一：proxy 专用凭据（X-Proxy-Grant-Secret == env PROXY_GRANT_SECRET）；
  凭据缺失/不符与令牌失败统一 401 同形（不分层，internal 纪律）；X-Task-Token
  仅定位 round、非授权因子
- 钉二：响应 {model, provider:{api_key, base_target}} 仅从已验 claims 派生
  （model=任务快照 resolved model；Key 为控制面解密结果）
- 钉三：fence 链序复用 /internal/tools 断言（lease_epoch==当前值且轮 running）；
  settle 后同令牌再取 401（grant 随轮终局作废）
- 钉五：Key 解密经 owner 会话（owner 取自 fence 后 claims，RLS 圈定行）
- 钉六：审计 action=provider.grant.issue，detail 零 Key 材料
- 缓存钉：响应 Cache-Control: no-store
- env 钉：task 容器 env 清单不含 PROXY_GRANT_SECRET（executor 源面零出现）；
  compose 仅 provider-proxy 服务注入该键
"""

import base64
import json
from pathlib import Path

import pytest
from sqlalchemy import text

from backend.main import app
from backend.v2.provider_crypto import key_sealer
from backend.v2.runtime import get_v2_runtime
from backend.v2.task_token import create_v2_task_token
from tests.test_v2_runtime import make_v2_runtime
from tests.v2_provider_helpers import seed_provider, seed_task_for_provider
from tests.v2_task_helpers import seed_running_task, seed_task_user

_PROXY_SECRET = "proxy-grant-secret-t5b"
_KEK_MATERIAL = base64.urlsafe_b64encode(bytes(range(32, 64))).decode()
_REAL_KEY = "sk-v2-grant-real-0000"

# task_executor 容器 env 键清单（task_executor.py _build_container_spec 的零改动锚；
# executor 属 T5 领地——本文件以常量清单断言，不改 executor）
_TASK_CONTAINER_ENV_KEYS = frozenset(
    {
        "AGENTCRAFT_BACKEND_URL",
        "AGENTCRAFT_TASK_TOKEN",
        "AGENTCRAFT_PROVIDER",
        "AGENTCRAFT_PROVIDER_MODEL",
        "NODE_ENV",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
    }
)


class Env:
    """grant 面环境：pg-backed runtime + 真实密封 Key 的 provider 行。"""

    def __init__(self, rt, pg) -> None:
        self.rt = rt
        self.pg = pg

    async def seed_running(self, email: str) -> tuple[str, str, str, int, str]:
        """user + provider（真实密封 Key）+ running 任务 → (uid, tid, rid, epoch, token)。"""
        uid = str(await seed_task_user(self.pg, email))
        pid = await seed_provider(self.pg, uid)
        key_ciphertext, dek_wrapped = key_sealer().seal(_REAL_KEY, provider_id=str(pid))
        async with self.pg.engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE user_providers SET key_ciphertext = :c, dek_wrapped = :d WHERE id = :p"
                ),
                {"c": key_ciphertext, "d": dek_wrapped, "p": str(pid)},
            )
        tid = str(await seed_running_task(self.pg, uid, pid))
        async with self.pg.engine.begin() as conn:
            # 任务快照 resolved model 钉真实值（seed_task_for_provider 硬编码 'm'）
            await conn.execute(
                text("UPDATE tasks SET provider_model_id = 'gpt-4o-mini' WHERE id = :t"),
                {"t": tid},
            )
        rid, epoch = await _fetch_round(self.pg, tid)
        token = create_v2_task_token(
            task_id=tid, owner_id=uid, round_id=rid, lease_epoch=epoch, instance="grant-inst"
        )
        return uid, tid, rid, epoch, token


async def _fetch_round(pg, tid: str, state: str = "running") -> tuple[str, int]:
    async with pg.engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT id, lease_epoch FROM task_rounds WHERE task_id = :t AND state = :s"),
                {"t": tid, "s": state},
            )
        ).first()
    assert row is not None
    return str(row[0]), int(row[1])


@pytest.fixture()
async def grant_env(pg, monkeypatch):
    monkeypatch.setenv("PROVIDER_KEY_ENCRYPTION_KEY", _KEK_MATERIAL)
    monkeypatch.setenv("PROXY_GRANT_SECRET", _PROXY_SECRET)
    rt = make_v2_runtime(pg)
    app.dependency_overrides[get_v2_runtime] = lambda: rt
    yield Env(rt, pg)
    app.dependency_overrides.pop(get_v2_runtime, None)
    rt.close()


def _grant(client, token=None, *, secret=_PROXY_SECRET, send_secret=True):
    headers = {}
    if send_secret:
        headers["X-Proxy-Grant-Secret"] = secret
    if token is not None:
        headers["X-Task-Token"] = token
    return client.post("/internal/provider-grant", headers=headers)


# ---------------------------------------------------------------------------
# 钉一：proxy 专用凭据（统一 401 不分层）
# ---------------------------------------------------------------------------


async def test_missing_or_wrong_proxy_credential_401(client, grant_env):
    """无凭据/凭据不符/令牌无效三种失败层响应体完全一致（不区分校验层）。"""
    _uid, _tid, _rid, _epoch, token = await grant_env.seed_running("grant-cred@x.test")
    no_secret = _grant(client, token, send_secret=False)
    wrong_secret = _grant(client, token, secret="totally-wrong")
    bad_token = _grant(client, "garbage-token")
    assert no_secret.status_code == wrong_secret.status_code == bad_token.status_code == 401
    assert no_secret.json() == wrong_secret.json() == bad_token.json()
    assert no_secret.json()["error"]["code"] == "UNAUTHORIZED"


async def test_server_secret_unconfigured_401(client, grant_env, monkeypatch):
    """控制面未配置 PROXY_GRANT_SECRET → 一律 401（fail-closed，空串不比对）。"""
    _uid, _tid, _rid, _epoch, token = await grant_env.seed_running("grant-nosrv@x.test")
    monkeypatch.setenv("PROXY_GRANT_SECRET", "")
    resp = _grant(client, token)
    assert resp.status_code == 401


async def test_missing_task_token_with_valid_credential_401(client, grant_env):
    _uid, _tid, _rid, _epoch, _token = await grant_env.seed_running("grant-notok@x.test")
    assert _grant(client, None).status_code == 401


# ---------------------------------------------------------------------------
# 钉二/钉五 + 缓存钉：正例响应派生
# ---------------------------------------------------------------------------


async def test_grant_success_derived_from_claims(client, grant_env):
    """200 {model, provider:{api_key, base_target}}：model=任务快照 resolved model；
    Key=控制面解密结果；base_target=catalog allowed_host+path_prefix。"""
    _uid, _tid, _rid, _epoch, token = await grant_env.seed_running("grant-ok@x.test")
    resp = _grant(client, token)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert set(data) == {"model", "provider"}
    assert data["model"] == "gpt-4o-mini"
    assert set(data["provider"]) == {"api_key", "base_target"}
    assert data["provider"]["api_key"] == _REAL_KEY
    assert data["provider"]["base_target"] == "https://api.openai.com/v1"


async def test_response_no_store(client, grant_env):
    _uid, _tid, _rid, _epoch, token = await grant_env.seed_running("grant-nostore@x.test")
    resp = _grant(client, token)
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"


# ---------------------------------------------------------------------------
# 钉三：fence 链序（非 running / 旧 epoch / settle 后同令牌）
# ---------------------------------------------------------------------------


async def test_non_running_round_401(client, grant_env, pg):
    """pending 轮（queued 任务）未达 running → 401。"""
    uid = str(await seed_task_user(pg, "grant-pending@x.test"))
    pid = await seed_provider(pg, uid)
    tid = str(await seed_task_for_provider(pg, uid, pid, status="queued"))
    rid, epoch = await _fetch_round(pg, tid, state="pending")
    token = create_v2_task_token(
        task_id=tid, owner_id=uid, round_id=rid, lease_epoch=epoch, instance="grant-inst"
    )
    assert _grant(client, token).status_code == 401


async def test_fenced_old_epoch_token_401(client, grant_env, pg):
    """fence 后旧 epoch 令牌 → 401（复用 /internal/tools 同构断言）。"""
    _uid, _tid, rid, _epoch, token = await grant_env.seed_running("grant-fence@x.test")
    async with pg.engine.begin() as conn:
        await conn.execute(
            text("UPDATE task_rounds SET lease_epoch = lease_epoch + 1 WHERE id = :r"),
            {"r": rid},
        )
    assert _grant(client, token).status_code == 401


async def test_settled_round_same_token_401(client, grant_env, pg):
    """钉三终局语义：settle 后同令牌再取 → 401（grant 随轮终局作废）。"""
    _uid, _tid, rid, _epoch, token = await grant_env.seed_running("grant-settle@x.test")
    assert _grant(client, token).status_code == 200
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE task_rounds SET state = 'settled', lease_owner = NULL, "
                "lease_expires_at = NULL WHERE id = :r"
            ),
            {"r": rid},
        )
    assert _grant(client, token).status_code == 401


# ---------------------------------------------------------------------------
# 钉六：审计（零 Key 材料）
# ---------------------------------------------------------------------------


async def test_audit_row_without_key_material(client, grant_env, pg):
    """发 Key 落审计 provider.grant.issue；detail={task_id, round_id, provider_id}
    且审计行不含 Key 明文（响应体本身按钉二携带 api_key——审计零 Key 材料仅指
    audit_logs 行）。"""
    _uid, tid, rid, _epoch, token = await grant_env.seed_running("grant-audit@x.test")
    resp = _grant(client, token)
    assert resp.status_code == 200
    async with pg.engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT action, target_type, target_id, detail, reason FROM audit_logs "
                    "WHERE action = 'provider.grant.issue'"
                )
            )
        ).all()
    assert len(rows) == 1
    row = rows[0]
    assert row[1] == "task" and str(row[2]) == tid
    detail = row[3]
    assert detail == {"task_id": tid, "round_id": rid, "provider_id": detail["provider_id"]}
    assert _REAL_KEY not in json.dumps(detail)
    # Key 明文也不落审计行的 reason / target 等其余字段（全行 JSON 序列化复验）
    assert _REAL_KEY not in json.dumps(
        {"action": row[0], "target_type": row[1], "reason": row[4], "detail": detail}
    )


# ---------------------------------------------------------------------------
# env 钉：task 容器 env / compose 注入面
# ---------------------------------------------------------------------------


def test_task_container_env_excludes_grant_secret():
    """钉一容器面：task 容器 env 清单不含 PROXY_GRANT_SECRET（常量清单 +
    executor 源面双断言——env 注入面测试归 test_v2_task_executor.py，此处以
    自持常量钉）。"""
    assert "PROXY_GRANT_SECRET" not in _TASK_CONTAINER_ENV_KEYS
    source = (
        Path(__file__).resolve().parents[1] / "backend" / "v2" / "task_executor.py"
    ).read_text(encoding="utf-8")
    assert "PROXY_GRANT_SECRET" not in source


def test_compose_grant_secret_only_on_proxy_service():
    """compose 注入面：PROXY_GRANT_SECRET 仅出现在 provider-proxy 服务块
    （control 经 env_file ../.env 读取，不显式声明；pi-worker 面禁出现）。"""
    compose = (Path(__file__).resolve().parents[1] / "docker" / "docker-compose.yml").read_text(
        encoding="utf-8"
    )
    head, _sep, rest = compose.partition("\n  provider-proxy:")
    proxy_block, _sep2, tail = rest.partition("\n  docker-socket-proxy:")
    assert "PROXY_GRANT_SECRET" in proxy_block
    assert "PROXY_GRANT_SECRET" not in head
    assert "PROXY_GRANT_SECRET" not in tail


# ---------------------------------------------------------------------------
# 顶一拓扑面（Phase 9 T1：D16② 接线）
# ---------------------------------------------------------------------------


def test_default_grant_url_targets_internal_alias():
    """默认 grant URL 必须走 internal 网络内控制面别名 agentcraft-control：
    dev 形态由后端转发容器提供该别名，compose 形态由 control 服务网络别名
    提供——两者同名才能共用一份默认配置。旧默认 http://control:8000 在 dev
    形态下 DNS 不可达（D16② 原缺口）。"""
    from backend.config import Settings

    settings = Settings(_env_file=None)
    assert settings.PROVIDER_PROXY_GRANT_URL == (
        "http://agentcraft-control:8000/internal/provider-grant"
    )


def test_compose_wires_grant_topology():
    """compose 拓扑面：control 加入 internal 网络并携带 agentcraft-control
    别名（proxy 侧 grant 兑换可达），且 proxy 服务显式声明
    PROVIDER_PROXY_GRANT_URL；pi-worker 面不得出现 grant 相关配置。"""
    compose = (Path(__file__).resolve().parents[1] / "docker" / "docker-compose.yml").read_text(
        encoding="utf-8"
    )
    control_block = compose.partition("\n  control:")[2].partition("\n  provider-proxy:")[0]
    assert "agentcraft-control" in control_block
    assert "internal:" in control_block

    proxy_block = compose.partition("\n  provider-proxy:")[2].partition("\n  docker-socket-proxy:")[
        0
    ]
    assert "PROVIDER_PROXY_GRANT_URL" in proxy_block

    worker_block = compose.partition("\n  pi-worker:")[2].partition("\nnetworks:")[0]
    assert "PROVIDER_PROXY_GRANT_URL" not in worker_block
    assert "PROXY_GRANT_SECRET" not in worker_block
