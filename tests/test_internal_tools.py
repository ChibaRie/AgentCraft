"""/internal/tools 四回调端点测试（Phase 6 T7，D2 全回调 / D16 / D17）。

真令牌链：create_v2_task_token 签发 + executor.tokens 真登记（RoundExecutor 真
件，tmp 扩展根）+ task_rounds lease 三元组（seed_running_task）。校验链全序
（D17）：decode → claims.task_id=请求 task_id → round_id/instance=登记表 →
claims.lease_epoch==task_rounds 当前值（owner 会话内 fence）→ assert_tool_enabled
（403）→ permissions 服务端强制（400）。测试形态对齐 test_internal_mcp.py 的 V1
回调面（client + 依赖 override 注入 pg-backed runtime）。
"""

import base64

import pytest
from sqlalchemy import text

from backend.main import app
from backend.v2.runtime import get_v2_runtime
from backend.v2.task_executor import RoundExecutor
from backend.v2.task_storage import TaskStorage
from backend.v2.task_streams import TaskStreamRegistry
from backend.v2.task_token import create_v2_task_token
from tests.conftest import APP_ROLE, make_role_engine
from tests.test_v2_runtime import make_v2_runtime
from tests.v2_provider_helpers import seed_provider, seed_task_for_provider
from tests.v2_task_helpers import (
    owner_tx,
    seed_published_revision,
    seed_running_task,
    seed_task_user,
)


class Env:
    """回调面环境：pg-backed runtime + 真件 executor（tokens 登记表）。"""

    def __init__(self, rt) -> None:
        self.rt = rt
        self.executor = RoundExecutor(rt, streams=TaskStreamRegistry(), instance_id="itest")
        rt.executor = self.executor

    def register(
        self, uid: str, tid: str, round_id: str, epoch: int, *, instance: str = "itest-inst"
    ) -> str:
        token = create_v2_task_token(
            task_id=tid,
            owner_id=uid,
            round_id=round_id,
            lease_epoch=epoch,
            instance=instance,
        )
        self.executor.tokens[round_id] = token
        return token


@pytest.fixture()
async def tools_env(test_db, tmp_path, pg):
    rt = make_v2_runtime(pg)
    rt.storage = TaskStorage(tmp_path)  # 扩展根/存储根钉 tmp（构造 executor 前）
    env = Env(rt)
    app.dependency_overrides[get_v2_runtime] = lambda: rt
    yield env
    app.dependency_overrides.pop(get_v2_runtime, None)
    rt.executor = None
    rt.close()


def _post(client, path: str, tid: str, token: str | None, **extra):
    body = {"task_id": tid, **extra}
    return client.post(
        f"/internal/tools/{path}",
        json=body,
        headers={"X-Task-Token": token} if token is not None else None,
    )


async def _fetch_running_round(pg, tid) -> tuple[str, int]:
    async with pg.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT id, lease_epoch FROM task_rounds "
                    "WHERE task_id = :t AND state = 'running'"
                ),
                {"t": tid},
            )
        ).first()
    assert row is not None
    return str(row[0]), int(row[1])


async def _seed_running_minimal(pg, email: str) -> tuple[str, str, str, int]:
    """user + provider + running 任务全套持有物（无输入文件）。"""
    uid = str(await seed_task_user(pg, email))
    pid = await seed_provider(pg, uid)
    tid = await seed_running_task(pg, uid, pid)
    round_id, epoch = await _fetch_running_round(pg, tid)
    return uid, str(tid), round_id, epoch


async def _seed_running_with_inputs(pg, storage, email: str, uploads) -> tuple[str, str, str, int]:
    """真实任务链：create_task → upload_files（真盘）→ commit_input → 人工领取
    （running 轮 lease 三元组 + running reservation + 槽位，seed_running_task 同型
    SQL 应用到既有任务）。返回 (uid, tid, round_id, epoch)。"""
    uid = str(await seed_task_user(pg, email))
    pid = await seed_provider(pg, uid)
    rid = await seed_published_revision(pg, uid)
    async with pg.engine.connect() as conn:
        cid = str(
            (
                await conn.execute(
                    text("SELECT catalog_id FROM user_providers WHERE id = :p"), {"p": str(pid)}
                )
            ).scalar_one()
        )
    snapshot = {
        "provider_id": str(pid),
        "provider_catalog_id": cid,
        "provider_model_id": "gpt-4o-mini",
        "provider_key_version": 1,
    }
    from backend.v2.task_file_service import upload_files
    from backend.v2.task_service import commit_input, create_task

    engine = make_role_engine(pg, APP_ROLE)
    try:
        async with owner_tx(engine, uid) as db:
            out = await create_task(
                db,
                owner_id=uid,
                expert_revision_id=rid,
                provider_id=pid,
                initial_message="t7 回调链",
                provider_snapshot=snapshot,
            )
        tid = out["task"]["id"]
        async with owner_tx(engine, uid) as db:
            await upload_files(db, storage, owner_id=uid, task_id=tid, uploads=uploads)
        async with owner_tx(engine, uid) as db:
            await commit_input(
                db,
                owner_id=uid,
                task_id=tid,
                manifest=[{"file_name": name} for name, _c in uploads],
            )
    finally:
        await engine.dispose()
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE task_rounds SET state = 'running', lease_owner = 'exec-test', "
                "lease_epoch = 1, lease_expires_at = now() + interval '90 seconds', "
                "attempt = 1 WHERE task_id = :t"
            ),
            {"t": tid},
        )
        await conn.execute(text("UPDATE tasks SET status = 'running' WHERE id = :t"), {"t": tid})
        await conn.execute(
            text(
                "INSERT INTO task_reservations (id, task_id, user_id, kind, bytes, state) "
                "VALUES (gen_random_uuid(), :t, :u, 'running', 0, 'held')"
            ),
            {"t": tid, "u": uid},
        )
        await conn.execute(
            text(
                "UPDATE user_quota_usage SET running_tasks = running_tasks + 1 WHERE user_id = :u"
            ),
            {"u": uid},
        )
        await conn.execute(
            text(
                "UPDATE platform_slots SET state = 'leased', task_id = :t, "
                "leased_until = now() + interval '90 seconds' "
                "WHERE slot_no = 1 AND state = 'free'"
            ),
            {"t": tid},
        )
    round_id, epoch = await _fetch_running_round(pg, tid)
    return uid, str(tid), round_id, epoch


# ---------------------------------------------------------------------------
# 四端点各 1 正例（真令牌链）
# ---------------------------------------------------------------------------


async def test_read_task_file_roundtrip(client, tools_env, pg):
    """read-task-file 正例：真令牌链 → 输入文件字节 base64 回读一致。"""
    uid, tid, rid, epoch = await _seed_running_with_inputs(
        pg, tools_env.rt.storage, "t7-read@x.test", [("hello.txt", b"HELLO BYTES")]
    )
    token = tools_env.register(uid, tid, rid, epoch)
    resp = _post(client, "read-task-file", tid, token, file_name="hello.txt")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert base64.b64decode(data["content_base64"]) == b"HELLO BYTES"
    assert data["size_bytes"] == 11 and data["file_name"] == "hello.txt"


async def test_write_output_file_registers(client, tools_env, pg):
    """write-output-file 正例：register_output 全链（行 registered + 物理双写 +
    账）——round_id/lease_epoch 取自 claims。"""
    uid, tid, rid, epoch = await _seed_running_minimal(pg, "t7-write@x.test")
    token = tools_env.register(uid, tid, rid, epoch)
    payload = base64.b64encode(b"OUT").decode("ascii")
    resp = _post(
        client, "write-output-file", tid, token, file_name="out.txt", content_base64=payload
    )
    assert resp.status_code == 200, resp.text
    entry = resp.json()["data"]["file"]
    assert entry["state"] == "registered" and entry["size_bytes"] == 3
    assert entry["produced_in_round_id"] == rid
    async with pg.engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT direction, state, produced_in_round_id FROM task_files WHERE id = :f"),
                {"f": entry["id"]},
            )
        ).first()
    assert row is not None
    assert row[0] == "output" and row[1] == "registered" and str(row[2]) == rid
    assert tools_env.rt.storage.output_path(tid, entry["id"]).read_bytes() == b"OUT"
    assert tools_env.rt.storage.artifact_path(tid, entry["id"]).read_bytes() == b"OUT"


async def test_list_task_files_success(client, tools_env, pg):
    """list-task-files 正例：存活输入元数据（墓碑/产物面不可见——T4 面口径）。"""
    from tests.v2_task_helpers import seed_input_file

    uid, tid, rid, epoch = await _seed_running_with_inputs(
        pg,
        tools_env.rt.storage,
        "t7-list@x.test",
        [("m1.txt", b"M1"), ("m2.txt", b"M22")],
    )
    # 墓碑输入与产物面行均不可见（list_input_meta 存活 + direction 过滤）
    await seed_input_file(pg, uid, tid, size_bytes=9, state="deleted", file_name="gone.txt")
    await seed_input_file(
        pg, uid, tid, size_bytes=5, direction="output", state="registered", file_name="o.bin"
    )
    token = tools_env.register(uid, tid, rid, epoch)
    resp = _post(client, "list-task-files", tid, token)
    assert resp.status_code == 200, resp.text
    files = resp.json()["data"]["files"]
    assert [f["file_name"] for f in files] == ["m1.txt", "m2.txt"]
    assert set(files[0]) == {"id", "file_name", "sha256", "size_bytes"}
    assert files[1]["size_bytes"] == 3


async def test_query_task_state_success_and_key_set(client, tools_env, pg):
    """query-task-state 正例：D14 视图子集 + 响应键集断言——任意层级不含
    lease_* 字段（服务端构造器剥除式，非 400 拒绝式）；round 摘要仅
    id/state/attempt。"""
    uid, tid, rid, epoch = await _seed_running_minimal(pg, "t7-query@x.test")
    token = tools_env.register(uid, tid, rid, epoch)
    resp = _post(client, "query-task-state", tid, token)
    assert resp.status_code == 200, resp.text
    view = resp.json()["data"]["task"]
    assert view["status"] == "running"
    assert view["active_round"]["id"] == rid
    assert set(view["active_round"]) == {"id", "state", "attempt"}
    assert view["counts"] == {"inputs": 0, "outputs": 0}

    def _scan(node):
        if isinstance(node, dict):
            assert not any(str(k).startswith("lease_") for k in node), node.keys()
            for v in node.values():
                _scan(v)
        elif isinstance(node, list):
            for v in node:
                _scan(v)

    _scan(resp.json())


# ---------------------------------------------------------------------------
# 校验链负例：permissions / fence / 登记表 / kill switch
# ---------------------------------------------------------------------------


async def test_permissions_traversal_rejected_400(client, tools_env, pg):
    """permissions 路径形态强制：穿越形 file_name → 400 TOOL_CALL_REJECTED
    （read 与 write 双面）。"""
    uid, tid, rid, epoch = await _seed_running_minimal(pg, "t7-trav@x.test")
    token = tools_env.register(uid, tid, rid, epoch)
    resp = _post(client, "read-task-file", tid, token, file_name="../x")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "TOOL_CALL_REJECTED"
    resp = _post(
        client,
        "write-output-file",
        tid,
        token,
        file_name="a/b",
        content_base64=base64.b64encode(b"x").decode("ascii"),
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "TOOL_CALL_REJECTED"


async def test_fenced_old_epoch_token_rejected(client, tools_env, pg):
    """fence 后旧 epoch 令牌回调拒绝（契约 C4）：登记表仍持旧令牌，但
    task_rounds.lease_epoch 已前移 → 401。"""
    uid, tid, rid, epoch = await _seed_running_minimal(pg, "t7-fence@x.test")
    token = tools_env.register(uid, tid, rid, epoch)
    async with pg.engine.begin() as conn:
        await conn.execute(
            text("UPDATE task_rounds SET lease_epoch = lease_epoch + 1 WHERE id = :r"),
            {"r": rid},
        )
    resp = _post(client, "write-output-file", tid, token, file_name="o.txt", content_base64="")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"
    async with pg.engine.connect() as conn:
        n = (
            await conn.execute(
                text("SELECT count(*) FROM task_files WHERE task_id = :t"), {"t": tid}
            )
        ).scalar_one()
    assert n == 0


async def test_registry_miss_and_stale_instance_401(client, tools_env, pg):
    """登记表核验：未登记令牌 / 他实例令牌（同 claims 异 instance）→ 401。"""
    uid, tid, rid, epoch = await _seed_running_minimal(pg, "t7-reg@x.test")
    unregistered = create_v2_task_token(
        task_id=tid, owner_id=uid, round_id=rid, lease_epoch=epoch, instance="ghost"
    )
    resp = _post(client, "list-task-files", tid, unregistered)
    assert resp.status_code == 401
    registered = tools_env.register(uid, tid, rid, epoch, instance="inst-a")
    stale = create_v2_task_token(
        task_id=tid, owner_id=uid, round_id=rid, lease_epoch=epoch, instance="inst-old"
    )
    resp = _post(client, "list-task-files", tid, stale)
    assert resp.status_code == 401
    resp = _post(client, "list-task-files", tid, registered)
    assert resp.status_code == 200


async def test_ready_state_callback_401(client, tools_env, pg):
    """ready 态回调 4xx（契约 I10）：轮收口（settle 弹登记表）→ 令牌不在登记表
    → 401（无活跃轮令牌）。"""
    uid, tid, rid, epoch = await _seed_running_minimal(pg, "t7-ready@x.test")
    tools_env.register(uid, tid, rid, epoch)
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE task_rounds SET state = 'settled', lease_owner = NULL, "
                "lease_expires_at = NULL WHERE id = :r"
            ),
            {"r": rid},
        )
        await conn.execute(text("UPDATE tasks SET status = 'ready' WHERE id = :t"), {"t": tid})
    tools_env.executor.tokens.pop(rid, None)  # settle 收尾路径弹出（T6b）
    resp = _post(
        client,
        "query-task-state",
        tid,
        create_v2_task_token(
            task_id=tid, owner_id=uid, round_id=rid, lease_epoch=epoch, instance="itest-inst"
        ),
    )
    assert resp.status_code == 401


async def test_disabled_tool_403(client, tools_env, pg):
    """kill switch 第二校验（回调面）：目录停用 read_task_file@1 → 403
    TOOL_REVOKED（令牌链全绿仍拦）。"""
    uid, tid, rid, epoch = await _seed_running_with_inputs(
        pg, tools_env.rt.storage, "t7-disabled@x.test", [("hello.txt", b"X")]
    )
    token = tools_env.register(uid, tid, rid, epoch)
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE tool_catalog SET enabled = false "
                "WHERE tool_id = 'read_task_file' AND version = '1'"
            )
        )
    resp = _post(client, "read-task-file", tid, token, file_name="hello.txt")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "TOOL_REVOKED"


async def test_missing_or_garbage_token_401(client, tools_env, pg):
    uid, tid, rid, epoch = await _seed_running_minimal(pg, "t7-notok@x.test")
    tools_env.register(uid, tid, rid, epoch)
    resp = _post(client, "read-task-file", tid, None, file_name="hello.txt")
    assert resp.status_code == 401
    resp = _post(client, "read-task-file", tid, "garbage", file_name="hello.txt")
    assert resp.status_code == 401


async def test_task_id_mismatch_401(client, tools_env, pg):
    """claims.task_id 与请求 task_id 不一致 → 401（越权回调面）。"""
    uid = str(await seed_task_user(pg, "t7-mism@x.test"))
    pid = await seed_provider(pg, uid)  # 同 user 复用唯一默认 provider（不重种）
    tid = str(await seed_running_task(pg, uid, pid))
    rid, epoch = await _fetch_running_round(pg, tid)
    token = tools_env.register(uid, tid, rid, epoch)
    other_tid = str(await seed_task_for_provider(pg, uid, pid, status="queued"))
    resp = _post(client, "query-task-state", other_tid, token)
    assert resp.status_code == 401
