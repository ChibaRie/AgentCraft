"""T7 task_artifacts 产物登记管道测试（Phase 6，D2 全回调写路径）。

纪律（T4 同型）：owner-RLS 种子走 superuser（tests/v2_task_helpers）；被测服务
一律真实 app 角色会话（owner_tx，GUC 已设）执行——禁 superuser 直跑（掩盖 RLS
空转）；真实写盘显式传 tmp 根 TaskStorage(tmp_path)。运行轮承载物经
seed_running_task（lease 三元组 + running reservation + 槽位）。
"""

import hashlib
import unicodedata
import urllib.parse
import uuid as _uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from backend.errors import AgentCraftError, ErrorCode
from backend.v2.task_artifacts import (
    download_headers,
    list_artifacts,
    register_output,
    resolve_download,
)
from backend.v2.task_storage import TaskStorage
from tests.v2_provider_helpers import seed_provider, seed_task_for_provider
from tests.v2_task_helpers import (
    owner_tx,
    seed_input_file,
    seed_running_task,
    seed_task_user,
)

_MIB = 1024 * 1024


@pytest.fixture
async def app_engine(role_engine):
    from tests.conftest import APP_ROLE

    engine = role_engine(APP_ROLE)
    yield engine
    await engine.dispose()


@pytest.fixture
async def domain(pg):
    """种子 user + BYOK provider；返回 (uid, pid)。"""
    uid = await seed_task_user(pg, "t7-artifacts@x.test")
    pid = await seed_provider(pg, uid)
    return str(uid), str(pid)


@pytest.fixture
def storage(tmp_path):
    return TaskStorage(tmp_path)


async def _running_round(pg, tid) -> tuple[str, int]:
    """任务的 running 轮 (round_id, lease_epoch)（seed_running_task 置 epoch=1）。"""
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
    assert row is not None, "种子未产生 running 轮"
    return str(row[0]), int(row[1])


async def _rotate_round(pg, uid: str, tid: str, old_round_id: str, *, seq: int = 99) -> str:
    """轮更替（第二次写入的来源轮）：旧轮 settled（让出活跃唯一索引）→ 新消息 +
    新 running 轮（epoch=1）。返回新 round_id。"""
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE task_rounds SET state = 'settled', lease_owner = NULL, "
                "lease_expires_at = NULL WHERE id = :r"
            ),
            {"r": old_round_id},
        )
        msg_id = (
            await conn.execute(
                text(
                    "INSERT INTO task_messages (id, task_id, owner_id, event_sequence, "
                    "author, content) VALUES (gen_random_uuid(), :t, :u, :s, 'user', 'rotate') "
                    "RETURNING id"
                ),
                {"t": tid, "u": uid, "s": seq},
            )
        ).scalar_one()
        new_id = (
            await conn.execute(
                text(
                    "INSERT INTO task_rounds (id, task_id, owner_id, source_message_id, "
                    "state, lease_owner, lease_epoch, lease_expires_at, attempt) "
                    "VALUES (gen_random_uuid(), :t, :u, :m, 'running', 'exec-test', 1, "
                    "now() + interval '90 seconds', 1) RETURNING id"
                ),
                {"t": tid, "u": uid, "m": msg_id},
            )
        ).scalar_one()
    return str(new_id)


async def _scalar(pg, sql, params=None):
    async with pg.engine.connect() as conn:
        return (await conn.execute(text(sql), params or {})).scalar_one()


async def _file_rows(pg, tid):
    async with pg.engine.connect() as conn:
        return (
            (
                await conn.execute(
                    text(
                        "SELECT id, file_name, direction, state, size_bytes, sha256, "
                        "produced_in_round_id FROM task_files WHERE task_id = :t "
                        "ORDER BY created_at, id"
                    ),
                    {"t": tid},
                )
            )
            .mappings()
            .all()
        )


async def _reservation_rows(pg, tid):
    async with pg.engine.connect() as conn:
        return (
            (
                await conn.execute(
                    text(
                        "SELECT kind, file_id, bytes, state FROM task_reservations "
                        "WHERE task_id = :t ORDER BY id"
                    ),
                    {"t": tid},
                )
            )
            .mappings()
            .all()
        )


async def _write(pg, app_engine, storage, uid, tid, rid, epoch, name, content):
    """owner_tx 内单次 register_output（测试书写糖）。"""
    async with owner_tx(app_engine, uid) as db:
        return await register_output(
            db,
            storage,
            owner_id=uid,
            task_id=tid,
            file_name=name,
            content=content,
            round_id=rid,
            lease_epoch=epoch,
        )


# ---------------------------------------------------------------------------
# register_output：登记 / 账务 / 物理双写
# ---------------------------------------------------------------------------


async def test_register_output_success_and_accounting(pg, app_engine, domain, storage):
    """登记全链：行（direction/state/produced_in_round_id）+ artifact_copy
    reservation（held, bytes=size）+ 用户/平台账条件增 + 物理双写一致。"""
    uid, pid = domain
    tid = await seed_running_task(pg, uid, pid)
    rid, epoch = await _running_round(pg, tid)
    content = b"ARTIFACT BYTES"
    out = await _write(pg, app_engine, storage, uid, tid, rid, epoch, "out.bin", content)
    assert set(out) == {"file"}
    entry = out["file"]
    assert set(entry) == {
        "id",
        "file_name",
        "sha256",
        "size_bytes",
        "state",
        "produced_in_round_id",
    }
    assert entry["state"] == "registered" and entry["size_bytes"] == len(content)
    assert entry["sha256"] == hashlib.sha256(content).hexdigest()
    assert entry["produced_in_round_id"] == rid

    rows = await _file_rows(pg, tid)
    assert len(rows) == 1
    row = rows[0]
    assert row["direction"] == "output" and row["state"] == "registered"
    assert str(row["produced_in_round_id"]) == rid
    assert row["size_bytes"] == len(content)

    copies = [r for r in await _reservation_rows(pg, tid) if r["kind"] == "artifact_copy"]
    assert len(copies) == 1
    assert copies[0]["state"] == "held" and copies[0]["bytes"] == len(content)
    assert str(copies[0]["file_id"]) == entry["id"]

    assert await _scalar(
        pg,
        "SELECT retained_storage_bytes FROM user_quota_usage WHERE user_id = :u",
        {"u": uid},
    ) == len(content)
    assert await _scalar(
        pg, "SELECT retained_storage_bytes FROM platform_storage WHERE singleton"
    ) == len(content)

    out_path = storage.output_path(tid, entry["id"])
    art_path = storage.artifact_path(tid, entry["id"])
    assert out_path.is_file() and out_path.read_bytes() == content
    assert art_path.is_file() and art_path.read_bytes() == content


async def test_register_output_upsert_overwrites(pg, app_engine, domain, storage):
    """upsert 覆写（D2）：同名（NFC 规范化）已 registered → 旧行 deleted + 旧
    artifact_copy reservation released（账退）+ 旧物理双文件删除；新行
    registered 且 hash/size/produced_in_round_id 更新；账净额 = 新行字节。"""
    uid, pid = domain
    tid = await seed_running_task(pg, uid, pid)
    rid1, epoch = await _running_round(pg, tid)
    v1, v2 = b"V1" * 50, b"V2" * 100
    out1 = await _write(pg, app_engine, storage, uid, tid, rid1, epoch, "out.txt", v1)
    fid1 = out1["file"]["id"]
    rid2 = await _rotate_round(pg, uid, tid, rid1)
    out2 = await _write(pg, app_engine, storage, uid, tid, rid2, 1, "out.txt", v2)
    fid2 = out2["file"]["id"]
    assert fid1 != fid2 and out2["file"]["produced_in_round_id"] == rid2

    rows = await _file_rows(pg, tid)
    assert len(rows) == 2
    old = next(r for r in rows if str(r["id"]) == fid1)
    new = next(r for r in rows if str(r["id"]) == fid2)
    assert old["state"] == "deleted"  # 墓碑留存（行不物理删）
    assert new["state"] == "registered"
    assert new["sha256"] == hashlib.sha256(v2).hexdigest()
    assert new["size_bytes"] == len(v2)
    assert str(new["produced_in_round_id"]) == rid2

    assert not storage.output_path(tid, fid1).exists()
    assert not storage.artifact_path(tid, fid1).exists()
    assert storage.output_path(tid, fid2).read_bytes() == v2
    assert storage.artifact_path(tid, fid2).read_bytes() == v2

    copies = [r for r in await _reservation_rows(pg, tid) if r["kind"] == "artifact_copy"]
    by_file = {str(r["file_id"]): r for r in copies}
    assert by_file[fid1]["state"] == "released" and by_file[fid1]["bytes"] == len(v1)
    assert by_file[fid2]["state"] == "held" and by_file[fid2]["bytes"] == len(v2)
    # 账净额：退 v1 + 收 v2 = len(v2)（用户维与平台维一致；无双重退还）
    assert await _scalar(
        pg,
        "SELECT retained_storage_bytes FROM user_quota_usage WHERE user_id = :u",
        {"u": uid},
    ) == len(v2)
    assert await _scalar(
        pg, "SELECT retained_storage_bytes FROM platform_storage WHERE singleton"
    ) == len(v2)


async def test_register_output_nfc_equivalent_name_overwrites(pg, app_engine, domain, storage):
    """规范化重复名纪律（同 T4 全集校验语义）：NFD 与 NFC 同名 → upsert 覆写
    （唯一存活产物行），不产生 NFC 冲突双行。"""
    uid, pid = domain
    tid = await seed_running_task(pg, uid, pid)
    rid, epoch = await _running_round(pg, tid)
    nfd_name = "café.txt"
    await _write(pg, app_engine, storage, uid, tid, rid, epoch, nfd_name, b"NFD")
    await _write(
        pg,
        app_engine,
        storage,
        uid,
        tid,
        rid,
        epoch,
        unicodedata.normalize("NFC", nfd_name),
        b"NFC",
    )
    rows = await _file_rows(pg, tid)
    live = [r for r in rows if r["state"] == "registered"]
    deleted = [r for r in rows if r["state"] == "deleted"]
    assert len(live) == 1 and len(deleted) == 1
    assert live[0]["sha256"] == hashlib.sha256(b"NFC").hexdigest()


async def test_register_output_not_bytes_rejected(pg, app_engine, domain, storage):
    uid, pid = domain
    tid = await seed_running_task(pg, uid, pid)
    rid, epoch = await _running_round(pg, tid)
    with pytest.raises(AgentCraftError) as ei:
        await _write(pg, app_engine, storage, uid, tid, rid, epoch, "x.bin", "str-not-bytes")
    assert ei.value.code is ErrorCode.TOOL_CALL_REJECTED and ei.value.http_status == 400


# ---------------------------------------------------------------------------
# 限额：单文件 5 MiB / 输入+产物总量 10 MiB（净口径）
# ---------------------------------------------------------------------------


async def test_register_output_single_file_cap(pg, app_engine, domain, storage):
    uid, pid = domain
    tid = await seed_running_task(pg, uid, pid)
    rid, epoch = await _running_round(pg, tid)
    with pytest.raises(AgentCraftError) as ei:
        await _write(
            pg, app_engine, storage, uid, tid, rid, epoch, "big.bin", b"\0" * (5 * _MIB + 1)
        )
    assert ei.value.code is ErrorCode.FILE_LIMIT_EXCEEDED and ei.value.http_status == 400
    out = await _write(
        pg, app_engine, storage, uid, tid, rid, epoch, "exact.bin", b"\0" * (5 * _MIB)
    )
    assert out["file"]["size_bytes"] == 5 * _MIB


async def test_register_output_total_cap_net_semantics(pg, app_engine, domain, storage):
    """总量 10 MiB（input+output 全部存活行）：7 MiB 输入 + 3 MiB 产物恰在上限；
    覆写按净值（旧行将被墓碑化）——naive 口径 13 MiB 会误拒，净口径 10 MiB 放行；
    此后再写 1 字节即越限。"""
    uid, pid = domain
    tid = await seed_running_task(pg, uid, pid)
    await seed_input_file(
        pg, uid, tid, size_bytes=7 * _MIB, state="committed", file_name="seed.bin"
    )
    rid, epoch = await _running_round(pg, tid)
    await _write(pg, app_engine, storage, uid, tid, rid, epoch, "o.bin", b"\0" * (3 * _MIB))
    # 覆写同名 3 MiB：净 = 7 + (3 - 3) + 3 = 10 ≤ 10 放行（naive 10+3=13 拒）
    await _write(pg, app_engine, storage, uid, tid, rid, epoch, "o.bin", b"\1" * (3 * _MIB))
    with pytest.raises(AgentCraftError) as ei:
        await _write(pg, app_engine, storage, uid, tid, rid, epoch, "p.bin", b"\0")
    assert ei.value.code is ErrorCode.FILE_LIMIT_EXCEEDED
    live = [r for r in await _file_rows(pg, tid) if r["state"] != "deleted"]
    assert sum(int(r["size_bytes"]) for r in live) == 10 * _MIB


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("../x", id="traversal-slash"),
        pytest.param("a/b", id="slash"),
        pytest.param("a\\b", id="backslash"),
        pytest.param("a\nb", id="control-char"),
        pytest.param("a\x00b", id="nul-byte"),
        pytest.param(".", id="dot"),
        pytest.param("..", id="dot-dot"),
    ],
)
async def test_register_output_rejects_bad_names(pg, app_engine, domain, storage, tmp_path, name):
    """文件名全集校验（复用 T4 _validate_file_name，不复制漂移——异常载体与 T4
    同型 HTTPException）：400 VALIDATION_ERROR；无行、无物理文件。"""
    uid, pid = domain
    tid = await seed_running_task(pg, uid, pid)
    rid, epoch = await _running_round(pg, tid)
    with pytest.raises(HTTPException) as ei:
        await _write(pg, app_engine, storage, uid, tid, rid, epoch, name, b"x")
    assert ei.value.status_code == 400
    assert ei.value.detail["code"] == "VALIDATION_ERROR"
    assert await _scalar(pg, "SELECT count(*) FROM task_files") == 0
    assert not (tmp_path / "tasks").exists()


# ---------------------------------------------------------------------------
# 轮面 fence 与任务行闸
# ---------------------------------------------------------------------------


async def test_register_output_requires_live_running_round(pg, app_engine, domain, storage):
    """fence 条件仲裁：pending 轮（queued 任务）/ 错 epoch / settled 轮（ready
    任务）→ TOOL_CALL_REJECTED 400（执行器写侧围栏同型）。"""
    uid, pid = domain
    # queued 任务：pending 轮 epoch=0
    tid_q = await seed_task_for_provider(pg, uid, pid, status="queued")
    async with pg.engine.connect() as conn:
        pending_id = (
            await conn.execute(
                text("SELECT id FROM task_rounds WHERE task_id = :t AND state = 'pending'"),
                {"t": tid_q},
            )
        ).scalar_one()
    with pytest.raises(AgentCraftError) as ei:
        await _write(pg, app_engine, storage, uid, tid_q, str(pending_id), 0, "o.txt", b"x")
    assert ei.value.code is ErrorCode.TOOL_CALL_REJECTED
    # running 任务 + 错 epoch（fence 后旧 epoch 写入拒绝）
    tid = await seed_running_task(pg, uid, pid)
    rid, epoch = await _running_round(pg, tid)
    with pytest.raises(AgentCraftError) as ei_e:
        await _write(pg, app_engine, storage, uid, tid, rid, epoch + 1, "o.txt", b"x")
    assert ei_e.value.code is ErrorCode.TOOL_CALL_REJECTED
    # ready 任务：轮 settled
    await _rotate_round(pg, uid, tid, rid)
    async with pg.engine.begin() as conn:
        await conn.execute(text("UPDATE tasks SET status = 'ready' WHERE id = :t"), {"t": tid})
    with pytest.raises(AgentCraftError) as ei_s:
        await _write(pg, app_engine, storage, uid, tid, rid, 1, "o.txt", b"x")
    assert ei_s.value.code is ErrorCode.TOOL_CALL_REJECTED
    assert await _scalar(pg, "SELECT count(*) FROM task_files") == 0


async def test_register_output_missing_or_deleted_task_404(pg, app_engine, domain, storage):
    uid, pid = domain
    tid = await seed_running_task(pg, uid, pid)
    rid, epoch = await _running_round(pg, tid)
    with pytest.raises(AgentCraftError) as ei_m:
        await _write(pg, app_engine, storage, uid, str(_uuid.uuid4()), rid, epoch, "o.txt", b"x")
    assert ei_m.value.code is ErrorCode.TASK_NOT_FOUND and ei_m.value.http_status == 404
    deleted_tid = await seed_task_for_provider(pg, uid, pid, status="deleted")
    with pytest.raises(AgentCraftError) as ei_d:
        await _write(pg, app_engine, storage, uid, deleted_tid, rid, epoch, "o.txt", b"x")
    assert ei_d.value.code is ErrorCode.TASK_NOT_FOUND


async def test_register_output_storage_quota_exceeded(pg, app_engine, storage, tmp_path):
    """存储账条件增失败（上限谓词 rowcount=0）→ 429 QUOTA_STORAGE_EXCEEDED，
    事务整体回滚：无行、无 reservation、账原样、无物理文件。"""
    uid = await seed_task_user(pg, "t7-quota@x.test", max_retained_storage_bytes=1024)
    pid = await seed_provider(pg, uid)
    tid = await seed_running_task(pg, uid, pid)
    rid, epoch = await _running_round(pg, tid)
    with pytest.raises(AgentCraftError) as ei:
        await _write(pg, app_engine, storage, uid, tid, rid, epoch, "o.bin", b"\0" * 2048)
    assert ei.value.code is ErrorCode.QUOTA_STORAGE_EXCEEDED and ei.value.http_status == 429
    assert await _scalar(pg, "SELECT count(*) FROM task_files") == 0
    assert (
        await _scalar(pg, "SELECT count(*) FROM task_reservations WHERE kind = 'artifact_copy'")
        == 0
    )
    assert (
        await _scalar(
            pg,
            "SELECT retained_storage_bytes FROM user_quota_usage WHERE user_id = :u",
            {"u": uid},
        )
        == 0
    )
    assert not (tmp_path / "tasks").exists() and not (tmp_path / "artifacts").exists()


# ---------------------------------------------------------------------------
# resolve_download / download_headers / list_artifacts（T8a 路由消费面）
# ---------------------------------------------------------------------------


async def test_resolve_download_roundtrip_and_headers(pg, app_engine, domain, storage):
    """resolve_download：artifacts 副本物理路径 + 元数据钉死；download_headers
    attachment/nosniff（ASCII 名与非 ASCII 名 RFC 5987 双形）。"""
    uid, pid = domain
    tid = await seed_running_task(pg, uid, pid)
    rid, epoch = await _running_round(pg, tid)
    content = b"DOWNLOAD ME"
    out = await _write(pg, app_engine, storage, uid, tid, rid, epoch, "交付.bin", content)
    fid = out["file"]["id"]
    async with owner_tx(app_engine, uid) as db:
        path, meta = await resolve_download(db, storage, owner_id=uid, task_id=tid, file_id=fid)
    assert path == storage.artifact_path(tid, fid)
    assert path.read_bytes() == content
    assert meta == {
        "file_name": "交付.bin",
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }
    headers = download_headers("交付.bin")
    assert headers["X-Content-Type-Options"] == "nosniff"
    disposition = headers["Content-Disposition"]
    assert disposition.startswith("attachment;")
    assert 'filename="__.bin"' in disposition  # 非 ASCII 落 `_` 的 ASCII 兜底
    assert f"filename*=UTF-8''{urllib.parse.quote('交付.bin', safe='')}" in disposition
    assert download_headers("plain.txt")["Content-Disposition"] == (
        "attachment; filename=\"plain.txt\"; filename*=UTF-8''plain.txt"
    )


async def test_resolve_download_negatives(pg, app_engine, domain, storage):
    """deleted 任务/缺失/他人（RLS）/非产物面 → 统一 404（TASK_NOT_FOUND /
    FILE_NOT_FOUND 分流）。"""
    uid, pid = domain
    tid = await seed_running_task(pg, uid, pid)
    rid, epoch = await _running_round(pg, tid)
    out = await _write(pg, app_engine, storage, uid, tid, rid, epoch, "o.txt", b"OK")
    fid = out["file"]["id"]
    input_fid = await seed_input_file(pg, uid, tid, size_bytes=1, file_name="i.txt")
    other = await seed_task_user(pg, "t7-other@x.test")
    with pytest.raises(AgentCraftError) as ei_i:
        async with owner_tx(app_engine, uid) as db:
            await resolve_download(db, storage, owner_id=uid, task_id=tid, file_id=input_fid)
    assert ei_i.value.code is ErrorCode.FILE_NOT_FOUND and ei_i.value.http_status == 404
    with pytest.raises(AgentCraftError) as ei_u:
        async with owner_tx(app_engine, other) as db:
            await resolve_download(db, storage, owner_id=other, task_id=tid, file_id=fid)
    assert ei_u.value.code is ErrorCode.TASK_NOT_FOUND
    deleted_tid = await seed_task_for_provider(pg, uid, pid, status="deleted")
    with pytest.raises(AgentCraftError) as ei_d:
        async with owner_tx(app_engine, uid) as db:
            await resolve_download(db, storage, owner_id=uid, task_id=deleted_tid, file_id=fid)
    assert ei_d.value.code is ErrorCode.TASK_NOT_FOUND
    with pytest.raises(AgentCraftError) as ei_m:
        async with owner_tx(app_engine, uid) as db:
            await resolve_download(
                db, storage, owner_id=uid, task_id=tid, file_id=str(_uuid.uuid4())
            )
    assert ei_m.value.code is ErrorCode.FILE_NOT_FOUND


async def test_list_artifacts_shape_and_rls(pg, app_engine, domain, storage):
    """产物列表（T8a 服务面）：含 produced_in_round_id、墓碑不可见、稳定序；
    他人（RLS）与已删除任务统一 404。"""
    uid, pid = domain
    tid = await seed_running_task(pg, uid, pid)
    rid1, epoch = await _running_round(pg, tid)
    await _write(pg, app_engine, storage, uid, tid, rid1, epoch, "a.txt", b"A")
    rid2 = await _rotate_round(pg, uid, tid, rid1, seq=98)
    await _write(pg, app_engine, storage, uid, tid, rid2, 1, "b.txt", b"B")
    await _write(pg, app_engine, storage, uid, tid, rid2, 1, "a.txt", b"A2")  # 覆写墓碑化旧行
    other = await seed_task_user(pg, "t7-list-other@x.test")
    async with owner_tx(app_engine, other) as db:
        with pytest.raises(AgentCraftError) as ei_u:
            await list_artifacts(db, owner_id=other, task_id=tid)
    assert ei_u.value.code is ErrorCode.TASK_NOT_FOUND
    async with owner_tx(app_engine, uid) as db:
        items = await list_artifacts(db, owner_id=uid, task_id=tid)
    # created_at ASC：a.txt 旧行已墓碑化，存活序 = b.txt（第 2 次写）→ a.txt 新行（第 3 次写）
    assert [i["file_name"] for i in items] == ["b.txt", "a.txt"]
    by_name = {i["file_name"]: i for i in items}
    assert by_name["a.txt"]["sha256"] == hashlib.sha256(b"A2").hexdigest()
    assert by_name["a.txt"]["produced_in_round_id"] == rid2
    assert by_name["b.txt"]["produced_in_round_id"] == rid2
    assert all(
        set(i) == {"id", "file_name", "sha256", "size_bytes", "state", "produced_in_round_id"}
        for i in items
    )
    rows = await _file_rows(pg, tid)
    assert sum(1 for r in rows if r["state"] == "deleted") == 1  # 墓碑不进列表
    deleted_tid = await seed_task_for_provider(pg, uid, pid, status="deleted")
    async with owner_tx(app_engine, uid) as db:
        with pytest.raises(AgentCraftError) as ei_d:
            await list_artifacts(db, owner_id=uid, task_id=deleted_tid)
    assert ei_d.value.code is ErrorCode.TASK_NOT_FOUND
