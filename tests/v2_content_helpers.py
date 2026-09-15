"""内容治理域测试共享助手（superuser 种子 + entitlement 授予）。

纪律同 v2_provider_helpers：owner-RLS 表种子一律走 pg.engine（superuser）；
user_entitlements 授予路径属 Phase 7 admin 面（0009 已补 admin INSERT/UPDATE/
DELETE policy 兑现 0003:63-64 预留；D17「不得补」为 Phase 4 期测试裁决，已由
Phase 7 T1 解除——改走 admin 面归 T3a，Phase 4 测试 superuser 直插保持）。
login/auth_client/provider_env 直接复用 v2_provider_helpers（provider_env 注入
的 PROVIDER_KEK 对治理域无害）。
"""

from sqlalchemy import text

from tests.v2_provider_helpers import auth_client, login, provider_env  # noqa: F401 re-export

EXPERT_CONTENT = {
    "name": "架构评审专家",
    "description": "对系统设计做结构化评审。",
    "category": "tech",
    "avatar_url": None,
    "persona": "严谨、注重取舍的资深架构师。",
    "methodology": "先约束后方案，再评审。",
    "task_examples": [],
    "skill_refs": [],
}

SKILL_CONTENT = {
    "name": "代码评审技能",
    "description": "对提交的代码做结构化评审并输出问题清单。",
    # 长文本字段须 ≥20 字（SkillContentPayload min_length=20；T7 走 HTTP 的用例直接消费本夹具）
    "use_case": "提交 Pull Request 之前对变更做自动化初审。",
    "role": "资深代码评审员",
    "goal": "发现代码中的缺陷与潜在风险并给出修改建议。",
    "steps": "1. 通读变更范围 2. 按清单逐项检查 3. 输出评审报告。",
    "input_requirements": None,
    "output_requirements": "输出结构化的问题清单，逐条标注严重级别。",
    "constraints": "不修改代码，仅输出评审意见，不执行任何命令。",
}


async def seed_entitlement(pg, user_id: str, entitlement: str = "expert_author") -> None:
    """superuser 授予 expert_author（granted_at 有 server_default）。"""
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO user_entitlements (id, user_id, entitlement) "
                "VALUES (gen_random_uuid(), :u, :e)"
            ),
            {"u": user_id, "e": entitlement},
        )


async def seed_entity_with_revision(
    pg,
    user_id: str,
    kind: str,  # "experts" | "skills"
    *,
    entity_status: str = "draft",
    revision_status: str = "draft",
    revision_no: int = 1,
    content_json: dict | None = None,
    content_sha256: str | None = None,
    with_pointer: bool = False,
    entity_id: str | None = None,
) -> tuple[str, str]:
    """superuser 造 entity + revision 链，返回 (entity_id, revision_id)。

    entity_id 传入时复用该实体（不新建）——用于同实体多 revision 场景，调用方
    必须保证 revision_no 不与该实体已有行冲突（UNIQUE(expert_id, revision_no)）。
    with_pointer=True 时同时置实体 published 指针与 published 状态（blueprint:
    v2_provider_helpers.seed_task_for_provider:124-147）。
    """
    assert kind in ("experts", "skills")
    fk = "expert_id" if kind == "experts" else "skill_id"
    content_json = content_json if content_json is not None else {"seed": True}
    content_sha256 = content_sha256 or ("a" * 64)
    async with pg.engine.begin() as conn:
        if entity_id is None:
            entity_id = (
                await conn.execute(
                    text(
                        f"INSERT INTO {kind} (id, owner_id, status) "
                        f"VALUES (gen_random_uuid(), :u, :s) RETURNING id"
                    ),
                    {"u": user_id, "s": entity_status},
                )
            ).scalar_one()
        revision_id = (
            await conn.execute(
                text(
                    f"INSERT INTO {kind[:-1]}_revisions (id, {fk}, owner_id, revision_no, "
                    f"content_json, content_sha256, status) VALUES (gen_random_uuid(), :e, "
                    f":u, :n, CAST(:c AS jsonb), :h, :s) RETURNING id"
                ),
                {
                    "e": entity_id,
                    "u": user_id,
                    "n": revision_no,
                    "c": _dumps(content_json),
                    "h": content_sha256,
                    "s": revision_status,
                },
            )
        ).scalar_one()
        if with_pointer:
            await conn.execute(
                text(
                    f"UPDATE {kind} SET published_revision_id = :r, status = 'published' "
                    f"WHERE id = :e"
                ),
                {"r": revision_id, "e": entity_id},
            )
    return str(entity_id), str(revision_id)


async def seed_revision_tools(pg, revision_id: str, tools: list[tuple[str, str]]) -> None:
    """superuser 写 revision_tools 行（tool_catalog 0002 种子含五工具 @version '1'）。"""
    async with pg.engine.begin() as conn:
        for tool_id, version in tools:
            await conn.execute(
                text(
                    "INSERT INTO revision_tools (id, expert_revision_id, tool_id, version) "
                    "VALUES (gen_random_uuid(), :r, :t, :v)"
                ),
                {"r": revision_id, "t": tool_id, "v": version},
            )


def _dumps(obj: dict) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)
