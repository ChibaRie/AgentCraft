"""admin 审核面端到端测试（Phase 7 T4；Sup §6:127-128）。

- HTTP 层经 httpx.AsyncClient(ASGITransport) 驱动真实 app（T2/T3a 同款 PG-httpx
  形态；admin_client 直驱全局 app）；
- 队列（GET /api/admin/reviews）：字段钉死（id/target_type/revision_no/owner_id/
  content_json 全量/content_sha256/auto_check/created_at）；auto_check 为读取时对
  content_json 重算 run_auto_check（纯函数，不持久化）；§6:128 审核队列豁免读审计；
- approve/reject（POST /api/admin/reviews/{revision_id}/approve|reject，body
  {target_type, reason}）：薄壳透传冻结服务语义——404 统一形态、409 REVIEW_PENDING、
  400 VALIDATION_ERROR（非 UUID 路径参数/词表外 target_type）；幂等三段壳
  （reason 原样参与 request_hash，同 key 异 reason → 409）；
- 种子/复核一律 superuser（pg.engine）；reviewer=真实 admin users 行
  （ContentReview.reviewer_id / AuditLog.actor_id 有 FK users）。
"""

import uuid as _uuid
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import text

from backend.main import app
from backend.v2.content_autocheck import run_auto_check
from backend.v2.content_hash import content_sha256
from backend.v2.runtime import owner_session
from backend.v2.session_service import COOKIE_NAME, create_session
from tests import v2_admin_helpers as _vah
from tests.v2_admin_helpers import admin_client
from tests.v2_content_helpers import EXPERT_CONTENT, SKILL_CONTENT, seed_entity_with_revision
from tests.v2_provider_helpers import seed_active_user

# admin_env 夹具发现形态：pytest 经模块命名空间发现夹具，「import + 参数同名」
# 触发 ruff F811——以赋值别名引入（T1 钉死形态，见 test_v2_admin_invitations.py:34）。
admin_env = _vah.admin_env

_UA = "AgentCraft-AdminReviewsTest/1.0"
_REVIEWS = "/api/admin/reviews"


# ---------- 种子与复核助手（superuser 绕 RLS/授权）----------


async def _set_created_at(pg, kind: str, revision_id: str, ts: datetime) -> None:
    """superuser 覆写 revision created_at（分页/排序确定性钉）。"""
    table = f"{kind[:-1]}_revisions"
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(f"UPDATE {table} SET created_at = :ts WHERE id = CAST(:r AS uuid)"),
            {"ts": ts, "r": revision_id},
        )


async def _seed_pending_revision(
    pg,
    kind: str,
    *,
    content: dict | None = None,
    revision_no: int = 1,
    entity_status: str = "draft",
    created_at: datetime | None = None,
) -> tuple[str, str, str]:
    """作者 + pending_review revision，返回 (author_id, entity_id, revision_id)。"""
    tag = _uuid.uuid4().hex[:6]
    author = await seed_active_user(pg, f"author-{kind}-{revision_no}-{tag}@x.com")
    payload = (
        content
        if content is not None
        else (dict(EXPERT_CONTENT) if kind == "experts" else dict(SKILL_CONTENT))
    )
    entity_id, revision_id = await seed_entity_with_revision(
        pg,
        author,
        kind,
        entity_status=entity_status,
        revision_status="pending_review",
        revision_no=revision_no,
        content_json=payload,
        content_sha256=content_sha256(payload),
    )
    if created_at is not None:
        await _set_created_at(pg, kind, revision_id, created_at)
    return author, entity_id, revision_id


async def _one(pg, sql: str, params: dict | None = None) -> dict:
    async with pg.engine.connect() as conn:
        return dict((await conn.execute(text(sql), params or {})).mappings().one())


async def _count(pg, table: str, where: str = "true", params: dict | None = None) -> int:
    async with pg.engine.connect() as conn:
        return (
            await conn.execute(text(f"SELECT count(*) FROM {table} WHERE {where}"), params or {})
        ).scalar_one()


async def _queue_item(pg, kind: str, revision_id: str) -> dict:
    table = f"{kind[:-1]}_revisions"
    return await _one(
        pg,
        f"SELECT status, content_json, content_sha256 FROM {table} WHERE id = CAST(:r AS uuid)",
        {"r": revision_id},
    )


async def _user_client(pg, rt, *, email: str) -> httpx.AsyncClient:
    """role='user'（无 TOTP）+ mfa_verified 会话客户端——若 admin 门②③先行会误报
    ADMIN_MFA_REQUIRED，403 FORBIDDEN 即门序① role 门先行的证据（T2 同款）。"""
    uid = str(_uuid.uuid4())
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash, role, status, mfa_secret_enc) "
                "VALUES (:i, :e, 'h', 'user', 'active', NULL)"
            ),
            {"i": uid, "e": email},
        )
    async with owner_session(rt, uid) as db:
        token, csrf = await create_session(
            db, user_id=_uuid.UUID(uid), device_label=_UA, mfa_verified=True
        )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("10.8.0.9", 51001)),
        base_url="http://testserver",
        headers={"User-Agent": _UA, "X-CSRF-Token": csrf},
        cookies={COOKIE_NAME: token},
    )


async def _rewind_mfa_verified(pg, user_id: str, *, hours: int) -> None:
    """superuser 回拨会话 mfa_verified_at（sessions owner-RLS，superuser 绕过）。"""
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE sessions SET mfa_verified_at = now() - make_interval(secs => :s) "
                "WHERE user_id = CAST(:u AS uuid)"
            ),
            {"s": hours * 3600, "u": user_id},
        )


async def _approve(
    client: httpx.AsyncClient,
    revision_id: str,
    *,
    target_type: str = "expert_revision",
    reason: str = "质量合格",
    key: str = "k-approve-1",
    body: dict | None = None,
) -> httpx.Response:
    payload = body if body is not None else {"target_type": target_type, "reason": reason}
    return await client.post(
        f"{_REVIEWS}/{revision_id}/approve", json=payload, headers={"Idempotency-Key": key}
    )


async def _reject(
    client: httpx.AsyncClient,
    revision_id: str,
    *,
    target_type: str = "skill_revision",
    reason: str = "内容不实",
    key: str = "k-reject-1",
) -> httpx.Response:
    return await client.post(
        f"{_REVIEWS}/{revision_id}/reject",
        json={"target_type": target_type, "reason": reason},
        headers={"Idempotency-Key": key},
    )


# ---------- 队列（GET /api/admin/reviews）----------


async def test_list_reviews_queue_shape_full_content_and_autocheck(pg, admin_env):
    """队列形状钉死：信封 {items,total,page,size}；item 八字段精确等值；content_json
    全量；auto_check == 读取时对 content_json 重算 run_auto_check（expert/skill
    双形态分派）；expert+skill 双域 pending 汇入。"""
    e_content = dict(EXPERT_CONTENT)
    s_content = dict(SKILL_CONTENT)
    e_author, _, expert_rev = await _seed_pending_revision(
        pg, "experts", content=e_content, created_at=datetime.now(timezone.utc)
    )
    _, _, skill_rev = await _seed_pending_revision(
        pg, "skills", content=s_content, created_at=datetime.now(timezone.utc)
    )
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="queue@example.com")
    try:
        resp = await client.get(_REVIEWS)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert set(data) == {"items", "total", "page", "size"}
        assert data["total"] == 2 and data["page"] == 1 and data["size"] == 20
        by_type = {it["target_type"]: it for it in data["items"]}
        assert set(by_type) == {"expert_revision", "skill_revision"}

        item = by_type["expert_revision"]
        assert set(item) == {
            "id",
            "target_type",
            "revision_no",
            "owner_id",
            "content_json",
            "content_sha256",
            "auto_check",
            "created_at",
        }
        assert item["id"] == expert_rev
        assert item["revision_no"] == 1
        assert item["owner_id"] == str(e_author)  # 种子实返 UUID 对象（D26 同款归一）
        assert item["content_json"] == e_content  # 全量（§6:128 审核队列豁免读审计）
        assert item["content_sha256"] == content_sha256(e_content)
        assert item["auto_check"] == run_auto_check("expert_revision", e_content)
        assert item["created_at"]  # isoformat 字符串

        skill_item = by_type["skill_revision"]
        assert skill_item["id"] == skill_rev
        assert skill_item["content_json"] == s_content
        assert skill_item["auto_check"] == run_auto_check("skill_revision", s_content)
        assert skill_item["auto_check"]["valid"] is True  # 合规种子内容零问题
    finally:
        await client.aclose()


async def test_list_reviews_autocheck_recomputed_from_live_content(pg, admin_env):
    """auto_check 读取时重算（结果不持久化）：superuser 篡改 content_json（保留旧
    hash——队列不做哈希一致性校验）后，重算结果随新内容变化（persona 注入 API Key
    形态 → ERROR 级命中 → valid=False）；两次读取结果一致（纯函数确定性）。"""
    content = dict(EXPERT_CONTENT, persona="严谨、注重取舍的资深架构师。")
    _, _, revision_id = await _seed_pending_revision(pg, "experts", content=content)
    tampered = dict(EXPERT_CONTENT, persona="内部凭据 sk-abcdefghijklmnopqrst 勿外传。")
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE expert_revisions SET content_json = CAST(:c AS jsonb) "
                "WHERE id = CAST(:r AS uuid)"
            ),
            {"c": _dumps(tampered), "r": revision_id},
        )
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="recheck@example.com")
    try:
        first = await client.get(_REVIEWS)
        assert first.status_code == 200
        item = first.json()["data"]["items"][0]
        assert item["id"] == revision_id
        assert item["content_json"] == tampered  # 读到的是行上最新内容
        assert item["auto_check"]["valid"] is False
        assert any(i["rule"] == "api_key" for i in item["auto_check"]["issues"])
        again = await client.get(_REVIEWS)
        assert again.json()["data"]["items"][0]["auto_check"] == item["auto_check"]
    finally:
        await client.aclose()


async def test_list_reviews_filter_pagination_ordering_and_invalid_params(pg, admin_env):
    """created_at DESC + id 稳定序合并分页（双域汇流）；target_type 过滤；词表外
    filter 与非法分页 400 VALIDATION_ERROR。"""
    base = datetime.now(timezone.utc) - timedelta(hours=3)
    seeds = {}
    for i, (kind, offset) in enumerate(
        [("experts", 0), ("experts", 1), ("experts", 2), ("skills", 3), ("skills", 4)]
    ):
        _, _, rev = await _seed_pending_revision(
            pg, kind, revision_no=i + 1, created_at=base + timedelta(hours=offset)
        )
        seeds[rev] = kind
    # created_at 递增序：e1 < e2 < e3 < s1 < s2 → DESC 期望 [s2, s1, e3, e2, e1]
    ids = list(seeds)
    e1, e2, e3 = ids[0], ids[1], ids[2]
    s1, s2 = ids[3], ids[4]

    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="pager@example.com")
    try:
        resp = await client.get(_REVIEWS)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 5
        assert [it["id"] for it in data["items"]] == [s2, s1, e3, e2, e1]
        assert [it["target_type"] for it in data["items"]] == [
            "skill_revision",
            "skill_revision",
            "expert_revision",
            "expert_revision",
            "expert_revision",
        ]

        filtered = (await client.get(_REVIEWS, params={"target_type": "expert_revision"})).json()[
            "data"
        ]
        assert filtered["total"] == 3
        assert [it["id"] for it in filtered["items"]] == [e3, e2, e1]
        skill_only = (await client.get(_REVIEWS, params={"target_type": "skill_revision"})).json()[
            "data"
        ]
        assert skill_only["total"] == 2 and [it["id"] for it in skill_only["items"]] == [s2, s1]

        paged = (await client.get(_REVIEWS, params={"page": 2, "size": 2})).json()["data"]
        assert paged["total"] == 5 and paged["page"] == 2 and paged["size"] == 2
        assert [it["id"] for it in paged["items"]] == [e3, e2]
        last_page = (await client.get(_REVIEWS, params={"page": 3, "size": 2})).json()["data"]
        assert [it["id"] for it in last_page["items"]] == [e1]

        for params in ({"target_type": "expert"}, {"page": 0}, {"size": 101}, {"size": 0}):
            bad = await client.get(_REVIEWS, params=params)
            assert bad.status_code == 400
            assert bad.json()["error"]["code"] == "VALIDATION_ERROR"
    finally:
        await client.aclose()


# ---------- approve（POST /api/admin/reviews/{revision_id}/approve）----------


async def test_approve_full_chain_row_values_and_audit(pg, admin_env):
    """approve 200 全链：响应 dict 形状（服务冻结返回）；行值真实变化（revision
    published / 实体 published+指针 / content_reviews approved / audit
    expert.approve_publish）；reviewer/actor=真实 admin。"""
    content = dict(EXPERT_CONTENT)
    _, entity_id, revision_id = await _seed_pending_revision(pg, "experts", content=content)
    client, _csrf, admin_id = await admin_client(pg, admin_env, email="approver@example.com")
    try:
        resp = await _approve(client, revision_id, reason="质量合格，准予发布")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert set(data) == {
            "entity_id",
            "entity_status",
            "published_revision_id",
            "previous_published_revision_id",
            "revision_no",
            "content_sha256",
        }
        assert data["entity_id"] == entity_id
        assert data["entity_status"] == "published"
        assert data["published_revision_id"] == revision_id
        assert data["previous_published_revision_id"] is None
        assert data["content_sha256"] == content_sha256(content)

        rev_row = await _queue_item(pg, "experts", revision_id)
        assert rev_row["status"] == "published"  # 行值真实变化（非仅不抛错）
        entity_row = await _one(
            pg,
            "SELECT status, published_revision_id FROM experts WHERE id = CAST(:e AS uuid)",
            {"e": entity_id},
        )
        assert entity_row["status"] == "published"
        assert str(entity_row["published_revision_id"]) == revision_id
        review = await _one(
            pg,
            "SELECT result, reviewer_id, content_sha256 FROM content_reviews "
            "WHERE target_revision_id = CAST(:r AS uuid)",
            {"r": revision_id},
        )
        assert review["result"] == "approved" and str(review["reviewer_id"]) == admin_id
        assert review["content_sha256"] == content_sha256(content)
        audit = await _one(
            pg,
            "SELECT action, actor_id, target_type, reason FROM audit_logs "
            "WHERE action = 'expert.approve_publish'",
        )
        assert str(audit["actor_id"]) == admin_id
        assert audit["target_type"] == "expert_revision"
        assert audit["reason"] == "质量合格，准予发布"
    finally:
        await client.aclose()


async def test_approve_skill_revision_via_http(pg, admin_env):
    """skill 域 approve（target_type=skill_revision 分派；skill 无 tools/skill_refs
    断言面）：实体翻转 published，audit skill.approve_publish。"""
    _, entity_id, revision_id = await _seed_pending_revision(pg, "skills")
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="skill-pub@example.com")
    try:
        resp = await _approve(
            client,
            revision_id,
            target_type="skill_revision",
            reason="技能内容合格",
            key="k-skill-1",
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["entity_id"] == entity_id and data["entity_status"] == "published"
        assert (await _queue_item(pg, "skills", revision_id))["status"] == "published"
        audit = await _one(
            pg, "SELECT action FROM audit_logs WHERE action = 'skill.approve_publish'"
        )
        assert audit["action"] == "skill.approve_publish"
    finally:
        await client.aclose()


async def test_approve_idempotent_replay_and_reason_conflict(pg, admin_env):
    """同 key 同载荷原样重放（零新副作用）；同 key 异 reason（reason 参与
    request_hash）→ 409 IDEMPOTENCY_CONFLICT。"""
    _, _, revision_id = await _seed_pending_revision(pg, "experts")
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="idem-rev@example.com")
    try:
        first = await _approve(client, revision_id, key="k-replay-1")
        assert first.status_code == 200
        replay = await _approve(client, revision_id, key="k-replay-1")
        assert replay.status_code == 200
        assert replay.json() == first.json()
        assert await _count(pg, "content_reviews") == 1  # 重放零新副作用
        assert await _count(pg, "audit_logs") == 1

        conflict = await _approve(client, revision_id, key="k-replay-1", reason="另一条理由")
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        assert await _count(pg, "content_reviews") == 1
        assert await _count(pg, "audit_logs") == 1
    finally:
        await client.aclose()


async def test_approve_non_pending_and_hash_drift_409_passthrough(pg, admin_env):
    """冻结服务 409 透传（壳层不重复断言）：draft revision → 409 REVIEW_PENDING；
    pending 但 content_json 与 hash 漂移（TOCTOU 模拟）→ 409 REVIEW_PENDING；
    两条路径行值零突变、审计零行。"""
    _, _, draft_rev = await _seed_pending_revision(pg, "experts", entity_status="draft")
    async with pg.engine.begin() as conn:  # 降级为 draft revision（未提审）
        await conn.execute(
            text("UPDATE expert_revisions SET status = 'draft' WHERE id = CAST(:r AS uuid)"),
            {"r": draft_rev},
        )
    _, _, drift_rev = await _seed_pending_revision(pg, "experts")
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE expert_revisions SET content_json = CAST(:c AS jsonb) "
                "WHERE id = CAST(:r AS uuid)"
            ),
            {"c": _dumps({"tampered": True}), "r": drift_rev},
        )
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="conflict@example.com")
    try:
        r1 = await _approve(client, draft_rev, key="k-draft")
        assert r1.status_code == 409
        assert r1.json()["error"]["code"] == "REVIEW_PENDING"
        r2 = await _approve(client, drift_rev, key="k-drift")
        assert r2.status_code == 409
        assert r2.json()["error"]["code"] == "REVIEW_PENDING"
        assert (await _queue_item(pg, "experts", draft_rev))["status"] == "draft"
        assert (await _queue_item(pg, "experts", drift_rev))["status"] == "pending_review"
        assert await _count(pg, "audit_logs") == 0
        assert await _count(pg, "content_reviews") == 0
    finally:
        await client.aclose()


async def test_approve_missing_revision_404_and_bad_uuid_400(pg, admin_env):
    """不存在 revision → 统一 404 NOT_FOUND（HTTPException.detail 透传）；非 UUID
    路径参数 → 400 VALIDATION_ERROR（服务 _parse_uuid 透传）。"""
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="missing@example.com")
    try:
        missing = await _approve(client, str(_uuid.uuid4()), key="k-miss")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "NOT_FOUND"
        bad = await _approve(client, "not-a-uuid", key="k-bad")
        assert bad.status_code == 400
        assert bad.json()["error"]["code"] == "VALIDATION_ERROR"
    finally:
        await client.aclose()


async def test_approve_target_type_out_of_vocabulary_400(pg, admin_env):
    """target_type 词表外 → 壳层 400 VALIDATION_ERROR（先于冻结服务，ValueError
    不外泄）；零副作用（revision 仍 pending_review、审计零行）。"""
    _, _, revision_id = await _seed_pending_revision(pg, "experts")
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="vocab@example.com")
    try:
        resp = await _approve(
            client, revision_id, body={"target_type": "expert", "reason": "词表外"}, key="k-vocab"
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
        resp2 = await _approve(
            client, revision_id, body={"target_type": "message", "reason": "词表外"}, key="k-vocab2"
        )
        assert resp2.status_code == 400
        assert (await _queue_item(pg, "experts", revision_id))["status"] == "pending_review"
        assert await _count(pg, "audit_logs") == 0
        assert await _count(pg, "content_reviews") == 0
    finally:
        await client.aclose()


# ---------- reject（POST /api/admin/reviews/{revision_id}/reject）----------


async def test_reject_full_chain_row_values_entity_untouched(pg, admin_env):
    """reject 200 全链（skill 域）：revision rejected、content_reviews rejected、
    实体与指针未被触碰（draft + 指针 NULL）、audit skill.reject。"""
    _, entity_id, revision_id = await _seed_pending_revision(pg, "skills")
    client, _csrf, admin_id = await admin_client(pg, admin_env, email="rejector@example.com")
    try:
        resp = await _reject(client, revision_id, reason="内容与描述不符")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert set(data) == {"revision_id", "status"}
        assert data["revision_id"] == revision_id and data["status"] == "rejected"

        assert (await _queue_item(pg, "skills", revision_id))["status"] == "rejected"
        entity_row = await _one(
            pg,
            "SELECT status, published_revision_id FROM skills WHERE id = CAST(:e AS uuid)",
            {"e": entity_id},
        )
        assert entity_row["status"] == "draft" and entity_row["published_revision_id"] is None
        review = await _one(
            pg,
            "SELECT result, target_type, reviewer_id FROM content_reviews "
            "WHERE target_revision_id = CAST(:r AS uuid)",
            {"r": revision_id},
        )
        assert review["result"] == "rejected" and review["target_type"] == "skill_revision"
        assert str(review["reviewer_id"]) == admin_id
        audit = await _one(
            pg,
            "SELECT action, actor_id, reason FROM audit_logs WHERE action = 'skill.reject'",
        )
        assert str(audit["actor_id"]) == admin_id and audit["reason"] == "内容与描述不符"
    finally:
        await client.aclose()


async def test_reject_vocabulary_and_reason_gates_400(pg, admin_env):
    """reject 壳层门负例：词表外 target_type 400 VALIDATION_ERROR；reason 缺失/
    空白 400 ADMIN_REASON_REQUIRED（壳层门先于幂等）；缺 Idempotency-Key 400
    VALIDATION_ERROR；全部零副作用。"""
    _, _, revision_id = await _seed_pending_revision(pg, "skills")
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="r-gates@example.com")
    try:
        # 合法词表值 × 异域 id（skill revision id 配 expert_revision）→ 服务内该域
        # 查无此行 → 统一 404 透传（词表校验只拦词表外值）
        cross = await _reject(client, revision_id, target_type="expert_revision", key="k-r-cross")
        assert cross.status_code == 404
        assert cross.json()["error"]["code"] == "NOT_FOUND"
        out = await _reject(client, revision_id, target_type="message", key="k-r-out")
        assert out.status_code == 400
        assert out.json()["error"]["code"] == "VALIDATION_ERROR"
        no_reason = await client.post(
            f"{_REVIEWS}/{revision_id}/reject",
            json={"target_type": "skill_revision"},
            headers={"Idempotency-Key": "k-r-noreason"},
        )
        assert no_reason.status_code == 400
        assert no_reason.json()["error"]["code"] == "ADMIN_REASON_REQUIRED"
        blank = await _reject(client, revision_id, reason="   ", key="k-r-blank")
        assert blank.status_code == 400
        assert blank.json()["error"]["code"] == "ADMIN_REASON_REQUIRED"
        no_key = await client.post(
            f"{_REVIEWS}/{revision_id}/reject",
            json={"target_type": "skill_revision", "reason": "ok"},
        )
        assert no_key.status_code == 400
        assert no_key.json()["error"]["code"] == "VALIDATION_ERROR"
        assert (await _queue_item(pg, "skills", revision_id))["status"] == "pending_review"
        assert await _count(pg, "audit_logs") == 0
        assert await _count(pg, "content_reviews") == 0
    finally:
        await client.aclose()


# ---------- 门与壳负例（三路由全挂三重门）----------


async def test_review_routes_role_gate_first_403_no_side_effects(pg, admin_env):
    """非 admin（无 TOTP + mfa_verified 会话齐全——403 只能来自①role 门）→ 三路由
    全 403 FORBIDDEN（读写全受门）；业务零副作用。"""
    _, _, revision_id = await _seed_pending_revision(pg, "experts")
    client = await _user_client(pg, admin_env, email="plain@example.com")
    try:
        get = await client.get(_REVIEWS)
        assert get.status_code == 403
        assert get.json()["error"]["code"] == "FORBIDDEN"
        approve = await _approve(client, revision_id)
        assert approve.status_code == 403
        assert approve.json()["error"]["code"] == "FORBIDDEN"
        reject = await _reject(client, revision_id, target_type="expert_revision")
        assert reject.status_code == 403
        assert reject.json()["error"]["code"] == "FORBIDDEN"
        assert (await _queue_item(pg, "experts", revision_id))["status"] == "pending_review"
        assert await _count(pg, "audit_logs") == 0
        assert await _count(pg, "content_reviews") == 0
    finally:
        await client.aclose()


async def test_review_routes_mfa_window_expired_403_all_routes(pg, admin_env):
    """12h 门过期（回拨 13h）→ 三路由全 403 ADMIN_MFA_REQUIRED（读写全受门，
    Sup:126），且门先于幂等（旧 key 不能绕过）。"""
    _, _, revision_id = await _seed_pending_revision(pg, "experts")
    client, _csrf, admin_id = await admin_client(pg, admin_env, email="stale-mfa@example.com")
    try:
        await _rewind_mfa_verified(pg, admin_id, hours=13)
        get = await client.get(_REVIEWS)
        assert get.status_code == 403
        assert get.json()["error"]["code"] == "ADMIN_MFA_REQUIRED"
        approve = await _approve(client, revision_id, key="k-expired-a")
        assert approve.status_code == 403
        assert approve.json()["error"]["code"] == "ADMIN_MFA_REQUIRED"
        reject = await _reject(
            client, revision_id, target_type="expert_revision", key="k-expired-r"
        )
        assert reject.status_code == 403
        assert reject.json()["error"]["code"] == "ADMIN_MFA_REQUIRED"
        assert (await _queue_item(pg, "experts", revision_id))["status"] == "pending_review"
    finally:
        await client.aclose()


async def test_approve_reason_gates_400_zero_side_effects(pg, admin_env):
    """approve reason 门：缺失/空白 → 400 ADMIN_REASON_REQUIRED（壳层门，先于幂等
    begin）；>2000 字符 → 400 VALIDATION_ERROR（壳层 ≤2000 门，服务层无长度门）；
    全部零副作用。"""
    _, _, revision_id = await _seed_pending_revision(pg, "experts")
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="a-gates@example.com")
    try:
        missing = await client.post(
            f"{_REVIEWS}/{revision_id}/approve",
            json={"target_type": "expert_revision"},
            headers={"Idempotency-Key": "k-a-noreason"},
        )
        assert missing.status_code == 400
        assert missing.json()["error"]["code"] == "ADMIN_REASON_REQUIRED"
        blank = await _approve(client, revision_id, reason="   ", key="k-a-blank")
        assert blank.status_code == 400
        assert blank.json()["error"]["code"] == "ADMIN_REASON_REQUIRED"
        too_long = await _approve(client, revision_id, reason="长" * 2001, key="k-a-long")
        assert too_long.status_code == 400
        assert too_long.json()["error"]["code"] == "VALIDATION_ERROR"
        assert (await _queue_item(pg, "experts", revision_id))["status"] == "pending_review"
        assert await _count(pg, "audit_logs") == 0
        assert await _count(pg, "content_reviews") == 0
    finally:
        await client.aclose()


def _dumps(obj: dict) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)
