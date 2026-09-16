"""任务域模型测试共享助手（Phase 9 T6 拆分自 test_v2_models_tasking.py）。

父行种子与 kwargs 构造器——被 test_v2_models_tasking（约束面）与
test_v2_models_tasking_messages（消息/事件/幂等面）共同消费。
"""

import uuid as _uuid
from datetime import datetime, timedelta, timezone

from backend.v2.models import (
    Expert,
    ExpertRevision,
    ProviderCatalog,
    Task,
    TaskMessage,
    User,
    UserProvider,
)

SHA_A = "a" * 64
EXPIRES_SOON = datetime.now(timezone.utc) + timedelta(hours=1)
CONTENT = {"system_prompt": "s", "model": "gpt-4o"}


async def _seed_task_parents(maker, email: str):
    """建 task 全部硬 FK 父行：user + expert + expert_revision + catalog +
    user_provider，返回构造 Task 所需的 id 字典。"""
    async with maker() as s:
        user = User(email=email, password_hash="h", role="user", status="active")
        s.add(user)
        await s.flush()
        expert = Expert(owner_id=user.id, status="published")
        s.add(expert)
        await s.flush()
        rev = ExpertRevision(
            expert_id=expert.id,
            owner_id=user.id,
            revision_no=1,
            content_json=CONTENT,
            content_sha256=SHA_A,
            status="published",
        )
        s.add(rev)
        catalog = ProviderCatalog(
            display_name="OpenAI",
            allowed_host="api.openai.com",
            models=["gpt-4o"],
            healthcheck_path="/v1/models",
        )
        s.add(catalog)
        await s.flush()
        provider = UserProvider(
            user_id=user.id,
            catalog_id=catalog.id,
            model_id="gpt-4o",
            key_ciphertext="ct",
            dek_wrapped="dek",
            key_last4="Ab1!",
        )
        s.add(provider)
        await s.commit()
        return {
            "owner_id": user.id,
            "expert_revision_id": rev.id,
            "provider_id": provider.id,
        }


def _task_kwargs(parents, **overrides):
    """Task 构造基线；负例经 overrides 覆写单一字段。"""
    base = dict(
        owner_id=parents["owner_id"],
        expert_revision_id=parents["expert_revision_id"],
        provider_id=parents["provider_id"],
        provider_catalog_id=_uuid.uuid4(),  # 快照列：裸 Uuid，无 FK
        provider_model_id="gpt-4o",
        provider_key_version=1,
    )
    base.update(overrides)
    return base


async def _seed_task(maker, email: str, **task_overrides):
    """建完整父链 + task，返回 (task_id, parents)。"""
    parents = await _seed_task_parents(maker, email)
    async with maker() as s:
        task = Task(**_task_kwargs(parents, **task_overrides))
        s.add(task)
        await s.commit()
        return task.id, parents


async def _seed_message(maker, task_id, owner_id, event_sequence=0):
    """建一条 user 消息，返回 message id（event_sequence 由调用方保证 per-task 唯一）。"""
    async with maker() as s:
        msg = TaskMessage(
            task_id=task_id,
            owner_id=owner_id,
            event_sequence=event_sequence,
            author="user",
            content="初始输入",
        )
        s.add(msg)
        await s.commit()
        return msg.id


async def _seed_task_with_message(maker, email: str):
    task_id, parents = await _seed_task(maker, email)
    msg_id = await _seed_message(maker, task_id, parents["owner_id"])
    return task_id, parents, msg_id


def _file_kwargs(task_id, owner_id, file_name="report.pdf", **overrides):
    """TaskFile 构造基线；负例经 overrides 覆写单一字段。"""
    base = dict(
        task_id=task_id,
        owner_id=owner_id,
        direction="input",
        file_name=file_name,
        storage_key=f"tasks/{task_id}/{_uuid.uuid4()}",
        sha256=_uuid.uuid4().hex * 2,  # 64 hex
        size_bytes=1024,
    )
    base.update(overrides)
    return base
