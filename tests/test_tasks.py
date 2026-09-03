"""任务与对话 API 测试（Engineering Spec §6.6 + DB 设计 §3.5-§3.8/§5.5/§6）。

- 创建任务：workdir 只接受授权根内相对路径（缺省/空 = 根），存储派生值
  /workspaces/authorized[/<relative>]；专家必须 published（否则 404）
- 快照冻结：expert_name/avatar、skill_snapshot（enabled ∩ published 的完整内容）、
  mcp_snapshot（v1 尚无 MCP 绑定 → tools: []）；创建后专家/Skill 变更不影响快照
- 消息 SSE：POST /api/tasks/{id}/messages 返回 text/event-stream，
  帧序 meta → text_delta×N → message_saved → done，schema 严格按 §6.6
- 状态机：created --首条消息--> running（failed 可重试回 running，completed 409，
  专家下架 409）；用户消息先落库，assistant 完整回复经 message_saved 落库
- 文件上传：仅 task.status=created 且无用户消息；文件名/单文件/数量/任务配额校验；
  失败补偿（整批回滚，不留孤儿文件）
"""

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from backend.dependencies import get_file_service, get_workspace_root
from backend.main import app
from backend.models.conversation import Conversation
from backend.models.task import Task
from backend.services.file_service import FilenameInvalidError, FileService, sweep_stale_storage
from tests.test_experts import bind_skill, create_expert, valid_expert
from tests.test_skills import auth_header, create_skill, register_expert

pytestmark = pytest.mark.usefixtures("client")


# ---------------------------------------------------------------------------
# 夹具与助手
# ---------------------------------------------------------------------------


@pytest.fixture()
def workspace_root(tmp_path: Path):
    root = tmp_path / "workspaces"
    (root / "proj").mkdir(parents=True)
    (root / "proj" / "sub").mkdir()
    app.dependency_overrides[get_workspace_root] = lambda: root
    yield root
    app.dependency_overrides.pop(get_workspace_root, None)


@pytest.fixture()
def file_root(tmp_path: Path):
    root = tmp_path / "data" / "task-files"
    app.dependency_overrides[get_file_service] = lambda: FileService(root)
    yield root
    app.dependency_overrides.pop(get_file_service, None)


def make_published_expert(client, username="tasker"):
    """注册 → 建 Skill（发布）→ 建专家 → 绑定并启用 → 发布专家。"""
    token, user_id = register_expert(client, username=username)
    skill_id = make_published_skill(client, token)
    response = create_expert(client, token)
    assert response.status_code == 201
    expert_id = response.json()["data"]["id"]
    assert bind_skill(client, token, expert_id, skill_id).status_code == 201
    enabled = client.put(
        f"/api/experts/{expert_id}/skills/{skill_id}",
        json={"enabled": True},
        headers=auth_header(token),
    )
    assert enabled.status_code == 200
    published = client.post(f"/api/experts/{expert_id}/publish", headers=auth_header(token))
    assert published.status_code == 200
    return token, user_id, expert_id, skill_id


def make_published_skill(client, token, **overrides):
    response = create_skill(client, token, **overrides)
    assert response.status_code == 201
    skill_id = response.json()["data"]["id"]
    published = client.post(f"/api/skills/{skill_id}/publish", headers=auth_header(token))
    assert published.status_code == 200
    return skill_id


def create_task(client, token, expert_id, description="帮我整理本周的技术周报", **overrides):
    payload = {"expert_id": expert_id, "description": description, **overrides}
    return client.post("/api/tasks", json=payload, headers=auth_header(token))


def seed_task(test_db, user_id, expert_id, status="created"):
    async def _seed():
        async with test_db.session_factory() as session:
            task = Task(
                user_id=user_id,
                expert_id=expert_id,
                expert_name_snapshot="技术周报专家",
                title="整理本周技术周报",
                status=status,
                skill_snapshot=json.dumps({"skills": [], "loaded_at": "2026-09-02T00:00:00Z"}),
                mcp_snapshot=json.dumps({"tools": [], "loaded_at": "2026-09-02T00:00:00Z"}),
                workdir="/workspaces/authorized",
            )
            session.add(task)
            await session.flush()
            session.add(Conversation(task_id=task.id))
            await session.commit()
            return task.id

    return asyncio.run(_seed())


def parse_sse(text):
    """解析 SSE 文本为 [(event, payload)]，并校验帧格式（event:/data:/空行分隔）。"""
    frames = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        event_name = None
        data_parts = []
        for line in block.split("\n"):
            if line.startswith("event:"):
                event_name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_parts.append(line[len("data:") :].strip())
        assert event_name is not None, f"帧缺少 event 行: {block!r}"
        payload = json.loads("\n".join(data_parts)) if data_parts else {}
        frames.append((event_name, payload))
    return frames


def send_message(client, token, task_id, content):
    return client.post(
        f"/api/tasks/{task_id}/messages",
        json={"content": content},
        headers={**auth_header(token), "Accept": "text/event-stream"},
    )


# ---------------------------------------------------------------------------
# GET /api/workspaces（工作目录浏览）
# ---------------------------------------------------------------------------


def test_workspaces_require_authentication(client):
    assert client.get("/api/workspaces").status_code == 401


def test_workspaces_list_root(client, workspace_root):
    token, _, _, _ = make_published_expert(client)
    response = client.get("/api/workspaces", headers=auth_header(token))
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["path"] == ""
    assert data["root_label"]
    assert [item["name"] for item in data["directories"]] == ["proj"]
    assert data["directories"][0]["relative_path"] == "proj"


def test_workspaces_browse_subdirectory(client, workspace_root):
    token, _, _, _ = make_published_expert(client)
    response = client.get("/api/workspaces", params={"path": "proj"}, headers=auth_header(token))
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["path"] == "proj"
    assert [item["relative_path"] for item in data["directories"]] == ["proj/sub"]


def test_workspaces_reject_invalid_paths(client, workspace_root):
    token, _, _, _ = make_published_expert(client)
    for bad in ("..", "a/../b", "/etc", "C:\\x"):
        response = client.get("/api/workspaces", params={"path": bad}, headers=auth_header(token))
        assert response.status_code == 400, f"path={bad!r}"
    missing = client.get(
        "/api/workspaces", params={"path": "ghost"}, headers=auth_header(token)
    )
    assert missing.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/tasks（创建 + workdir 校验 + 快照冻结）
# ---------------------------------------------------------------------------


def test_create_task_requires_authentication(client):
    assert client.post("/api/tasks", json={"expert_id": 1, "description": "x"}).status_code == 401


def test_create_task_happy_path(client, workspace_root):
    token, _, expert_id, _ = make_published_expert(client)
    response = create_task(client, token, expert_id)
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["task_id"] > 0
    assert data["conversation_id"] > 0
    assert data["status"] == "created"
    assert data["workdir"] == "/workspaces/authorized"


def test_create_task_derives_relative_workdir(client, workspace_root):
    token, _, expert_id, _ = make_published_expert(client)
    response = create_task(client, token, expert_id, workdir="proj/sub")
    assert response.status_code == 201
    assert response.json()["data"]["workdir"] == "/workspaces/authorized/proj/sub"


def test_create_task_rejects_invalid_workdir(client, workspace_root):
    token, _, expert_id, _ = make_published_expert(client)
    for bad in ("/etc", "..", "a/../b", "C:\\x", "a\\b"):
        response = create_task(client, token, expert_id, workdir=bad)
        assert response.status_code == 400, f"workdir={bad!r}"
        assert response.json()["error"]["code"] == "WORKDIR_INVALID"


def test_create_task_rejects_missing_workdir(client, workspace_root):
    token, _, expert_id, _ = make_published_expert(client)
    response = create_task(client, token, expert_id, workdir="ghost")
    assert response.status_code == 400


def test_create_task_rejects_windows_alias_workdir(client, workspace_root):
    """结尾点/空格在 Win32 下会被剥除产生路径别名，必须拒绝。"""
    token, _, expert_id, _ = make_published_expert(client)
    for bad in ("sub.", "sub ", "sub .", ".sub", "a/sub.", "a /b"):
        response = create_task(client, token, expert_id, workdir=bad)
        assert response.status_code == 400, f"workdir={bad!r}"


def test_create_task_rejects_overlong_workdir(client, workspace_root):
    """派生存储值超过 tasks.workdir VARCHAR(500) 时拒绝（§3.5）。"""
    token, _, expert_id, _ = make_published_expert(client)
    (workspace_root / "longdir").mkdir()
    response = create_task(client, token, expert_id, workdir="longdir/" + "段" * 250)
    assert response.status_code == 400


def test_create_task_404_when_expert_not_published(client):
    token, _, expert_id, skill_id = make_published_expert(client, username="owner404")
    # draft 专家不可召唤
    draft = create_expert(client, token)
    draft_id = draft.json()["data"]["id"]
    assert create_task(client, token, draft_id).status_code == 404
    # 绑定并启用 Skill 后发布 → published 可召唤
    assert bind_skill(client, token, draft_id, skill_id).status_code == 201
    assert (
        client.put(
            f"/api/experts/{draft_id}/skills/{skill_id}",
            json={"enabled": True},
            headers=auth_header(token),
        ).status_code
        == 200
    )
    published = client.post(f"/api/experts/{draft_id}/publish", headers=auth_header(token))
    assert published.status_code == 200
    assert create_task(client, token, draft_id).status_code == 201
    # offline 专家不可召唤
    offline = client.post(f"/api/experts/{draft_id}/offline", headers=auth_header(token))
    assert offline.status_code == 200
    assert create_task(client, token, draft_id).status_code == 404
    # 不存在的专家
    assert create_task(client, token, 99999).status_code == 404


def test_create_task_rejects_blank_description(client):
    token, _, expert_id, _ = make_published_expert(client)
    response = create_task(client, token, expert_id, description="   ")
    assert response.status_code == 400


def test_create_task_title_truncates_to_200(client):
    token, _, expert_id, _ = make_published_expert(client)
    long_description = "描" * 250
    response = create_task(client, token, expert_id, description=long_description)
    assert response.status_code == 201
    task_id = response.json()["data"]["task_id"]
    detail = client.get(f"/api/tasks/{task_id}", headers=auth_header(token))
    assert detail.status_code == 200
    assert detail.json()["data"]["title"] == long_description[:200]


def test_create_task_snapshots_frozen(client, test_db):
    token, _, expert_id, skill_id = make_published_expert(client)
    # 再绑定一个 published 但未启用的 Skill：不应进入快照
    second_skill = make_published_skill(client, token, name="未启用能力包")
    assert bind_skill(client, token, expert_id, second_skill).status_code == 201

    response = create_task(client, token, expert_id)
    task_id = response.json()["data"]["task_id"]

    async def load_task():
        async with test_db.session_factory() as session:
            return await session.get(Task, task_id)

    # 直接查库验证快照 JSON
    task = asyncio.run(load_task())
    skill_snapshot = json.loads(task.skill_snapshot)
    assert len(skill_snapshot["skills"]) == 1
    assert skill_snapshot["skills"][0]["name"] == "技术周报生成"
    assert "收集本周技术素材" in skill_snapshot["skills"][0]["content"]
    assert skill_snapshot["expert_persona"] == valid_expert()["persona"]
    assert skill_snapshot["expert_methodology"] == valid_expert()["methodology"]
    assert skill_snapshot["loaded_at"]
    mcp_snapshot = json.loads(task.mcp_snapshot)
    assert mcp_snapshot["tools"] == []
    assert mcp_snapshot["loaded_at"]
    assert task.expert_name_snapshot == "技术周报专家"

    # 冻结验证：此后专家改名 / Skill 改内容，快照不变
    rename = client.put(
        f"/api/experts/{expert_id}", json={"name": "改名后的专家"}, headers=auth_header(token)
    )
    assert rename.status_code == 200
    edited = client.put(
        f"/api/skills/{skill_id}",
        json={"goal": "快照之后修改过的目标，不应反映到已创建任务。"},
        headers=auth_header(token),
    )
    assert edited.status_code == 200

    frozen = asyncio.run(load_task())
    assert json.loads(frozen.skill_snapshot) == skill_snapshot
    assert frozen.expert_name_snapshot == "技术周报专家"


# ---------------------------------------------------------------------------
# GET /api/tasks（列表）
# ---------------------------------------------------------------------------


def test_list_tasks_empty(client):
    token, _, _, _ = make_published_expert(client)
    response = client.get("/api/tasks", headers=auth_header(token))
    assert response.status_code == 200
    data = response.json()
    assert data["data"] == []
    assert data["total"] == 0
    assert data["page"] == 1
    assert data["size"] == 20


def test_list_tasks_returns_own_newest_first(client):
    token, user_id, expert_id, _ = make_published_expert(client)
    for index in range(3):
        response = create_task(client, token, expert_id, description=f"任务{index}")
        assert response.status_code == 201
    response = client.get("/api/tasks", params={"page": 1, "size": 2}, headers=auth_header(token))
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert body["page"] == 1
    assert body["size"] == 2
    assert len(body["data"]) == 2
    assert [item["title"] for item in body["data"]] == ["任务2", "任务1"]
    card = body["data"][0]
    assert set(card) == {"id", "title", "status", "expert_name_snapshot", "created_at"}
    assert card["status"] == "created"


def test_list_tasks_excludes_other_users(client):
    token_a, _, expert_id, _ = make_published_expert(client, username="lista")
    assert create_task(client, token_a, expert_id).status_code == 201
    registered = client.post(
        "/api/auth/register",
        json={"username": "listb", "email": "listb@example.com", "password": "secret123"},
    ).json()["data"]
    response = client.get("/api/tasks", headers=auth_header(registered["token"]))
    assert response.status_code == 200
    assert response.json()["data"] == []


# ---------------------------------------------------------------------------
# GET /api/tasks/{id}（详情）
# ---------------------------------------------------------------------------


def test_get_task_detail_happy_path(client):
    token, _, expert_id, _ = make_published_expert(client)
    task_id = create_task(client, token, expert_id).json()["data"]["task_id"]
    response = client.get(f"/api/tasks/{task_id}", headers=auth_header(token))
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["id"] == task_id
    assert data["title"] == "帮我整理本周的技术周报"
    assert data["status"] == "created"
    assert data["workdir"] == "/workspaces/authorized"
    assert data["expert_name_snapshot"] == "技术周报专家"
    assert data["files"] == []
    assert data["messages"] == []


def test_get_task_detail_404_missing(client):
    token, _, _, _ = make_published_expert(client)
    assert client.get("/api/tasks/99999", headers=auth_header(token)).status_code == 404


def test_get_task_detail_403_not_owner(client):
    token_a, _, expert_id, _ = make_published_expert(client, username="detaila")
    task_id = create_task(client, token_a, expert_id).json()["data"]["task_id"]
    registered = client.post(
        "/api/auth/register",
        json={"username": "detailb", "email": "detailb@example.com", "password": "secret123"},
    ).json()["data"]
    response = client.get(f"/api/tasks/{task_id}", headers=auth_header(registered["token"]))
    assert response.status_code == 403


def test_get_task_detail_requires_authentication(client):
    assert client.get("/api/tasks/1").status_code == 401


# ---------------------------------------------------------------------------
# POST /api/tasks/{id}/messages（EchoEngine + SSE 契约）
# ---------------------------------------------------------------------------


def test_send_message_requires_authentication(client):
    assert client.post("/api/tasks/1/messages", json={"content": "hi"}).status_code == 401


def test_send_message_404_missing_task(client):
    token, _, _, _ = make_published_expert(client)
    response = send_message(client, token, 99999, "你好")
    assert response.status_code == 404


def test_send_message_403_not_owner(client):
    token_a, _, expert_id, _ = make_published_expert(client, username="sse-a")
    task_id = create_task(client, token_a, expert_id).json()["data"]["task_id"]
    registered = client.post(
        "/api/auth/register",
        json={"username": "sse-b", "email": "sse-b@example.com", "password": "secret123"},
    ).json()["data"]
    response = send_message(client, registered["token"], task_id, "你好")
    assert response.status_code == 403


def test_send_message_409_completed(client, test_db):
    token, user_id, expert_id, _ = make_published_expert(client)
    task_id = seed_task(test_db, user_id, expert_id, status="completed")
    response = send_message(client, token, task_id, "你好")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_STATE_TRANSITION"


def test_send_message_409_expert_offline(client):
    token, _, expert_id, _ = make_published_expert(client, username="offline-expert")
    task_id = create_task(client, token, expert_id).json()["data"]["task_id"]
    offline = client.post(f"/api/experts/{expert_id}/offline", headers=auth_header(token))
    assert offline.status_code == 200
    response = send_message(client, token, task_id, "你好")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EXPERT_OFFLINE"


def test_send_message_rejects_blank_content(client):
    token, _, expert_id, _ = make_published_expert(client)
    task_id = create_task(client, token, expert_id).json()["data"]["task_id"]
    response = send_message(client, token, task_id, "   ")
    assert response.status_code == 400


def test_send_message_sse_contract(client):
    """SSE 契约冻结测试：帧序、事件 schema、字段严格按 §6.6。"""
    token, _, expert_id, _ = make_published_expert(client)
    task_id = create_task(client, token, expert_id).json()["data"]["task_id"]
    content = "请帮我整理本周的技术周报"

    response = send_message(client, token, task_id, content)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    frames = parse_sse(response.text)
    names = [name for name, _ in frames]
    assert names[0] == "meta"
    assert names[-1] == "done"
    assert "message_saved" in names
    assert set(names) == {"meta", "text_delta", "message_saved", "done"}

    # meta：{task_id, seq, started_at}，首帧
    meta = frames[0][1]
    assert set(meta) == {"task_id", "seq", "started_at"}
    assert meta["task_id"] == task_id
    assert meta["seq"] == 1

    # text_delta：{delta}，拼接还原完整回复（EchoEngine 原样回显）
    deltas = [payload["delta"] for name, payload in frames if name == "text_delta"]
    for name, payload in frames:
        if name == "text_delta":
            assert set(payload) == {"delta"}
    assert len(deltas) >= 2, "EchoEngine 应分多帧吐出 text_delta 以验证流式"
    assert "".join(deltas) == content

    # message_saved：{message_id, content}
    saved = dict(frames)["message_saved"]
    assert set(saved) == {"message_id", "content"}
    assert saved["content"] == content
    assert saved["message_id"] > 0

    # done：{finish_reason, usage:{prompt_tokens, completion_tokens}}
    done = frames[-1][1]
    assert set(done) == {"finish_reason", "usage"}
    assert done["finish_reason"] == "stop"
    assert set(done["usage"]) == {"prompt_tokens", "completion_tokens"}


def test_send_message_persists_history_and_transitions_to_running(client):
    token, _, expert_id, _ = make_published_expert(client)
    task_id = create_task(client, token, expert_id).json()["data"]["task_id"]
    content = "第一轮消息"
    assert send_message(client, token, task_id, content).status_code == 200

    detail = client.get(f"/api/tasks/{task_id}", headers=auth_header(token))
    data = detail.json()["data"]
    assert data["status"] == "running"
    roles = [message["role"] for message in data["messages"]]
    assert roles == ["user", "assistant"]
    assert data["messages"][0]["content"] == content
    # EchoEngine：assistant 完整回复原样回显用户消息
    assert data["messages"][1]["content"] == content
    for message in data["messages"]:
        assert set(message) == {"id", "role", "content", "created_at"}


def test_send_message_second_round_increments_seq(client):
    token, _, expert_id, _ = make_published_expert(client)
    task_id = create_task(client, token, expert_id).json()["data"]["task_id"]
    assert send_message(client, token, task_id, "第一轮").status_code == 200

    response = send_message(client, token, task_id, "第二轮")
    assert response.status_code == 200
    frames = parse_sse(response.text)
    assert frames[0][1]["seq"] == 2

    detail = client.get(f"/api/tasks/{task_id}", headers=auth_header(token))
    assert [m["role"] for m in detail.json()["data"]["messages"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]


def test_send_message_failed_retry_returns_to_running(client, test_db):
    token, user_id, expert_id, _ = make_published_expert(client)
    task_id = seed_task(test_db, user_id, expert_id, status="failed")
    response = send_message(client, token, task_id, "重试一次")
    assert response.status_code == 200
    detail = client.get(f"/api/tasks/{task_id}", headers=auth_header(token))
    assert detail.json()["data"]["status"] == "running"


def test_send_message_created_task_without_workspace_override(client):
    """未覆盖 workspace 依赖时（默认 ./workspaces 根），创建任务仍可用根目录。"""
    token, _, expert_id, _ = make_published_expert(client)
    response = create_task(client, token, expert_id)
    assert response.status_code == 201


# ---------------------------------------------------------------------------
# 文件上传（POST/GET /api/tasks/{id}/files）
# ---------------------------------------------------------------------------


def test_upload_files_requires_authentication(client, file_root):
    assert client.post("/api/tasks/1/files").status_code == 401


def test_upload_files_happy_path(client, file_root):
    token, _, expert_id, _ = make_published_expert(client)
    task_id = create_task(client, token, expert_id).json()["data"]["task_id"]
    payload = "周报素材：本周完成三件事。"
    response = client.post(
        f"/api/tasks/{task_id}/files",
        files=[("files", ("report.txt", payload.encode("utf-8"), "text/plain"))],
        headers=auth_header(token),
    )
    assert response.status_code == 201
    files = response.json()["data"]["files"]
    assert len(files) == 1
    item = files[0]
    assert set(item) == {
        "id",
        "original_name",
        "agent_path",
        "size_bytes",
        "mime_type",
        "sha256",
        "created_at",
    }
    assert item["original_name"] == "report.txt"
    assert item["size_bytes"] == len(payload.encode("utf-8"))
    assert item["mime_type"] == "text/plain"
    assert item["sha256"] == hashlib.sha256(payload.encode("utf-8")).hexdigest()
    assert item["agent_path"].startswith("/task-files/")

    # 内容落到 HOST_DATA_ROOT/task-files/task-{id}/
    stored_dirs = list(file_root.glob(f"task-{task_id}/*"))
    assert len(stored_dirs) == 1
    assert stored_dirs[0].read_bytes() == payload.encode("utf-8")


def test_upload_files_multiple_and_list(client, file_root):
    token, _, expert_id, _ = make_published_expert(client)
    task_id = create_task(client, token, expert_id).json()["data"]["task_id"]
    response = client.post(
        f"/api/tasks/{task_id}/files",
        files=[
            ("files", ("a.txt", b"AAA", "text/plain")),
            ("files", ("b.txt", b"BBB", "text/plain")),
        ],
        headers=auth_header(token),
    )
    assert response.status_code == 201
    names = {item["original_name"] for item in response.json()["data"]["files"]}
    assert names == {"a.txt", "b.txt"}

    listing = client.get(f"/api/tasks/{task_id}/files", headers=auth_header(token))
    assert listing.status_code == 200
    data = listing.json()["data"]
    assert len(data) == 2
    assert all(item["agent_path"].startswith("/task-files/") for item in data)

    # 详情页 files 与列表一致
    detail = client.get(f"/api/tasks/{task_id}", headers=auth_header(token))
    assert len(detail.json()["data"]["files"]) == 2


def test_upload_files_sanitizes_mime_type(client, file_root):
    """MIME 仅用于展示：非法/超长的客户端声明一律存空。"""
    token, _, expert_id, _ = make_published_expert(client)
    task_id = create_task(client, token, expert_id).json()["data"]["task_id"]
    response = client.post(
        f"/api/tasks/{task_id}/files",
        files=[
            ("files", ("ok.txt", b"a", "text/plain")),
            ("files", ("evil.txt", b"b", "x" * 150)),
            ("files", ("space.txt", b"c", "not a mime")),
        ],
        headers=auth_header(token),
    )
    assert response.status_code == 201
    items = {item["original_name"]: item for item in response.json()["data"]["files"]}
    assert items["ok.txt"]["mime_type"] == "text/plain"
    assert items["evil.txt"]["mime_type"] is None
    assert items["space.txt"]["mime_type"] is None


def test_upload_files_rejects_illegal_names(client, file_root):
    token, _, expert_id, _ = make_published_expert(client)
    task_id = create_task(client, token, expert_id).json()["data"]["task_id"]
    for bad in ("a/b.txt", "a\\b.txt", "..", "x" * 256):
        response = client.post(
            f"/api/tasks/{task_id}/files",
            files=[("files", (bad, b"x", "text/plain"))],
            headers=auth_header(token),
        )
        assert response.status_code == 400, f"filename={bad!r}"


def test_filename_rule_unit():
    """控制字符等 httpx 无法走 multipart 的非法名，直接校验 FileService 规则。"""
    service = FileService(Path("."))
    for bad in ("a\x01.txt", "a/b", "a\\b", "..", ".", "", "x" * 256, "con.txt\x7f"):
        with pytest.raises(FilenameInvalidError):
            service.validate_original_name(bad)
    assert service.validate_original_name("周报素材.txt") == "周报素材.txt"


def test_upload_files_409_after_first_message(client, file_root):
    token, _, expert_id, _ = make_published_expert(client)
    task_id = create_task(client, token, expert_id).json()["data"]["task_id"]
    assert send_message(client, token, task_id, "开始吧").status_code == 200
    response = client.post(
        f"/api/tasks/{task_id}/files",
        files=[("files", ("late.txt", b"x", "text/plain"))],
        headers=auth_header(token),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "TASK_ALREADY_STARTED"


def test_upload_files_403_not_owner(client, file_root):
    token_a, _, expert_id, _ = make_published_expert(client, username="file-a")
    task_id = create_task(client, token_a, expert_id).json()["data"]["task_id"]
    registered = client.post(
        "/api/auth/register",
        json={"username": "file-b", "email": "file-b@example.com", "password": "secret123"},
    ).json()["data"]
    response = client.post(
        f"/api/tasks/{task_id}/files",
        files=[("files", ("x.txt", b"x", "text/plain"))],
        headers=auth_header(registered["token"]),
    )
    assert response.status_code == 403


def test_upload_files_404_missing_task(client, file_root):
    token, _, _, _ = make_published_expert(client)
    response = client.post(
        "/api/tasks/99999/files",
        files=[("files", ("x.txt", b"x", "text/plain"))],
        headers=auth_header(token),
    )
    assert response.status_code == 404


def test_upload_files_quota_limits(client, tmp_path):
    """单文件 / 单次数量 / 任务累计配额（依赖注入小限额便于测试）。"""
    root = tmp_path / "quota-task-files"
    app.dependency_overrides[get_file_service] = lambda: FileService(
        root, max_single_bytes=1000, max_files_per_request=2, max_task_bytes=1500
    )
    try:
        token, _, expert_id, _ = make_published_expert(client)
        # 单文件超限
        task_id = create_task(client, token, expert_id).json()["data"]["task_id"]
        too_big = client.post(
            f"/api/tasks/{task_id}/files",
            files=[("files", ("big.bin", b"x" * 1001, "application/octet-stream"))],
            headers=auth_header(token),
        )
        assert too_big.status_code == 413
        assert too_big.json()["error"]["code"] == "FILE_TOO_LARGE"

        # 单次数量超限
        three = client.post(
            f"/api/tasks/{task_id}/files",
            files=[
                ("files", ("a.txt", b"a", "text/plain")),
                ("files", ("b.txt", b"b", "text/plain")),
                ("files", ("c.txt", b"c", "text/plain")),
            ],
            headers=auth_header(token),
        )
        assert three.status_code == 413
        assert three.json()["error"]["code"] == "FILE_COUNT_EXCEEDED"

        # 任务累计配额：先传 1000，再传 600 → 413
        first = client.post(
            f"/api/tasks/{task_id}/files",
            files=[("files", ("first.bin", b"x" * 1000, "application/octet-stream"))],
            headers=auth_header(token),
        )
        assert first.status_code == 201
        second = client.post(
            f"/api/tasks/{task_id}/files",
            files=[("files", ("second.bin", b"y" * 600, "application/octet-stream"))],
            headers=auth_header(token),
        )
        assert second.status_code == 413
        assert second.json()["error"]["code"] == "FILE_QUOTA_EXCEEDED"
    finally:
        app.dependency_overrides.pop(get_file_service, None)


def test_upload_files_batch_failure_compensates(client, tmp_path):
    """批次内任一文件违规：整批 413，不落任何文件、不留孤儿。"""
    root = tmp_path / "compensation-task-files"
    app.dependency_overrides[get_file_service] = lambda: FileService(
        root, max_single_bytes=1000, max_files_per_request=5, max_task_bytes=1500
    )
    try:
        token, _, expert_id, _ = make_published_expert(client)
        task_id = create_task(client, token, expert_id).json()["data"]["task_id"]
        response = client.post(
            f"/api/tasks/{task_id}/files",
            files=[
                ("files", ("ok.bin", b"x" * 800, "application/octet-stream")),
                ("files", ("bad.bin", b"x" * 900, "application/octet-stream")),
            ],
            headers=auth_header(token),
        )
        assert response.status_code == 413
        listing = client.get(f"/api/tasks/{task_id}/files", headers=auth_header(token))
        assert listing.json()["data"] == []
        assert not (root / f"task-{task_id}").exists()
    finally:
        app.dependency_overrides.pop(get_file_service, None)


def test_sweep_stale_storage_cleans_orphans(client, test_db, file_root):
    """启动巡检（§6.6）：以 TaskFile 为事实源清理孤儿文件与超时暂存批次。"""
    token, _, expert_id, _ = make_published_expert(client)
    task_id = create_task(client, token, expert_id).json()["data"]["task_id"]
    uploaded = client.post(
        f"/api/tasks/{task_id}/files",
        files=[("files", ("known.txt", b"k", "text/plain"))],
        headers=auth_header(token),
    )
    assert uploaded.status_code == 201

    task_dir = file_root / f"task-{task_id}"
    (task_dir / "orphan.bin").write_bytes(b"o")
    old_batch = file_root / ".staging" / "stale-batch"
    old_batch.mkdir(parents=True)
    (old_batch / "f.bin").write_bytes(b"x")
    stale_timestamp = time.time() - 7200
    os.utime(old_batch, (stale_timestamp, stale_timestamp))

    stats = asyncio.run(sweep_stale_storage(test_db.session_factory, file_root))
    assert stats == {"staging_batches": 1, "orphan_files": 1}
    assert not old_batch.exists()
    assert not (task_dir / "orphan.bin").exists()
    # 有 TaskFile 元数据的文件保留，任务目录不删
    assert len(list(task_dir.iterdir())) == 1


def test_send_message_429_when_round_active(client, test_db, monkeypatch):
    """轮锁（§6.6/§7.2.1）：当前一轮未结束时再次发送返回 429 + Retry-After。"""
    import concurrent.futures
    import threading

    from backend.engine.echo import EngineEvent

    entered = threading.Event()
    release = threading.Event()

    class SlowEngine:
        async def stream(self, content):
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.02)
            yield EngineEvent("text_delta", {"delta": content})
            yield EngineEvent(
                "final",
                {"content": content, "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
            )

    monkeypatch.setattr("backend.api.tasks.EchoEngine", SlowEngine)
    token, _, expert_id, _ = make_published_expert(client)
    task_id = create_task(client, token, expert_id).json()["data"]["task_id"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(send_message, client, token, task_id, "第一轮")
        assert entered.wait(timeout=10), "第一轮未进入引擎"
        second = pool.submit(send_message, client, token, task_id, "并发第二轮")
        second_response = second.result(timeout=10)
        release.set()
        first_response = first.result(timeout=10)

    assert first_response.status_code == 200
    assert second_response.status_code == 429
    assert second_response.json()["error"]["code"] == "TASK_ROUND_BUSY"
    assert second_response.headers.get("retry-after") == "5"

    detail = client.get(f"/api/tasks/{task_id}", headers=auth_header(token))
    assert [m["role"] for m in detail.json()["data"]["messages"]] == ["user", "assistant"]


def test_upload_size_guard_rejects_before_multipart():
    """体量守卫中间件：Content-Length 超限直接 413，不进入后续 multipart 解析。"""

    async def run():
        from backend.middleware.upload_guard import UploadSizeGuardMiddleware

        reached_inner = {"value": False}

        async def inner_app(scope, receive, send):
            reached_inner["value"] = True

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            pass

        wrapped = UploadSizeGuardMiddleware(inner_app, max_bytes=100)
        for path in ("/api/tasks/1/files", "/api/tasks/999/files"):
            messages = []

            async def collect(message, _messages=messages):
                _messages.append(message)

            scope = {
                "type": "http",
                "method": "POST",
                "path": path,
                "headers": [(b"content-length", b"1000")],
            }
            await wrapped(scope, receive, collect)
            start = [m for m in messages if m["type"] == "http.response.start"]
            assert start[0]["status"] == 413
        assert reached_inner["value"] is False

        # 非上传路径不拦截
        messages = []

        async def collect2(message):
            messages.append(message)

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/tasks",
            "headers": [(b"content-length", b"999999")],
        }
        await wrapped(scope, receive, collect2)
        assert reached_inner["value"] is True

    asyncio.run(run())
