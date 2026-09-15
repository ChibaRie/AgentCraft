"""T8a 任务域路由测试（Phase 6）：CRUD/files/artifacts/quota + 幂等 + 限流。

纪律（对齐 test_v2_provider_crud / test_v2_reports_api 现行 API 测试惯例）：
make_v2_runtime + app 依赖 override（provider_env）+ 真实登录流（双 cookie +
CSRF 头）；owner-RLS 种子一律 superuser；触盘用例经 api_env 把任务存储根钉到
tmp_path（防污染仓内 ./data/task-storage）。限流窗口依赖每测试独立克隆库。
"""

import base64
import uuid as _uuid

import pytest
from sqlalchemy import text

from backend.v2.ids import uuid7
from backend.v2.rate_limit import LIMITS, hmac_subject
from backend.v2.task_storage import TaskStorage
from tests.v2_provider_helpers import (
    auth_client,
    login,
    seed_active_user,
    seed_provider,
    seed_task_for_provider,
)
from tests.v2_task_helpers import (
    seed_input_file,
    seed_published_revision,
    seed_running_task,
)

_PASSWORD = "User-Passw0rd!"
_D14_VIEW_KEYS = {
    "id",
    "status",
    "abort_reason",
    "created_at",
    "input_committed",
    "input_manifest_sha256",
    "event_sequence",
    "active_round",
    "initial_round",
    "counts",
}


# ---------------------------------------------------------------------------
# 种子 / 登录助手
# ---------------------------------------------------------------------------


@pytest.fixture
async def api_env(provider_env, tmp_path):
    """provider_env 基础上把任务物理存储根钉到 tmp（路由触盘用例统一入口）。"""
    provider_env.storage = TaskStorage(tmp_path / "task-storage")
    return provider_env


async def _seed_domain(pg, email: str) -> tuple[str, str, str]:
    """登录态用户（真实密码哈希）+ 默认位 provider + published revision。"""
    uid = await seed_active_user(pg, email)
    pid = await seed_provider(pg, uid, is_default=True)
    rid = await seed_published_revision(pg, uid)
    return str(uid), str(pid), str(rid)


async def _login(pg, email: str):
    client = auth_client()
    await login(client, email, _PASSWORD)
    return client


async def _create_task(
    client,
    rid: str,
    pid: str | None = None,
    *,
    idem: str = "t8a-create-1",
    initial: str = "第一句话",
):
    body = {"expert_revision_id": rid, "initial_message": initial}
    if pid is not None:
        body["provider_id"] = pid
    return await client.post("/api/v2/tasks", json=body, headers={"Idempotency-Key": idem})


async def _upload(client, task_id: str, idem: str, payloads: list[tuple[str, bytes]]):
    files = [("files", (name, content)) for name, content in payloads]
    return await client.post(
        f"/api/v2/tasks/{task_id}/files", files=files, headers={"Idempotency-Key": idem}
    )


async def _scalar(pg, sql: str, params: dict | None = None):
    async with pg.engine.connect() as conn:
        return (await conn.execute(text(sql), params or {})).scalar_one()


# ---------------------------------------------------------------------------
# 创建：201 形态 / 幂等重放 / 冲突 / provider 解析
# ---------------------------------------------------------------------------


async def test_create_task_201_shape_and_replay(pg, api_env):
    uid, pid, rid = await _seed_domain(pg, "t8a-create@example.com")
    client = await _login(pg, "t8a-create@example.com")
    first = await _create_task(client, rid, pid)
    assert first.status_code == 201, first.text
    data = first.json()["data"]
    assert data["task"]["status"] == "uploading"
    assert data["task"]["input_committed"] is False
    assert data["message"]["event_sequence"] == 1
    replay = await _create_task(client, rid, pid, idem="t8a-create-1")
    assert replay.status_code == 201
    assert replay.json() == first.json()
    n = await _scalar(pg, "SELECT count(*) FROM tasks WHERE owner_id = :u", {"u": uid})
    assert n == 1


async def test_create_conflict_same_key_different_payload(pg, api_env):
    _, pid, rid = await _seed_domain(pg, "t8a-conflict@example.com")
    client = await _login(pg, "t8a-conflict@example.com")
    await _create_task(client, rid, pid, idem="t8a-cf-1")
    conflict = await _create_task(client, rid, pid, idem="t8a-cf-1", initial="换一句话")
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


async def test_create_default_provider_fallback_and_not_configured(pg, api_env):
    """缺省回退（默认位 active 行）与 PROVIDER_NOT_CONFIGURED（无默认位）。"""
    uid, pid, rid = await _seed_domain(pg, "t8a-fallback@example.com")
    client = await _login(pg, "t8a-fallback@example.com")
    ok = await _create_task(client, rid, pid=None, idem="t8a-fb-1")
    assert ok.status_code == 201, ok.text
    # 同用户第二任务：默认位仍在（is_default 未变）→ 仍走回退
    ok2 = await _create_task(client, rid, pid=None, idem="t8a-fb-2")
    assert ok2.status_code == 201
    # 无默认位用户（provider 均非默认）→ 400 PROVIDER_NOT_CONFIGURED
    nodata_uid = await seed_active_user(pg, "t8a-nodefault@example.com")
    await seed_provider(pg, nodata_uid, is_default=False)
    nodata_client = await _login(pg, "t8a-nodefault@example.com")
    denied = await nodata_client.post(
        "/api/v2/tasks",
        json={"expert_revision_id": rid, "initial_message": "x"},
        headers={"Idempotency-Key": "t8a-nd-1"},
    )
    assert denied.status_code == 400
    assert denied.json()["error"]["code"] == "PROVIDER_NOT_CONFIGURED"


async def test_create_requires_idempotency_key(pg, api_env):
    _, pid, rid = await _seed_domain(pg, "t8a-nokey@example.com")
    client = await _login(pg, "t8a-nokey@example.com")
    resp = await client.post(
        "/api/v2/tasks", json={"expert_revision_id": rid, "initial_message": "x"}
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 认证 / CSRF 负例 + 统一 404
# ---------------------------------------------------------------------------


async def test_auth_negative_401_and_csrf_negative_403(pg, api_env):
    _, pid, rid = await _seed_domain(pg, "t8a-auth@example.com")
    anon = auth_client()
    resp = await anon.get("/api/v2/tasks")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "SESSION_EXPIRED"
    client = await _login(pg, "t8a-auth@example.com")
    client.headers.pop("X-CSRF-Token", None)
    resp = await _create_task(client, rid, pid, idem="t8a-csrf-1")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "CSRF_INVALID"


async def test_cross_user_unified_404(pg, api_env):
    _, pid, rid = await _seed_domain(pg, "t8a-owner@example.com")
    client_a = await _login(pg, "t8a-owner@example.com")
    created = await _create_task(client_a, rid, pid, idem="t8a-x-1")
    task_id = created.json()["data"]["task"]["id"]
    await seed_active_user(pg, "t8a-intruder@example.com")
    client_b = await _login(pg, "t8a-intruder@example.com")
    seen = await client_b.get(f"/api/v2/tasks/{task_id}")
    assert seen.status_code == 404
    assert seen.json()["error"]["code"] == "TASK_NOT_FOUND"
    removed = await client_b.delete(
        f"/api/v2/tasks/{task_id}", headers={"Idempotency-Key": "t8a-x-del-1"}
    )
    assert removed.status_code == 404
    assert removed.json()["error"]["code"] == "TASK_NOT_FOUND"


# ---------------------------------------------------------------------------
# commit / abort / complete / delete：幂等与 202 映射
# ---------------------------------------------------------------------------


async def test_commit_shape_replay_conflict_and_state_gate(pg, api_env):
    _, pid, rid = await _seed_domain(pg, "t8a-commit@example.com")
    client = await _login(pg, "t8a-commit@example.com")
    created = await _create_task(client, rid, pid, idem="t8a-cm-0")
    task_id = created.json()["data"]["task"]["id"]
    manifest = [{"file_name": "a.txt", "sha256": "c" * 64, "size": 5}]
    first = await client.post(
        f"/api/v2/tasks/{task_id}/input/commit",
        json={"manifest": manifest},
        headers={"Idempotency-Key": "t8a-cm-1"},
    )
    assert first.status_code == 200, first.text
    data = first.json()["data"]
    assert set(data.keys()) == {"task", "manifest_sha256", "round_id", "event_sequence"}
    assert data["task"]["status"] == "queued"
    assert data["task"]["input_committed"] is True
    replay = await client.post(
        f"/api/v2/tasks/{task_id}/input/commit",
        json={"manifest": manifest},
        headers={"Idempotency-Key": "t8a-cm-1"},
    )
    assert replay.status_code == 200
    assert replay.json() == first.json()
    conflict = await client.post(
        f"/api/v2/tasks/{task_id}/input/commit",
        json={"manifest": []},
        headers={"Idempotency-Key": "t8a-cm-1"},
    )
    assert conflict.status_code == 409
    # 幂等未命中（新 key）→ 状态门 INPUT_COMMITTED 409
    again = await client.post(
        f"/api/v2/tasks/{task_id}/input/commit",
        json={"manifest": manifest},
        headers={"Idempotency-Key": "t8a-cm-2"},
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "INPUT_COMMITTED"


@pytest.mark.parametrize(
    ("endpoint", "expected_pending"),
    [
        ("abort", "aborted"),
        ("complete", "completed"),
    ],
)
async def test_running_terminal_202_shape_and_replay(pg, api_env, endpoint, expected_pending):
    """running 分支（abort/complete 共用 D19 路径）：202 {task, round:{state:cancelling}}；
    重放原响应无论当前状态。"""
    email = f"t8a-{endpoint}@example.com"
    uid = await seed_active_user(pg, email)
    pid = await seed_provider(pg, uid)
    # seed_running_task：running 任务全套持有物（活跃轮/running 账/slot 租约）
    task_id = str(await seed_running_task(pg, uid, pid))
    client = await _login(pg, email)
    first = await client.post(
        f"/api/v2/tasks/{task_id}/{endpoint}", headers={"Idempotency-Key": f"t8a-{endpoint}-1"}
    )
    assert first.status_code == 202, first.text
    data = first.json()["data"]
    assert data["task"]["status"] == "running"
    assert data["round"] == {"state": "cancelling"}
    replay = await client.post(
        f"/api/v2/tasks/{task_id}/{endpoint}", headers={"Idempotency-Key": f"t8a-{endpoint}-1"}
    )
    assert replay.status_code == 202
    assert replay.json() == first.json()
    status = await _scalar(pg, "SELECT status FROM tasks WHERE id = :t", {"t": _uuid.UUID(task_id)})
    pending = await _scalar(
        pg,
        "SELECT COALESCE(pending_terminal, '') FROM tasks WHERE id = :t",
        {"t": _uuid.UUID(task_id)},
    )
    assert (status, pending) == ("running", expected_pending)
    round_state = await _scalar(
        pg, "SELECT state FROM task_rounds WHERE task_id = :t", {"t": _uuid.UUID(task_id)}
    )
    assert round_state == "cancelling"


async def test_terminal_direct_edges_200(pg, api_env):
    """queued abort / ready complete 直翻分支：200 无 round 键；complete 重放。"""
    uid = await seed_active_user(pg, "t8a-direct@example.com")
    pid = await seed_provider(pg, uid)
    queued_id = str(await seed_task_for_provider(pg, uid, pid, status="queued"))
    ready_id = str(await seed_task_for_provider(pg, uid, pid, status="ready"))
    client = await _login(pg, "t8a-direct@example.com")
    aborted = await client.post(
        f"/api/v2/tasks/{queued_id}/abort", headers={"Idempotency-Key": "t8a-dt-1"}
    )
    assert aborted.status_code == 200, aborted.text
    assert aborted.json()["data"]["task"]["status"] == "aborted"
    assert "round" not in aborted.json()["data"]
    reason = await _scalar(
        pg, "SELECT abort_reason FROM tasks WHERE id = :t", {"t": _uuid.UUID(queued_id)}
    )
    assert reason == "user_cancel"
    completed = await client.post(
        f"/api/v2/tasks/{ready_id}/complete", headers={"Idempotency-Key": "t8a-dt-2"}
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["data"]["task"]["status"] == "completed"
    replay = await client.post(
        f"/api/v2/tasks/{ready_id}/complete", headers={"Idempotency-Key": "t8a-dt-2"}
    )
    assert replay.status_code == 200
    assert replay.json() == completed.json()


async def test_delete_task_replay_then_404(pg, api_env):
    """DELETE：重放原 200（重试语义）；幂等记录仍活着的新 key → 业务 404。"""
    _, pid, rid = await _seed_domain(pg, "t8a-del@example.com")
    client = await _login(pg, "t8a-del@example.com")
    created = await _create_task(client, rid, pid, idem="t8a-dl-0")
    task_id = created.json()["data"]["task"]["id"]
    first = await client.delete(f"/api/v2/tasks/{task_id}", headers={"Idempotency-Key": "t8a-dl-1"})
    assert first.status_code == 200, first.text
    assert first.json()["data"]["task"] == {"id": task_id, "status": "deleted"}
    replay = await client.delete(
        f"/api/v2/tasks/{task_id}", headers={"Idempotency-Key": "t8a-dl-1"}
    )
    assert replay.status_code == 200
    assert replay.json() == first.json()
    fresh_key = await client.delete(
        f"/api/v2/tasks/{task_id}", headers={"Idempotency-Key": "t8a-dl-2"}
    )
    assert fresh_key.status_code == 404
    assert fresh_key.json()["error"]["code"] == "TASK_NOT_FOUND"


async def test_delete_post_commit_physical_deletion(pg, api_env, tmp_path):
    """post-commit 物理删（T3 裁决）：上传触盘 → DELETE 提交后任务树全清。"""
    _, pid, rid = await _seed_domain(pg, "t8a-rm@example.com")
    client = await _login(pg, "t8a-rm@example.com")
    created = await _create_task(client, rid, pid, idem="t8a-rm-0")
    task_id = created.json()["data"]["task"]["id"]
    uploaded = await _upload(client, task_id, "t8a-rm-f1", [("a.txt", b"artifact-payload")])
    assert uploaded.status_code == 200, uploaded.text
    task_tree = tmp_path / "task-storage" / "tasks" / task_id
    assert task_tree.is_dir()
    first = await client.delete(f"/api/v2/tasks/{task_id}", headers={"Idempotency-Key": "t8a-rm-1"})
    assert first.status_code == 200
    assert not task_tree.exists()
    assert not (tmp_path / "task-storage" / "artifacts" / task_id).exists()


# ---------------------------------------------------------------------------
# 文件上传/列表/删除
# ---------------------------------------------------------------------------


async def test_upload_files_replay_conflict_and_list(pg, api_env):
    _, pid, rid = await _seed_domain(pg, "t8a-up@example.com")
    client = await _login(pg, "t8a-up@example.com")
    created = await _create_task(client, rid, pid, idem="t8a-up-0")
    task_id = created.json()["data"]["task"]["id"]
    payloads = [("a.txt", b"alpha"), ("b.txt", b"beta!")]
    first = await _upload(client, task_id, "t8a-up-1", payloads)
    assert first.status_code == 200, first.text
    files = first.json()["data"]["files"]
    assert [f["file_name"] for f in files] == ["a.txt", "b.txt"]
    assert all(f["state"] == "staged" for f in files)
    assert set(files[0].keys()) == {"id", "file_name", "sha256", "size_bytes", "state"}
    listing = await client.get(f"/api/v2/tasks/{task_id}/files", params={"direction": "input"})
    assert listing.status_code == 200
    # 同批文件 created_at 同值（事务时钟）——稳定序由 id 兜底，批内顺序不钉声明序
    assert {f["file_name"] for f in listing.json()["data"]} == {"a.txt", "b.txt"}
    # 幂等重放：同名同尺寸元数据 → 原响应，无重复行
    replay = await _upload(client, task_id, "t8a-up-1", payloads)
    assert replay.status_code == 200
    assert replay.json() == first.json()
    n = await _scalar(
        pg,
        "SELECT count(*) FROM task_files WHERE task_id = :t AND state != 'deleted'",
        {"t": _uuid.UUID(task_id)},
    )
    assert n == 2
    # 同 key 异元数据（换名）→ 409
    conflict = await _upload(client, task_id, "t8a-up-1", [("c.txt", b"alpha")])
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    # direction 词表外 → 400
    bad = await client.get(f"/api/v2/tasks/{task_id}/files", params={"direction": "bogus"})
    assert bad.status_code == 400


async def test_delete_file_idempotent_then_404(pg, api_env):
    _, pid, rid = await _seed_domain(pg, "t8a-df@example.com")
    client = await _login(pg, "t8a-df@example.com")
    created = await _create_task(client, rid, pid, idem="t8a-df-0")
    task_id = created.json()["data"]["task"]["id"]
    uploaded = await _upload(client, task_id, "t8a-df-f1", [("gone.txt", b"bye")])
    fid = uploaded.json()["data"]["files"][0]["id"]
    first = await client.delete(
        f"/api/v2/tasks/{task_id}/files/{fid}", headers={"Idempotency-Key": "t8a-df-1"}
    )
    assert first.status_code == 200, first.text
    assert first.json()["data"]["file"] == {"id": fid, "state": "deleted"}
    replay = await client.delete(
        f"/api/v2/tasks/{task_id}/files/{fid}", headers={"Idempotency-Key": "t8a-df-1"}
    )
    assert replay.status_code == 200
    assert replay.json() == first.json()
    fresh_key = await client.delete(
        f"/api/v2/tasks/{task_id}/files/{fid}", headers={"Idempotency-Key": "t8a-df-2"}
    )
    assert fresh_key.status_code == 404
    assert fresh_key.json()["error"]["code"] == "FILE_NOT_FOUND"


# ---------------------------------------------------------------------------
# 产物列表 + 下载头 + 任务级 quota
# ---------------------------------------------------------------------------


async def _seed_registered_artifact(
    pg, storage: TaskStorage, uid: str, task_id: str, name: str, payload: bytes
) -> str:
    """superuser 种 registered 产物行（produced_in_round_id 非空）+ 物理登记副本
    （写 api_env.storage 派生的 artifacts 路径——resolve_download 的消费面）。"""
    fid = await seed_input_file(
        pg,
        uid,
        task_id,
        size_bytes=len(payload),
        state="registered",
        direction="output",
        file_name=name,
    )
    async with pg.engine.begin() as conn:
        key = (
            await conn.execute(
                text("SELECT storage_key FROM task_files WHERE id = :i"), {"i": _uuid.UUID(fid)}
            )
        ).scalar_one()
        # produced_in_round_id FK → task_rounds：先种消息+轮，再回填产物轮次
        # （API 创建的任务初始消息已占 event_sequence=1——seed 消息用 2 防唯一冲突）
        message_id = (
            await conn.execute(
                text(
                    "INSERT INTO task_messages (id, task_id, owner_id, event_sequence, "
                    "author, content) VALUES (gen_random_uuid(), :t, :u, 2, 'user', 'seed') "
                    "RETURNING id"
                ),
                {"t": _uuid.UUID(task_id), "u": _uuid.UUID(uid)},
            )
        ).scalar_one()
        round_id = (
            await conn.execute(
                text(
                    "INSERT INTO task_rounds (id, task_id, owner_id, source_message_id, "
                    "state, lease_epoch, attempt) "
                    "VALUES (gen_random_uuid(), :t, :u, :m, 'pending', 0, 0) RETURNING id"
                ),
                {"t": _uuid.UUID(task_id), "u": _uuid.UUID(uid), "m": message_id},
            )
        ).scalar_one()
        await conn.execute(
            text("UPDATE task_files SET produced_in_round_id = :r WHERE id = :i"),
            {"r": round_id, "i": _uuid.UUID(fid)},
        )
    parts = str(key).split("/")
    storage.artifact_path(parts[1], parts[2]).write_bytes(payload)
    return fid


async def test_artifacts_list_and_download_headers(pg, api_env):
    uid, pid, rid = await _seed_domain(pg, "t8a-art@example.com")
    client = await _login(pg, "t8a-art@example.com")
    created = await _create_task(client, rid, pid, idem="t8a-art-0")
    task_id = created.json()["data"]["task"]["id"]
    payload = b"artifact-bytes"
    fid = await _seed_registered_artifact(pg, api_env.storage, uid, task_id, "结果.txt", payload)
    listing = await client.get(f"/api/v2/tasks/{task_id}/artifacts")
    assert listing.status_code == 200, listing.text
    items = listing.json()["data"]
    assert len(items) == 1
    assert set(items[0].keys()) == {
        "id",
        "file_name",
        "sha256",
        "size_bytes",
        "state",
        "produced_in_round_id",
    }
    assert items[0]["produced_in_round_id"] is not None
    download = await client.get(f"/api/v2/tasks/{task_id}/artifacts/{fid}/download")
    assert download.status_code == 200
    assert download.content == payload
    disposition = download.headers["content-disposition"]
    assert disposition.startswith("attachment")
    assert "filename*=UTF-8''" in disposition
    assert download.headers["x-content-type-options"] == "nosniff"


async def test_task_quota_view_and_owner_quota(pg, api_env):
    _, pid, rid = await _seed_domain(pg, "t8a-quota@example.com")
    client = await _login(pg, "t8a-quota@example.com")
    created = await _create_task(client, rid, pid, idem="t8a-qt-0")
    task_id = created.json()["data"]["task"]["id"]
    await _upload(client, task_id, "t8a-qt-f1", [("a.txt", b"x" * 100)])
    view = await client.get(f"/api/v2/tasks/{task_id}/quota")
    assert view.status_code == 200, view.text
    data = view.json()["data"]
    assert data["usage"]["inputs_count"] == 1
    assert data["usage"]["inputs_bytes"] == 100
    assert data["usage"]["outputs_count"] == 0
    assert data["usage"]["total_bytes"] == 100
    assert data["limits"] == {
        "max_files_per_task": 10,
        "max_single_file_bytes": 5 * 1024 * 1024,
        "max_task_bytes": 10 * 1024 * 1024,
    }
    assert data["input_frozen"] is False
    committed = await client.post(
        f"/api/v2/tasks/{task_id}/input/commit",
        json={"manifest": []},
        headers={"Idempotency-Key": "t8a-qt-1"},
    )
    assert committed.status_code == 200
    after = await client.get(f"/api/v2/tasks/{task_id}/quota")
    assert after.json()["data"]["input_frozen"] is True
    owner = await client.get("/api/v2/quota")
    assert owner.status_code == 200
    owner_data = owner.json()["data"]
    assert owner_data["usage"]["tasks_started_today"] == 1
    assert owner_data["limits"]["max_daily_tasks"] == 5
    assert owner_data["limits"]["max_retained_storage_bytes"] == 1_073_741_824


# ---------------------------------------------------------------------------
# 列表分页
# ---------------------------------------------------------------------------


async def test_list_tasks_pagination_and_deleted_hiding(pg, api_env):
    _, pid, rid = await _seed_domain(pg, "t8a-list@example.com")
    client = await _login(pg, "t8a-list@example.com")
    first = await _create_task(client, rid, pid, idem="t8a-ls-1")
    second = await _create_task(client, rid, pid, idem="t8a-ls-2")
    listing = await client.get("/api/v2/tasks")
    assert listing.status_code == 200
    data = listing.json()["data"]
    assert data["total"] == 2 and data["page"] == 1 and data["size"] == 20
    assert {t["id"] for t in data["items"]} == {
        first.json()["data"]["task"]["id"],
        second.json()["data"]["task"]["id"],
    }
    assert set(data["items"][0].keys()) == _D14_VIEW_KEYS
    removed = await client.delete(
        f"/api/v2/tasks/{first.json()['data']['task']['id']}",
        headers={"Idempotency-Key": "t8a-ls-del"},
    )
    assert removed.status_code == 200
    after = await client.get("/api/v2/tasks")
    assert after.json()["data"]["total"] == 1
    bad = await client.get("/api/v2/tasks", params={"page": 0})
    assert bad.status_code == 400


# ---------------------------------------------------------------------------
# 限流（D12）：task_create / upload 路由挂接 + 注册表登记钉
# ---------------------------------------------------------------------------


def test_rate_limit_registry_pins(monkeypatch):
    """D12 四 scope 登记值 + "task" kind 扩展（sse_connect/send_message 挂接归 T8b）。"""
    monkeypatch.setenv("RATE_LIMIT_HMAC_KEY", base64.urlsafe_b64encode(bytes(range(32))).decode())
    assert LIMITS["task_create"] == (30, 86400)
    assert LIMITS["upload"] == (60, 3600)
    assert LIMITS["send_message"] == (60, 3600)
    assert LIMITS["sse_connect"] == (60, 3600)
    digest = hmac_subject("task", "task-subject")
    assert len(digest) == 64 and int(digest, 16) >= 0
    with pytest.raises(ValueError):
        hmac_subject("bogus", "x")


async def test_rate_limit_task_create_429(pg, api_env):
    await _seed_domain(pg, "t8a-rl-create@example.com")
    client = await _login(pg, "t8a-rl-create@example.com")
    # 空请求体 → 400/422 校验失败（限流依赖先于 body 校验消耗窗口）
    for i in range(30):
        resp = await client.post("/api/v2/tasks", headers={"Idempotency-Key": f"t8a-rlc-{i}"})
        assert resp.status_code in (400, 422), resp.text
    over = await client.post("/api/v2/tasks", headers={"Idempotency-Key": "t8a-rlc-over"})
    assert over.status_code == 429
    assert "retry-after" in {k.lower() for k in over.headers.keys()}
    assert over.json()["error"]["code"] == "TOO_MANY_REQUESTS"


async def test_rate_limit_upload_429(pg, api_env):
    await _seed_domain(pg, "t8a-rl-upload@example.com")
    client = await _login(pg, "t8a-rl-upload@example.com")
    target = f"/api/v2/tasks/{uuid7()}/files"
    for i in range(60):
        resp = await client.post(target, headers={"Idempotency-Key": f"t8a-rlu-{i}"})
        assert resp.status_code in (400, 422), resp.text
    over = await client.post(target, headers={"Idempotency-Key": "t8a-rlu-over"})
    assert over.status_code == 429
    assert "retry-after" in {k.lower() for k in over.headers.keys()}
    assert over.json()["error"]["code"] == "TOO_MANY_REQUESTS"
