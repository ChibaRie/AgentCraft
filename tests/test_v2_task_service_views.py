"""任务服务：视图 / 配额 / 释放（Phase 6；Phase 9 T6 拆分）。

D14 视图形状与展示字段、列表分页、配额四维视图、release_task_holdings 幂等。
自 test_v2_task_service.py 逐字搬移；共享助手见 v2_task_service_helpers。
"""

import json
import uuid as _uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from backend.errors import AgentCraftError, ErrorCode
from backend.v2.content_hash import content_sha256
from backend.v2.task_service import (
    commit_input,
    delete_task,
    get_quota_view,
    get_task_view,
    list_tasks,
    release_task_holdings,
)
from tests import v2_task_service_helpers as _ts_helpers
from tests.v2_provider_helpers import seed_task_for_provider
from tests.v2_task_helpers import (
    owner_tx,
    seed_task_user,
)
from tests.v2_task_service_helpers import (
    _create_ok,
    _seed_uploading_with_files,
)

# 夹具发现形态：以赋值别名引入（admin_env 同款惯例）。
app_engine = _ts_helpers.app_engine
domain = _ts_helpers.domain

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
    "expert",
    "provider",
    # Phase 10 M4：用户 MCP 挂载快照回显键
    "mcp_servers",
}
_FAKE_CATALOG_ID = "00000000-0000-0000-0000-00000000000c"


async def test_get_task_view_d14_shape_and_excludes_lease(pg, app_engine, domain):
    """D14 视图键集钉死（排除 lease_owner/lease_epoch 红线）；round 摘要仅
    id/state/attempt。"""
    uid, _pid, _rid, _cid = domain
    tid, manifest = await _seed_uploading_with_files(pg, app_engine, domain)
    async with owner_tx(app_engine, uid) as db:
        await commit_input(db, owner_id=uid, task_id=tid, manifest=manifest)
        view = await get_task_view(db, owner_id=uid, task_id=tid)
    assert set(view.keys()) == _D14_VIEW_KEYS
    assert set(view["active_round"].keys()) == {"id", "state", "attempt"}
    assert view["initial_round"] == view["active_round"]
    assert view["counts"] == {"inputs": 2, "outputs": 0}
    assert view["created_at"].endswith("+00:00")
    assert "lease_owner" not in str(view) and "lease_epoch" not in str(view)


async def test_get_task_view_expert_and_provider_display_fields(pg, app_engine, domain):
    """Sup §10.3（Phase 8 T2）：视图键集增 expert{name,avatar_url}/provider
    {display_name,model} 展示字段。display_name 经 provider_catalog join（任务快照
    四键无 display_name），model 直取任务快照；契约键集约束：不加 skills 键
    （skills 经 discover 详情二次拉取，不进任务视图）。"""
    uid, pid, _rid, _cid = domain
    tid = await seed_task_for_provider(pg, uid, pid, status="uploading")
    content = {"name": "唤起专家", "avatar_url": "https://cdn.example.com/a.png"}
    async with pg.engine.begin() as conn:  # superuser 回填 content（冻结触发器仅拦 app 上下文）
        rev_id = (
            await conn.execute(
                text("SELECT expert_revision_id FROM tasks WHERE id = CAST(:t AS uuid)"),
                {"t": tid},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "UPDATE expert_revisions SET content_json = CAST(:c AS jsonb), "
                "content_sha256 = :h WHERE id = CAST(:r AS uuid)"
            ),
            {
                "c": json.dumps(content, ensure_ascii=False),
                "h": content_sha256(content),
                "r": str(rev_id),
            },
        )
    async with owner_tx(app_engine, uid) as db:
        view = await get_task_view(db, owner_id=uid, task_id=tid)
    assert view["expert"] == {"name": "唤起专家", "avatar_url": "https://cdn.example.com/a.png"}
    # 2026-09-17 去目录化：display_name = base_url 上游 host（不再经目录 join）；
    # seed_provider 缺省 base https://api.openai.com/v1 → 'api.openai.com'；快照 model 直取
    assert view["provider"] == {"display_name": "api.openai.com", "model": "m"}
    assert "skills" not in view


async def test_get_task_view_expert_null_after_takedown_and_republish(pg, app_engine, domain):
    """Sup §10.3 null 语义（契约审查 I4）：takedown（实体 status→draft、指针不清，
    §10.6 offline 同语义）后 expert=null 不抛；作者再发布新版（指针前移）后旧
    revision 脱离「实体当前 published 指针」→ 仍 null（历史任务不回溯旧版内容）。
    provider 展示不受内容治理影响。"""
    uid, pid, _rid, _cid = domain
    tid = await seed_task_for_provider(pg, uid, pid, status="uploading")
    async with pg.engine.begin() as conn:  # superuser：0006 guard 仅拦 app 上下文
        await conn.execute(
            text(
                "UPDATE experts SET status = 'draft' WHERE id = "
                "(SELECT expert_id FROM expert_revisions WHERE id = "
                "(SELECT expert_revision_id FROM tasks WHERE id = CAST(:t AS uuid)))"
            ),
            {"t": tid},
        )
    async with owner_tx(app_engine, uid) as db:
        view = await get_task_view(db, owner_id=uid, task_id=tid)
    assert view["expert"] is None
    assert view["provider"] is not None
    # 作者再发布新版：指针前移至 revision_no=2 → 旧 revision 仍不可见
    async with pg.engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT r.expert_id AS expert_id, r.owner_id AS owner_id "
                    "FROM expert_revisions r JOIN tasks t ON t.expert_revision_id = r.id "
                    "WHERE t.id = CAST(:t AS uuid)"
                ),
                {"t": tid},
            )
        ).one()
        new_rev = (
            await conn.execute(
                text(
                    "INSERT INTO expert_revisions (id, expert_id, owner_id, revision_no, "
                    "content_json, content_sha256, status) VALUES (gen_random_uuid(), "
                    "CAST(:e AS uuid), CAST(:o AS uuid), 2, CAST(:c AS jsonb), :h, 'published') "
                    "RETURNING id"
                ),
                {
                    "e": str(row.expert_id),
                    "o": str(row.owner_id),
                    "c": json.dumps({"name": "新版专家"}),
                    "h": "b" * 64,
                },
            )
        ).scalar_one()
        await conn.execute(
            text(
                "UPDATE experts SET status = 'published', published_revision_id = "
                "CAST(:r AS uuid) WHERE id = CAST(:e AS uuid)"
            ),
            {"r": str(new_rev), "e": str(row.expert_id)},
        )
    async with owner_tx(app_engine, uid) as db:
        after = await get_task_view(db, owner_id=uid, task_id=tid)
    assert after["expert"] is None
    assert after["provider"] == view["provider"]


async def test_get_task_view_other_owner_404(pg, app_engine, domain):
    """RLS 统一 404：他人任务与缺失任务同形（Sup §7）。"""
    uid, pid, _rid, _cid = domain
    other = await seed_task_user(pg, "t3-other@x.test")
    tid = await seed_task_for_provider(pg, uid, pid, status="uploading")
    with pytest.raises(AgentCraftError) as ei_other:
        async with owner_tx(app_engine, other) as db:
            await get_task_view(db, owner_id=other, task_id=tid)
    assert ei_other.value.code is ErrorCode.TASK_NOT_FOUND
    with pytest.raises(AgentCraftError) as ei_missing:
        async with owner_tx(app_engine, uid) as db:
            await get_task_view(db, owner_id=uid, task_id=str(_uuid.uuid4()))
    assert ei_missing.value.code is ErrorCode.TASK_NOT_FOUND


async def test_task_id_segment_discipline(pg, app_engine, domain):
    """入口纪律：task_id 路径段一律严格 UUID 解析——分隔符/`.`/`..` 等非法串
    400 VALIDATION_ERROR（TaskStorage 路径段永不接收外部裸串）。"""
    uid, _pid, _rid, _cid = domain
    for bad in ("..", "../etc", "a/b", "not-a-uuid"):
        with pytest.raises(HTTPException) as ei:
            async with owner_tx(app_engine, uid) as db:
                await get_task_view(db, owner_id=uid, task_id=bad)
        assert ei.value.status_code == 400
        assert ei.value.detail["code"] == "VALIDATION_ERROR"


async def test_list_tasks_pagination_excludes_deleted(pg, app_engine, domain):
    """分页列表（created_at DESC 稳定序 + total/page/size 信封）；deleted 不可见。"""
    uid, pid, rid, cid = domain
    async with owner_tx(app_engine, uid) as db:
        ids = [
            (await _create_ok(db, uid, pid, rid, cid, message=f"m{i}"))["task"]["id"]
            for i in range(3)
        ]
        page1 = await list_tasks(db, owner_id=uid, page=1, size=2)
        page2 = await list_tasks(db, owner_id=uid, page=2, size=2)
        await delete_task(db, owner_id=uid, task_id=ids[0])
        after_delete = await list_tasks(db, owner_id=uid, page=1, size=10)
    assert page1["total"] == 3 and page1["page"] == 1 and page1["size"] == 2
    assert len(page1["items"]) == 2
    assert page2["total"] == 3 and len(page2["items"]) == 1
    assert page1["items"][0]["id"] != page1["items"][1]["id"]
    assert after_delete["total"] == 2
    assert all(item["id"] != ids[0] for item in after_delete["items"])
    with pytest.raises(HTTPException) as ei:
        async with owner_tx(app_engine, uid) as db:
            await list_tasks(db, owner_id=uid, page=0, size=10)
    assert ei.value.status_code == 400


async def test_get_quota_view_four_dimensions(pg, app_engine, domain):
    """owner 四维用量与上限（daily/active/running/storage）。"""
    uid, pid, _rid, _cid = domain
    await seed_task_for_provider(pg, uid, pid, status="uploading")  # active_tasks=1
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO usage_daily (id, user_id, day, tasks_started) "
                "VALUES (gen_random_uuid(), :u, CURRENT_DATE, 2)"
            ),
            {"u": uid},
        )
        await conn.execute(
            text("UPDATE user_quota_usage SET retained_storage_bytes = 123 WHERE user_id = :u"),
            {"u": uid},
        )
    async with owner_tx(app_engine, uid) as db:
        out = await get_quota_view(db, owner_id=uid)
    assert out["usage"] == {
        "tasks_started_today": 2,
        "active_tasks": 1,
        "running_tasks": 0,
        "retained_storage_bytes": 123,
    }
    assert out["limits"] == {
        "max_daily_tasks": 5,
        "max_active_tasks": 3,
        "max_running_tasks": 1,
        "max_retained_storage_bytes": 1_073_741_824,
    }


# ---------------------------------------------------------------------------
# release_task_holdings：三本账对称释放原语
# ---------------------------------------------------------------------------


async def test_release_holdings_returning_gate_no_double_refund(pg, app_engine, domain):
    """RETURNING 闸门：零行即零退——二次调用不再递减计数、不再退存储（负值防御
    的前置保证）；deleted 分档全清（active+task_root）。"""
    uid, pid, _rid, _cid = domain
    tid = await seed_task_for_provider(pg, uid, pid, status="queued")
    async with pg.engine.begin() as conn:
        await conn.execute(text("UPDATE tasks SET status = 'deleted' WHERE id = :t"), {"t": tid})
        await conn.execute(
            text(
                "UPDATE task_reservations SET bytes = 400 WHERE task_id = :t AND kind = 'task_root'"
            ),
            {"t": tid},
        )
        await conn.execute(
            text("UPDATE user_quota_usage SET retained_storage_bytes = 400 WHERE user_id = :u"),
            {"u": uid},
        )
        await conn.execute(
            text("UPDATE platform_storage SET retained_storage_bytes = 400 WHERE singleton")
        )
    async with owner_tx(app_engine, uid) as db:
        await release_task_holdings(db, task_id=tid, owner_id=uid)
        await release_task_holdings(db, task_id=tid, owner_id=uid)  # 零行：零退
    async with pg.engine.connect() as conn:
        res = (
            await conn.execute(
                text("SELECT kind, state FROM task_reservations WHERE task_id = :t ORDER BY kind"),
                {"t": tid},
            )
        ).all()
        usage = (
            (
                await conn.execute(
                    text(
                        "SELECT active_tasks, retained_storage_bytes FROM user_quota_usage "
                        "WHERE user_id = :u"
                    ),
                    {"u": uid},
                )
            )
            .mappings()
            .one()
        )
        platform_bytes = (
            await conn.execute(
                text("SELECT retained_storage_bytes FROM platform_storage WHERE singleton")
            )
        ).scalar_one()
    assert dict(res) == {"active": "released", "task_root": "released"}
    assert usage["active_tasks"] == 0 and usage["retained_storage_bytes"] == 0
    assert platform_bytes == 0


async def test_release_holdings_non_terminal_keeps_active(pg, app_engine, domain):
    """非终态（settle→ready 调用形态）：只回收轮账；active/task_root 不受影响、
    active_tasks 不减。"""
    uid, pid, _rid, _cid = domain
    tid = await seed_task_for_provider(pg, uid, pid, status="ready")  # active+task_root held
    async with owner_tx(app_engine, uid) as db:
        await release_task_holdings(db, task_id=tid, owner_id=uid)
    async with pg.engine.connect() as conn:
        res = (
            await conn.execute(
                text("SELECT kind, state FROM task_reservations WHERE task_id = :t ORDER BY kind"),
                {"t": tid},
            )
        ).all()
        usage = (
            await conn.execute(
                text("SELECT active_tasks FROM user_quota_usage WHERE user_id = :u"), {"u": uid}
            )
        ).scalar_one()
    assert dict(res) == {"active": "held", "task_root": "held"}
    assert usage == 1


async def test_release_holdings_missing_task_silent(pg, app_engine):
    """任务行不存在（D18 物理删竞态）→ 静默返回（D16 弃权语义），不抛。"""
    uid = await seed_task_user(pg, "t3-silent@x.test")
    async with owner_tx(app_engine, uid) as db:
        await release_task_holdings(db, task_id=str(_uuid.uuid4()), owner_id=uid)
