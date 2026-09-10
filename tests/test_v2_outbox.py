"""邮件 outbox + 传输 + dispatcher 测试（Task 8）。

契约出处：task-8-brief + Supplement §9.3（mail-egress 生产闸门）+ Eng §2.2
（邮件 payload 红线：无 URL/header/正文/附件）。

- enqueue 属调用方 owner 事务（不提交）：回滚原子性与 owner-RLS 写均按真实 role 验证；
- dispatcher 走 admin role（0003 的 email_outbox_admin_update），认领与终态分属两个
  短事务，transport.send 恒在事务外；租约 5 分钟 + SKIP LOCKED + 过期重认领 = 崩溃恢复；
- 密钥注入：测试进程不配置真实 EMAIL_OUTBOX_ENCRYPTION_KEY（conftest 仅设
  ALLOW_INSECURE_SECRETS=true），经 monkeypatch.setenv 注入一次性 b64url 32B 材料
  ——env var 优先级高于 .env（pydantic-settings 语义），Settings() 每次现读，
  用例间互不污染（同 test_v2_rate_limit 模式）；
- 播种一律 superuser（pg.engine）；attempts 列无 server default，直插 SQL 必须显式给 0。
"""

import asyncio
import base64
import json
import logging
from contextlib import suppress

import pytest
from sqlalchemy import text

from backend.utils.crypto import EncryptionError, decrypt_text, encrypt_text, make_keyring
from backend.v2 import outbox as outbox_mod
from backend.v2.ids import uuid7
from backend.v2.mailer import ConsoleMailTransport, MailEgressTransport, transport_from_settings
from backend.v2.outbox import (
    BACKOFF_SECONDS,
    LEASE_MINUTES,
    MAX_ATTEMPTS,
    VALID_HOURS,
    _build_payload,
    dispatch_once,
    enqueue,
    outbox_loop,
)
from backend.v2.runtime import owner_session
from tests.conftest import PgDb
from tests.test_v2_runtime import make_v2_runtime

# 32 字节确定性密钥材料（b64url 43 字符）；仅测试注入，非任何真实密钥
_KEY_MATERIAL = base64.urlsafe_b64encode(bytes(range(1, 33))).decode()
_KID = "primary"


@pytest.fixture
def outbox_key(monkeypatch):
    """注入 EMAIL_OUTBOX_ENCRYPTION_KEY 环境变量（用例结束自动还原）。"""
    monkeypatch.setenv("EMAIL_OUTBOX_ENCRYPTION_KEY", _KEY_MATERIAL)


@pytest.fixture
async def v2_runtime(pg: PgDb):
    """app/admin 双 role 运行时（复用 test_v2_runtime 构建器；不挂 FastAPI override）。"""
    rt = make_v2_runtime(pg)
    yield rt
    rt.close()


# ---------- 假传输与播种/断言助手 ----------


class RecordingTransport:
    """记录成功投递的假传输。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def send(self, *, purpose: str, payload: dict) -> None:
        self.sent.append((purpose, payload))


class FlakyTransport:
    """恒抛异常的假传输（模拟 SMTP 故障）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def send(self, *, purpose: str, payload: dict) -> None:
        self.calls += 1
        raise RuntimeError("smtp down")


async def _seed_user(pg: PgDb, tag: str) -> str:
    uid = str(uuid7())
    async with pg.engine.begin() as seed:
        await seed.execute(
            text(
                "INSERT INTO users (id, email, password_hash, role, status) "
                "VALUES (:i, :e, 'h', 'user', 'active')"
            ),
            {"i": uid, "e": f"{tag}@outbox.test"},
        )
    return uid


async def _enqueue_one(
    rt,
    uid: str,
    *,
    purpose: str = "email_verify",
    recipient: str = "User@Example.COM",
    action_token: str = "tok-123",
    outbox_id=None,
):
    oid = outbox_id or uuid7()
    async with owner_session(rt, uid) as db:
        await enqueue(
            db,
            purpose=purpose,
            user_id=uid,
            recipient=recipient,
            action_token=action_token,
            outbox_id=oid,
        )
    return oid


async def _row(pg: PgDb, oid):
    async with pg.engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT id, user_id, purpose, payload_ciphertext, state, lease_owner, "
                "lease_expires_at, attempts, next_attempt_at FROM email_outbox WHERE id = :id"
            ),
            {"id": oid},
        )
        return result.mappings().one()


async def _exec(pg: PgDb, sql: str, **params) -> None:
    async with pg.engine.begin() as conn:
        await conn.execute(text(sql), params)


async def _seconds_until(pg: PgDb, oid, column: str) -> float | None:
    async with pg.engine.connect() as conn:
        value = (
            await conn.execute(
                text(
                    f"SELECT extract(epoch FROM ({column} - now())) "
                    "FROM email_outbox WHERE id = :id"
                ),
                {"id": oid},
            )
        ).scalar_one()
    return None if value is None else float(value)


def _decrypt_payload(row) -> dict:
    """以行 id 重建 AAD 解密 payload（密钥材料为测试注入值）。"""
    _, keyring = make_keyring(f"{_KID}:{_KEY_MATERIAL}")
    envelope = json.loads(row["payload_ciphertext"])
    return json.loads(
        decrypt_text(envelope, aad=f"agentcraft:email_outbox:{row['id']}:v1", keyring=keyring)
    )


async def _seed_leased_row(pg: PgDb, uid: str, *, expires_sql: str, attempts: int = 0):
    """superuser 直插带租约的 pending 行（合法信封；attempts 列无 server default 必须显式给）。"""
    oid = uuid7()
    _, keyring = make_keyring(f"{_KID}:{_KEY_MATERIAL}")
    payload = {
        "template_id": "email_verify",
        "recipient": "stale@outbox.test",
        "vars": {"action_token": "tok-stale", "valid_hours": 72},
    }
    envelope = encrypt_text(
        json.dumps(payload, ensure_ascii=False),
        aad=f"agentcraft:email_outbox:{oid}:v1",
        keyring=keyring,
        active_kid=_KID,
    )
    await _exec(
        pg,
        "INSERT INTO email_outbox (id, user_id, purpose, payload_ciphertext, state, "
        "lease_owner, lease_expires_at, attempts) "
        "VALUES (:id, :uid, 'email_verify', :ct, 'pending', 'outbox-dead', "
        f"{expires_sql}, :attempts)",
        id=oid,
        uid=uid,
        ct=json.dumps(envelope),
        attempts=attempts,
    )
    return oid


# ---------- 常量与 payload 白名单（纯函数，无 DB）----------


def test_constants_pin_brief_values():
    assert VALID_HOURS == {
        "invitation": 168,
        "email_verify": 72,
        "password_reset": 0.5,
        "deletion_cancel": 336,
    }
    assert (MAX_ATTEMPTS, LEASE_MINUTES) == (5, 5)
    assert BACKOFF_SECONDS == (60, 300, 900, 1800)


def test_build_payload_whitelist_exact_shape():
    payload = _build_payload("email_verify", "User@Example.COM", "tok-123")
    assert payload == {
        "template_id": "email_verify",
        "recipient": "User@example.com",  # 规范化：去空白 + 域名小写
        "vars": {"action_token": "tok-123", "valid_hours": 72},
    }


def test_build_payload_rejects_extra_vars_and_unknown_purpose():
    # 白名单外 vars 在构造处即拒绝（Eng §2.2 红线：无 URL/header/正文/附件）
    with pytest.raises(ValueError):
        _build_payload("email_verify", "a@b.test", "t", extra_vars={"url": "https://x"})
    with pytest.raises(ValueError):
        _build_payload("newsletter", "a@b.test", "t")  # 未登记 purpose


# ---------- enqueue：加密往返 / AAD 绑定 / 事务归属 ----------


async def test_encrypt_roundtrip_and_aad_binding(pg, v2_runtime, outbox_key):
    uid = await _seed_user(pg, "aad")
    oid = await _enqueue_one(v2_runtime, uid)
    row = await _row(pg, oid)
    assert json.loads(row["payload_ciphertext"])["alg"] == "A256GCM"
    assert _decrypt_payload(row)["vars"]["action_token"] == "tok-123"

    # AAD 绑定行 id：换一个 id 重建 AAD 必然解密失败（防跨行搬用密文）
    _, keyring = make_keyring(f"{_KID}:{_KEY_MATERIAL}")
    envelope = json.loads(row["payload_ciphertext"])
    with pytest.raises(EncryptionError):
        decrypt_text(envelope, aad=f"agentcraft:email_outbox:{uuid7()}:v1", keyring=keyring)


@pytest.mark.parametrize(
    "purpose", ["invitation", "email_verify", "password_reset", "deletion_cancel"]
)
async def test_enqueue_persists_pending_row_after_owner_commit(pg, v2_runtime, outbox_key, purpose):
    uid = await _seed_user(pg, f"enq-{purpose}")
    oid = await _enqueue_one(v2_runtime, uid, purpose=purpose)
    row = await _row(pg, oid)
    assert str(row["user_id"]) == uid
    assert row["purpose"] == purpose
    assert row["state"] == "pending"
    assert row["attempts"] == 0  # ORM default=0（列无 server default）
    assert row["lease_owner"] is None and row["lease_expires_at"] is None
    assert row["next_attempt_at"] is None
    payload = _decrypt_payload(row)
    assert payload == {
        "template_id": purpose,
        "recipient": "User@example.com",
        "vars": {"action_token": "tok-123", "valid_hours": VALID_HOURS[purpose]},
    }


async def test_enqueue_rolls_back_with_caller_transaction(pg, v2_runtime, outbox_key):
    uid = await _seed_user(pg, "rollback")
    oid = uuid7()
    with pytest.raises(RuntimeError):
        async with owner_session(v2_runtime, uid) as db:
            await enqueue(
                db,
                purpose="email_verify",
                user_id=uid,
                recipient="a@example.com",
                action_token="t",
                outbox_id=oid,
            )
            raise RuntimeError("caller tx failed")
    async with pg.engine.connect() as conn:
        cnt = (await conn.execute(text("SELECT count(*) FROM email_outbox"))).scalar_one()
    assert cnt == 0  # 事务性 outbox：调用方回滚 → outbox 行一并消失


async def test_enqueue_requires_configured_key(pg, v2_runtime):
    uid = await _seed_user(pg, "nokey")  # 不注入 outbox_key：V1-only 默认空串
    with pytest.raises(ValueError):
        await _enqueue_one(v2_runtime, uid)


# ---------- transport 层 ----------


async def test_mail_egress_transport_raises_not_implemented():
    with pytest.raises(NotImplementedError):
        await MailEgressTransport().send(purpose="email_verify", payload={})


def test_transport_selection_from_settings(monkeypatch):
    monkeypatch.setenv("MAIL_TRANSPORT", "mailegress")
    assert isinstance(transport_from_settings(), MailEgressTransport)
    monkeypatch.setenv("MAIL_TRANSPORT", "console")
    assert isinstance(transport_from_settings(), ConsoleMailTransport)
    monkeypatch.setenv("MAIL_TRANSPORT", "carrier-pigeon")
    with pytest.raises(ValueError):
        transport_from_settings()


# ---------- dispatcher：认领 / 终态 / 租约 / 退避 ----------


async def test_dispatch_pending_to_sent_with_masked_log(
    pg, v2_runtime, outbox_key, caplog, monkeypatch
):
    monkeypatch.setenv("ALLOW_INSECURE_SECRETS", "false")  # 生产语义：令牌必须掩码
    uid = await _seed_user(pg, "sent")
    oid = await _enqueue_one(v2_runtime, uid)
    transport = ConsoleMailTransport()

    with caplog.at_level(logging.INFO, logger="agentcraft.mail"):
        assert await dispatch_once(v2_runtime, transport) == 1
    row = await _row(pg, oid)
    assert row["state"] == "sent"
    assert row["lease_owner"] is None and row["lease_expires_at"] is None  # 终态清租约
    assert await dispatch_once(v2_runtime, transport) == 0  # 无剩余 pending

    # 日志红线：非 insecure 模式下令牌掩码；purpose/recipient/valid_hours 正常输出
    assert "action_token=****" in caplog.text
    assert "tok-123" not in caplog.text
    assert "purpose=email_verify" in caplog.text
    assert "User@example.com" in caplog.text
    assert "valid_hours=72" in caplog.text


async def test_console_transport_reveals_token_only_when_insecure_secrets(
    pg, v2_runtime, outbox_key, caplog, monkeypatch
):
    monkeypatch.setenv("ALLOW_INSECURE_SECRETS", "true")  # conftest 已设；显式钉死意图
    uid = await _seed_user(pg, "insecure")
    await _enqueue_one(v2_runtime, uid)
    with caplog.at_level(logging.INFO, logger="agentcraft.mail"):
        await dispatch_once(v2_runtime, ConsoleMailTransport())
    assert "action_token=tok-123" in caplog.text  # dev 逃生舱：明文令牌仅此模式下可见


async def test_dispatch_failure_backoff_then_delivery_failed(pg, v2_runtime, outbox_key, caplog):
    uid = await _seed_user(pg, "flaky")
    oid = await _enqueue_one(v2_runtime, uid)
    transport = FlakyTransport()
    caplog.set_level(logging.INFO, logger="agentcraft.mail")

    for attempt in range(1, MAX_ATTEMPTS):
        assert await dispatch_once(v2_runtime, transport) == 0
        row = await _row(pg, oid)
        assert row["state"] == "pending"
        assert row["attempts"] == attempt
        assert row["lease_owner"] is None and row["lease_expires_at"] is None
        backoff = BACKOFF_SECONDS[attempt - 1]
        remaining = await _seconds_until(pg, oid, "next_attempt_at")
        assert backoff - 5 <= remaining <= backoff  # 退避按 attempts 档位递增
        # 退避窗口内不可再次认领
        assert await dispatch_once(v2_runtime, transport) == 0
        await _exec(
            pg,
            "UPDATE email_outbox SET next_attempt_at = now() - interval '1 second' WHERE id = :id",
            id=oid,
        )

    # 第 5 次失败：delivery_failed + ERROR 日志（管理员告警语义，无 payload 内容）
    assert await dispatch_once(v2_runtime, transport) == 0
    row = await _row(pg, oid)
    assert row["state"] == "delivery_failed"
    assert row["attempts"] == MAX_ATTEMPTS
    assert row["lease_owner"] is None and row["lease_expires_at"] is None
    assert transport.calls == MAX_ATTEMPTS
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any("outbox delivery failed" in r.getMessage() for r in errors)


async def test_decrypt_failure_counts_as_attempt_failure(pg, v2_runtime, outbox_key):
    uid = await _seed_user(pg, "corrupt")
    oid = uuid7()
    await _exec(
        pg,
        "INSERT INTO email_outbox (id, user_id, purpose, payload_ciphertext, state, attempts) "
        "VALUES (:id, :uid, 'email_verify', :ct, 'pending', 0)",
        id=oid,
        uid=uid,
        # 信封结构完整但密文/nonce 均非真实材料 → 解密必败
        ct=json.dumps(
            {
                "v": 1,
                "alg": "A256GCM",
                "kid": _KID,
                "nonce": "AAAA",
                "ciphertext": "AAAA",
                "tag": "AAAA",
            }
        ),
    )
    assert await dispatch_once(v2_runtime, RecordingTransport()) == 0
    row = await _row(pg, oid)
    assert row["attempts"] == 1  # 解密失败等同一次投递失败
    assert row["state"] == "pending"
    remaining = await _seconds_until(pg, oid, "next_attempt_at")
    assert 55 <= remaining <= 65  # 首档退避 60s
    assert row["lease_owner"] is None and row["lease_expires_at"] is None


async def test_expired_lease_row_is_reclaimed(pg, v2_runtime, outbox_key):
    uid = await _seed_user(pg, "stale")
    oid = await _seed_leased_row(pg, uid, expires_sql="now() - interval '1 minute'")
    transport = RecordingTransport()
    assert await dispatch_once(v2_runtime, transport) == 1  # 崩溃恢复：过期租约可重新认领
    assert len(transport.sent) == 1
    row = await _row(pg, oid)
    assert row["state"] == "sent"
    assert row["lease_owner"] is None and row["lease_expires_at"] is None


async def test_fresh_lease_and_future_next_attempt_rows_are_not_claimed(pg, v2_runtime, outbox_key):
    uid = await _seed_user(pg, "held")
    leased = await _seed_leased_row(pg, uid, expires_sql="now() + interval '5 minutes'")
    future = await _enqueue_one(v2_runtime, uid, recipient="future@Example.COM")
    await _exec(
        pg,
        "UPDATE email_outbox SET next_attempt_at = now() + interval '1 hour' WHERE id = :id",
        id=future,
    )

    assert await dispatch_once(v2_runtime, RecordingTransport()) == 0
    row = await _row(pg, leased)
    assert row["state"] == "pending" and row["lease_owner"] == "outbox-dead"  # 租约未到期不抢
    row = await _row(pg, future)
    assert row["state"] == "pending" and row["attempts"] == 0  # 重试时间未到不认领


async def test_dispatch_respects_batch_limit(pg, v2_runtime, outbox_key):
    uid = await _seed_user(pg, "batch")
    oids = [await _enqueue_one(v2_runtime, uid, recipient=f"b{i}@Example.COM") for i in range(3)]
    transport = RecordingTransport()
    assert await dispatch_once(v2_runtime, transport, batch=2) == 2
    assert await dispatch_once(v2_runtime, transport) == 1  # 缺省 batch=10 取剩余
    assert len(transport.sent) == 3
    for oid in oids:
        assert (await _row(pg, oid))["state"] == "sent"
    with pytest.raises(ValueError):
        await dispatch_once(v2_runtime, transport, batch=0)  # 非法 batch 拒绝


# ---------- outbox_loop（lifespan 后台协程）----------


async def test_outbox_loop_survives_cycle_exception(
    pg, v2_runtime, outbox_key, monkeypatch, caplog
):
    calls = {"n": 0}

    async def flaky_dispatch(_rt, _transport, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db blip")
        return 0

    monkeypatch.setattr(outbox_mod, "dispatch_once", flaky_dispatch)
    caplog.set_level(logging.ERROR, logger="agentcraft.mail")
    task = asyncio.create_task(outbox_loop(v2_runtime, ConsoleMailTransport(), poll_seconds=0.01))
    try:
        async with asyncio.timeout(5):
            while calls["n"] < 2:
                await asyncio.sleep(0.02)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    assert calls["n"] >= 2  # 单轮异常只记录不外抛，循环继续（不杀进程）
    assert any("outbox dispatch cycle failed" in r.getMessage() for r in caplog.records)


async def test_outbox_loop_dispatches_until_cancelled(pg, v2_runtime, outbox_key):
    uid = await _seed_user(pg, "loop")
    oid = await _enqueue_one(v2_runtime, uid)
    transport = RecordingTransport()
    task = asyncio.create_task(outbox_loop(v2_runtime, transport, poll_seconds=0.01))
    try:
        async with asyncio.timeout(5):
            # 以 DB 终态为就绪条件（transport 记录先于终态提交，直接断言会竞态）
            while (await _row(pg, oid))["state"] != "sent":
                await asyncio.sleep(0.02)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    assert transport.sent[0][0] == "email_verify"
    assert (await _row(pg, oid))["state"] == "sent"
