"""T4 task_file_service 输入文件面测试（Phase 6）。

纪律：owner-RLS 种子走 superuser（tests/v2_task_helpers）；被测服务一律真实
app 角色会话（owner_tx，GUC 已设）执行——禁 superuser 直跑（掩盖 RLS 空转）；
真实写盘测试显式传 tmp 根 TaskStorage(tmp_path)（T2 顺延 M2）；delete_file 无
storage 形参（计划冻结签名），物理删根按 Settings 现读——测试经
V2_TASK__STORAGE_ROOT env 钉到 tmp 根（get_settings 无缓存，逐次现读）。
"""

import asyncio
import hashlib
import uuid as _uuid
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from backend.errors import AgentCraftError, ErrorCode
from backend.v2.task_file_service import (
    delete_file,
    list_files,
    list_input_meta,
    read_input_bytes,
    upload_files,
)
from backend.v2.task_service import commit_input, create_task
from backend.v2.task_storage import TaskStorage
from tests.conftest import APP_ROLE
from tests.v2_provider_helpers import seed_provider, seed_task_for_provider
from tests.v2_task_helpers import (
    owner_tx,
    seed_input_file,
    seed_published_revision,
    seed_task_user,
)

_MIB = 1024 * 1024


@pytest.fixture
async def app_engine(role_engine):
    engine = role_engine(APP_ROLE)
    yield engine
    await engine.dispose()


@pytest.fixture
async def domain(pg):
    """种子 user + BYOK provider + published revision；返回 (uid, pid, rid, cid)。"""
    uid = await seed_task_user(pg, "t4-user@x.test")
    pid = await seed_provider(pg, uid)
    rid = await seed_published_revision(pg, uid)
    async with pg.engine.connect() as conn:
        cid = str(
            (
                await conn.execute(
                    text("SELECT catalog_id FROM user_providers WHERE id = :p"),
                    {"p": str(pid)},
                )
            ).scalar_one()
        )
    return uid, pid, rid, cid


@pytest.fixture
def storage(tmp_path) -> TaskStorage:
    """upload/read 面的真实写盘根（T2 顺延 M2：显式 tmp 根，禁 CWD 相对默认根）。"""
    return TaskStorage(tmp_path)


@pytest.fixture
def delete_root(tmp_path, monkeypatch) -> Path:
    """delete_file 物理删根：计划冻结签名无 storage 形参，实现按 Settings 现读
    V2_TASK.storage_root——env 钉到同一 tmp 根。"""
    monkeypatch.setenv("V2_TASK__STORAGE_ROOT", str(tmp_path))
    return tmp_path


def _snapshot(pid, cid) -> dict:
    """D14 快照键（与 ResolvedProvider 一一对应）。"""
    return {
        "provider_id": str(pid),
        "provider_catalog_id": str(cid),
        "provider_model_id": "gpt-4o-mini",
        "provider_key_version": 1,
    }


async def _uploading_task(pg, domain) -> tuple[str, str]:
    """superuser 造 uploading 任务（active reservation 持有）；返回 (uid, tid)。

    str() 归一：text() 路径的 asyncpg 返回原生 pgproto.UUID 对象（T3 _parse_id
    的 str 强转同源问题）——本文件断言直接拼 f-string/路径，入口即钉字符串。"""
    uid, pid, _rid, _cid = domain
    tid = await seed_task_for_provider(pg, uid, pid, status="uploading")
    return str(uid), str(tid)


async def _scalar(pg, sql, params=None):
    async with pg.engine.connect() as conn:
        return (await conn.execute(text(sql), params or {})).scalar_one()


async def _file_rows(pg, tid):
    async with pg.engine.connect() as conn:
        return (
            (
                await conn.execute(
                    text(
                        "SELECT file_name, storage_key, sha256, size_bytes, state, direction, "
                        "owner_id FROM task_files WHERE task_id = :t"
                    ),
                    {"t": tid},
                )
            )
            .mappings()
            .all()
        )


# ---------------------------------------------------------------------------
# upload_files：staged 时序 / 限额 / 文件名全集校验 / 原子性
# ---------------------------------------------------------------------------


async def test_upload_files_success(pg, app_engine, domain, storage):
    """staged 全链：行（direction/state/owner/sha256/size）+ storage_key 逻辑键
    形态（DB §3:109）+ 物理文件落在 inputs/ 且字节一致。"""
    uid, tid = await _uploading_task(pg, domain)
    uploads = [("hello.txt", b"hello world"), ("数据.bin", bytes(range(256)))]
    async with owner_tx(app_engine, uid) as db:
        out = await upload_files(db, storage, owner_id=uid, task_id=tid, uploads=uploads)
    assert set(out) == {"files"} and len(out["files"]) == 2
    rows = await _file_rows(pg, tid)
    assert len(rows) == 2
    for entry, (name, content) in zip(out["files"], uploads, strict=True):
        assert set(entry) == {"id", "file_name", "sha256", "size_bytes", "state"}
        assert entry["file_name"] == name and entry["state"] == "staged"
        assert entry["sha256"] == hashlib.sha256(content).hexdigest()
        assert entry["size_bytes"] == len(content)
        row = next(r for r in rows if r["file_name"] == name)
        fid = entry["id"]
        assert row["storage_key"] == f"tasks/{tid}/{fid}"
        assert row["direction"] == "input" and row["state"] == "staged"
        assert str(row["owner_id"]) == uid
        path = storage.input_path(tid, fid)
        assert path.exists() and path.read_bytes() == content


async def test_upload_files_count_cap(pg, app_engine, domain, storage):
    """单任务 10 个（PRD §4.1:108）：9 存量 + 1 = 10 恰在上限，第 11 个
    FILE_LIMIT_EXCEEDED 400；超限批零残留。"""
    uid, tid = await _uploading_task(pg, domain)
    for i in range(9):
        await seed_input_file(pg, uid, tid, size_bytes=1, file_name=f"f{i}.txt")
    async with owner_tx(app_engine, uid) as db:
        out = await upload_files(
            db, storage, owner_id=uid, task_id=tid, uploads=[("new.txt", b"x")]
        )
    assert len(out["files"]) == 1
    with pytest.raises(AgentCraftError) as ei:
        async with owner_tx(app_engine, uid) as db:
            await upload_files(db, storage, owner_id=uid, task_id=tid, uploads=[("over.txt", b"x")])
    assert ei.value.code is ErrorCode.FILE_LIMIT_EXCEEDED
    assert ei.value.http_status == 400
    assert len(await _file_rows(pg, tid)) == 10


async def test_upload_files_single_size_cap(pg, app_engine, domain, storage):
    """单个 5 MiB（PRD §4.1:108）：5 MiB + 1 字节拒绝，恰 5 MiB 放行。"""
    uid, tid = await _uploading_task(pg, domain)
    with pytest.raises(AgentCraftError) as ei:
        async with owner_tx(app_engine, uid) as db:
            await upload_files(
                db,
                storage,
                owner_id=uid,
                task_id=tid,
                uploads=[("big.bin", b"\0" * (5 * _MIB + 1))],
            )
    assert ei.value.code is ErrorCode.FILE_LIMIT_EXCEEDED
    assert ei.value.http_status == 400
    async with owner_tx(app_engine, uid) as db:
        out = await upload_files(
            db, storage, owner_id=uid, task_id=tid, uploads=[("exact.bin", b"\0" * (5 * _MIB))]
        )
    assert out["files"][0]["size_bytes"] == 5 * _MIB


async def test_upload_files_total_cap(pg, app_engine, domain, storage):
    """输入+产物总量 10 MiB（PRD §4.1:109）：存量 6 MiB + 新增 4 MiB + 1 字节
    拒绝，恰 10 MiB 放行。"""
    uid, tid = await _uploading_task(pg, domain)
    await seed_input_file(pg, uid, tid, size_bytes=6 * _MIB, file_name="seed.bin")
    with pytest.raises(AgentCraftError) as ei:
        async with owner_tx(app_engine, uid) as db:
            await upload_files(
                db,
                storage,
                owner_id=uid,
                task_id=tid,
                uploads=[("over.bin", b"\0" * (4 * _MIB + 1))],
            )
    assert ei.value.code is ErrorCode.FILE_LIMIT_EXCEEDED
    async with owner_tx(app_engine, uid) as db:
        out = await upload_files(
            db, storage, owner_id=uid, task_id=tid, uploads=[("exact.bin", b"\0" * (4 * _MIB))]
        )
    assert out["files"][0]["size_bytes"] == 4 * _MIB


async def test_upload_files_tombstones_free_count_slot(
    pg, app_engine, domain, storage, delete_root
):
    """存量口径 = 存活行（deleted 墓碑不计）：10 staged 删 1 → 9 存活 + 1 放行，
    再传即撞上限。"""
    uid, tid = await _uploading_task(pg, domain)
    fids = [
        await seed_input_file(pg, uid, tid, size_bytes=1, file_name=f"g{i}.txt") for i in range(10)
    ]
    async with owner_tx(app_engine, uid) as db:
        await delete_file(db, owner_id=uid, task_id=tid, file_id=fids[0])
    async with owner_tx(app_engine, uid) as db:
        out = await upload_files(db, storage, owner_id=uid, task_id=tid, uploads=[("n.txt", b"x")])
    assert len(out["files"]) == 1
    with pytest.raises(AgentCraftError) as ei:
        async with owner_tx(app_engine, uid) as db:
            await upload_files(db, storage, owner_id=uid, task_id=tid, uploads=[("m.txt", b"x")])
    assert ei.value.code is ErrorCode.FILE_LIMIT_EXCEEDED


@pytest.mark.parametrize(
    "uploads",
    [
        pytest.param([("../x", b"x")], id="traversal-slash"),
        pytest.param([("a\\b", b"x")], id="backslash"),
        pytest.param([("a\nb", b"x")], id="control-char"),
        pytest.param([("a\x00b", b"x")], id="nul-byte"),
        pytest.param([("..", b"x")], id="dot-dot"),
        pytest.param([("é.txt", b"x"), ("é.txt", b"x")], id="nfc-duplicate"),
    ],
)
async def test_upload_files_rejects_malicious_names(
    pg, app_engine, domain, storage, tmp_path, uploads
):
    """文件名全集校验（Eng §5.2:133 + 空字节 + NFC 规范化重复名）：400
    VALIDATION_ERROR；校验先于一切写——无行、无任何物理文件。"""
    uid, tid = await _uploading_task(pg, domain)
    with pytest.raises(HTTPException) as ei:
        async with owner_tx(app_engine, uid) as db:
            await upload_files(db, storage, owner_id=uid, task_id=tid, uploads=uploads)
    assert ei.value.status_code == 400
    assert ei.value.detail["code"] == "VALIDATION_ERROR"
    assert await _scalar(pg, "SELECT count(*) FROM task_files") == 0
    assert not (tmp_path / "tasks").exists() and not (tmp_path / "staging").exists()


async def test_upload_files_rename_failure_atomic(
    pg, app_engine, domain, storage, tmp_path, monkeypatch
):
    """原子性（Eng §5.2:1「写入失败不登记文件」）：第二个 rename 失败 → 整批
    回退——无行、无文件（首个已 rename 目标补偿清退 + staging 残留清空）。"""
    uid, tid = await _uploading_task(pg, domain)
    real_replace = Path.replace
    calls = {"n": 0}

    def flaky_replace(self, target):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("simulated rename failure")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", flaky_replace)
    with pytest.raises(OSError):
        async with owner_tx(app_engine, uid) as db:
            await upload_files(
                db, storage, owner_id=uid, task_id=tid, uploads=[("a.txt", b"A"), ("b.txt", b"B")]
            )
    assert await _scalar(pg, "SELECT count(*) FROM task_files WHERE task_id = :t", {"t": tid}) == 0
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


async def test_upload_boundary_validation(pg, app_engine, domain, storage):
    """边界形状：空批 / 非 bytes 内容 / 批内裸重复名 / 非法 task_id 段 → 400。"""
    uid, tid = await _uploading_task(pg, domain)
    for bad in ([], [("a.txt", "str-not-bytes")], [("a.txt", b"x"), ("a.txt", b"y")]):
        with pytest.raises(HTTPException) as ei:
            async with owner_tx(app_engine, uid) as db:
                await upload_files(db, storage, owner_id=uid, task_id=tid, uploads=bad)
        assert ei.value.status_code == 400
        assert ei.value.detail["code"] == "VALIDATION_ERROR"
    with pytest.raises(HTTPException) as ei_tid:
        async with owner_tx(app_engine, uid) as db:
            await upload_files(
                db, storage, owner_id=uid, task_id="../etc", uploads=[("a.txt", b"x")]
            )
    assert ei_tid.value.status_code == 400
    assert await _scalar(pg, "SELECT count(*) FROM task_files") == 0


# ---------------------------------------------------------------------------
# delete_file：staged 墓碑 + 物理删（D7a）
# ---------------------------------------------------------------------------


async def test_delete_staged_file_tombstones_and_unlinks(
    pg, app_engine, domain, storage, delete_root
):
    """delete staged 行：墓碑留存 + 物理文件移除 + 列表不可见；再删同 id
    FILE_NOT_FOUND 404。"""
    uid, tid = await _uploading_task(pg, domain)
    async with owner_tx(app_engine, uid) as db:
        out = await upload_files(
            db, storage, owner_id=uid, task_id=tid, uploads=[("gone.txt", b"BYE")]
        )
    fid = out["files"][0]["id"]
    path = storage.input_path(tid, fid)
    assert path.exists()
    async with owner_tx(app_engine, uid) as db:
        res = await delete_file(db, owner_id=uid, task_id=tid, file_id=fid)
    assert res == {"file": {"id": fid, "state": "deleted"}}
    assert not path.exists()
    assert [r["state"] for r in await _file_rows(pg, tid)] == ["deleted"]
    async with owner_tx(app_engine, uid) as db:
        assert await list_files(db, owner_id=uid, task_id=tid, direction="input") == []
    with pytest.raises(AgentCraftError) as ei:
        async with owner_tx(app_engine, uid) as db:
            await delete_file(db, owner_id=uid, task_id=tid, file_id=fid)
    assert ei.value.code is ErrorCode.FILE_NOT_FOUND and ei.value.http_status == 404


async def test_delete_file_not_staged_or_missing_404(pg, app_engine, domain, storage, delete_root):
    """行必须 staged：committed 行（任务仍 uploading 的人为态）/ registered 产物
    行 / 未知 file_id → 一律 FILE_NOT_FOUND 404。"""
    uid, tid = await _uploading_task(pg, domain)
    committed_fid = await seed_input_file(
        pg, uid, tid, size_bytes=1, state="committed", file_name="c.txt"
    )
    output_fid = await seed_input_file(
        pg, uid, tid, size_bytes=1, direction="output", state="registered", file_name="o.bin"
    )
    for fid in (committed_fid, output_fid, str(_uuid.uuid4())):
        with pytest.raises(AgentCraftError) as ei:
            async with owner_tx(app_engine, uid) as db:
                await delete_file(db, owner_id=uid, task_id=tid, file_id=fid)
        assert ei.value.code is ErrorCode.FILE_NOT_FOUND
        assert ei.value.http_status == 404


# ---------------------------------------------------------------------------
# uploading 仲裁：冻结后 / 终态 / 与 commit 竞态
# ---------------------------------------------------------------------------


async def test_upload_after_commit_and_terminal_rejected(pg, app_engine, domain, storage):
    """commit 后上传 → INPUT_COMMITTED 409；终态（failed）任务上传 →
    TASK_INVALID_TRANSITION 409（与 commit_input 同型分流）。"""
    uid, pid, rid, cid = domain
    async with owner_tx(app_engine, uid) as db:
        out = await create_task(
            db,
            owner_id=uid,
            expert_revision_id=rid,
            provider_id=pid,
            initial_message="t4 冻结",
            provider_snapshot=_snapshot(pid, cid),
        )
    tid = out["task"]["id"]
    async with owner_tx(app_engine, uid) as db:
        await commit_input(db, owner_id=uid, task_id=tid, manifest=[])
    with pytest.raises(AgentCraftError) as ei:
        async with owner_tx(app_engine, uid) as db:
            await upload_files(db, storage, owner_id=uid, task_id=tid, uploads=[("x.txt", b"x")])
    assert ei.value.code is ErrorCode.INPUT_COMMITTED and ei.value.http_status == 409
    failed_tid = await seed_task_for_provider(pg, uid, pid, status="failed")
    with pytest.raises(AgentCraftError) as ei_t:
        async with owner_tx(app_engine, uid) as db:
            await upload_files(
                db, storage, owner_id=uid, task_id=failed_tid, uploads=[("x.txt", b"x")]
            )
    assert ei_t.value.code is ErrorCode.TASK_INVALID_TRANSITION
    assert ei_t.value.http_status == 409


async def test_upload_vs_commit_race_no_dirty_state(pg, role_engine, domain, storage):
    """并发仲裁不变量（双 role_engine + gather）：upload 与 commit 竞态在 FOR
    UPDATE 行锁串行化下只有两种合法序——commit 先胜（upload INPUT_COMMITTED
    409，仲裁先于一切写）或 upload 先胜（commit 正常冻结：上传本属 uploading
    阶段，序贯 409 面由 test_upload_after_commit_and_terminal_rejected 钉死）。
    无论何种序：commit 恒成功、任务终态 queued、无 staged 残留、task_root 恰一。
    """
    uid, pid, rid, cid = domain
    e1 = role_engine(APP_ROLE)
    e2 = role_engine(APP_ROLE)
    try:
        async with owner_tx(e1, uid) as db:
            out = await create_task(
                db,
                owner_id=uid,
                expert_revision_id=rid,
                provider_id=pid,
                initial_message="t4 竞态",
                provider_snapshot=_snapshot(pid, cid),
            )
        tid = out["task"]["id"]

        async def do_upload():
            async with owner_tx(e1, uid) as db:
                return await upload_files(
                    db, storage, owner_id=uid, task_id=tid, uploads=[("r.txt", b"x")]
                )

        async def do_commit():
            async with owner_tx(e2, uid) as db:
                return await commit_input(db, owner_id=uid, task_id=tid, manifest=[])

        up_res, commit_res = await asyncio.gather(do_upload(), do_commit(), return_exceptions=True)
    finally:
        await e1.dispose()
        await e2.dispose()

    # gather 保序（参数序）：commit 恒成功——upload 不是 commit 的冲突面
    assert not isinstance(commit_res, BaseException)
    # upload：行锁先胜则成功，否则恰 INPUT_COMMITTED 409——绝无第三种结果
    if isinstance(up_res, dict):
        assert set(up_res) == {"files"}
        fid = up_res["files"][0]["id"]
    else:
        assert isinstance(up_res, AgentCraftError)
        assert up_res.code is ErrorCode.INPUT_COMMITTED and up_res.http_status == 409
        fid = None
    # 终态一致：queued + task_root 恰一 + 行与盘一致（committed 或零行）
    assert await _scalar(pg, "SELECT status FROM tasks WHERE id = :t", {"t": tid}) == "queued"
    assert (
        await _scalar(
            pg,
            "SELECT count(*) FROM task_reservations WHERE task_id = :t AND kind = 'task_root'",
            {"t": tid},
        )
        == 1
    )
    rows = await _file_rows(pg, tid)
    if fid is None:
        assert rows == []
    else:
        assert len(rows) == 1
        assert rows[0]["state"] == "committed"  # commit 的 staged→committed 翻转已及
        assert storage.input_path(tid, fid).exists()


# ---------------------------------------------------------------------------
# RLS 与读取/元数据面
# ---------------------------------------------------------------------------


async def test_other_owner_file_ops_404(pg, app_engine, domain, storage, delete_root):
    """RLS 统一 404（Sup §7）：他人任务的 upload/delete/list → TASK_NOT_FOUND；
    他人任务的 read_input_bytes → FILE_NOT_FOUND（同 404 族）。"""
    uid, tid = await _uploading_task(pg, domain)
    other = await seed_task_user(pg, "t4-other@x.test")
    fid = await seed_input_file(pg, uid, tid, size_bytes=1, file_name="mine.txt")
    with pytest.raises(AgentCraftError) as ei_u:
        async with owner_tx(app_engine, other) as db:
            await upload_files(db, storage, owner_id=other, task_id=tid, uploads=[("x.txt", b"x")])
    assert ei_u.value.code is ErrorCode.TASK_NOT_FOUND and ei_u.value.http_status == 404
    with pytest.raises(AgentCraftError) as ei_d:
        async with owner_tx(app_engine, other) as db:
            await delete_file(db, owner_id=other, task_id=tid, file_id=fid)
    assert ei_d.value.code is ErrorCode.TASK_NOT_FOUND
    with pytest.raises(AgentCraftError) as ei_l:
        async with owner_tx(app_engine, other) as db:
            await list_files(db, owner_id=other, task_id=tid, direction="input")
    assert ei_l.value.code is ErrorCode.TASK_NOT_FOUND
    with pytest.raises(AgentCraftError) as ei_r:
        async with owner_tx(app_engine, other) as db:
            await read_input_bytes(storage, db, owner_id=other, task_id=tid, file_name="mine.txt")
    assert ei_r.value.code is ErrorCode.FILE_NOT_FOUND and ei_r.value.http_status == 404


async def test_list_files_direction_filter_and_shape(pg, app_engine, domain, storage, delete_root):
    """列表（Sup §4:103 含 sha256/size/state）：direction 词表过滤、deleted 墓碑
    不可见、词表外 400。"""
    uid, tid = await _uploading_task(pg, domain)
    async with owner_tx(app_engine, uid) as db:
        out = await upload_files(
            db, storage, owner_id=uid, task_id=tid, uploads=[("a.txt", b"A"), ("b.txt", b"BB")]
        )
    await seed_input_file(
        pg, uid, tid, size_bytes=9, direction="output", state="registered", file_name="o.bin"
    )
    async with owner_tx(app_engine, uid) as db:
        files = await list_files(db, owner_id=uid, task_id=tid, direction="input")
        outputs = await list_files(db, owner_id=uid, task_id=tid, direction="output")
        await delete_file(db, owner_id=uid, task_id=tid, file_id=out["files"][0]["id"])
        after = await list_files(db, owner_id=uid, task_id=tid, direction="input")
    assert {f["file_name"] for f in files} == {"a.txt", "b.txt"}
    assert all(set(f) == {"id", "file_name", "sha256", "size_bytes", "state"} for f in files)
    by_name = {f["file_name"]: f for f in files}
    assert by_name["b.txt"]["size_bytes"] == 2 and by_name["b.txt"]["state"] == "staged"
    assert by_name["a.txt"]["sha256"] == hashlib.sha256(b"A").hexdigest()
    assert [o["file_name"] for o in outputs] == ["o.bin"]
    assert [f["file_name"] for f in after] == ["b.txt"]
    with pytest.raises(HTTPException) as ei:
        async with owner_tx(app_engine, uid) as db:
            await list_files(db, owner_id=uid, task_id=tid, direction="both")
    assert ei.value.status_code == 400


async def test_read_input_bytes_roundtrip(pg, app_engine, domain, storage):
    """T7 读取面：字节回读一致；缺失名与穿越形名（../x）→ FILE_NOT_FOUND 404
    ——file_name 只作行查询键，永不参与路径拼接。"""
    uid, tid = await _uploading_task(pg, domain)
    async with owner_tx(app_engine, uid) as db:
        await upload_files(
            db, storage, owner_id=uid, task_id=tid, uploads=[("hello.txt", b"HELLO BYTES")]
        )
        got = await read_input_bytes(storage, db, owner_id=uid, task_id=tid, file_name="hello.txt")
        assert got == b"HELLO BYTES"
        for missing in ("nope.txt", "../x"):
            with pytest.raises(AgentCraftError) as ei:
                await read_input_bytes(storage, db, owner_id=uid, task_id=tid, file_name=missing)
            assert ei.value.code is ErrorCode.FILE_NOT_FOUND
            assert ei.value.http_status == 404


async def test_read_input_bytes_missing_physical_file_404(pg, app_engine, domain, storage):
    """行在盘无（OSError 兜底）：FILE_NOT_FOUND 404，不裸抛。"""
    uid, tid = await _uploading_task(pg, domain)
    await seed_input_file(pg, uid, tid, size_bytes=3, file_name="ghost.txt")
    async with owner_tx(app_engine, uid) as db:
        with pytest.raises(AgentCraftError) as ei:
            await read_input_bytes(storage, db, owner_id=uid, task_id=tid, file_name="ghost.txt")
    assert ei.value.code is ErrorCode.FILE_NOT_FOUND
    assert ei.value.http_status == 404


async def test_read_input_bytes_cross_direction_name_collision(pg, app_engine, domain, storage):
    """跨方向同名（task_files 无 (task_id, file_name) 唯一约束）：committed 输入
    a.txt 与 registered 产物 a.txt 并存 → 只认 input 行返回输入内容，不抛
    MultipleResultsFound、不落产物路径派生。"""
    uid, tid = await _uploading_task(pg, domain)
    input_fid = await seed_input_file(
        pg, uid, tid, size_bytes=5, state="committed", file_name="a.txt"
    )
    await seed_input_file(
        pg, uid, tid, size_bytes=9, direction="output", state="registered", file_name="a.txt"
    )
    async with pg.engine.connect() as conn:  # seed 不写盘：按行内 storage_key 补物理文件
        key = (
            await conn.execute(
                text("SELECT storage_key FROM task_files WHERE id = :f"),
                {"f": str(input_fid)},
            )
        ).scalar_one()
    storage.input_path(tid, str(key).split("/")[2]).write_bytes(b"INPUT")
    async with owner_tx(app_engine, uid) as db:
        got = await read_input_bytes(storage, db, owner_id=uid, task_id=tid, file_name="a.txt")
    assert got == b"INPUT"


async def test_read_input_bytes_tombstoned_input_with_output_same_name_404(
    pg, app_engine, domain, storage
):
    """输入墓碑 + 产物同名：input 面 + 存活口径（与 list_input_meta 对齐）→
    FILE_NOT_FOUND 404，不把产物行送进 input 路径派生。"""
    uid, tid = await _uploading_task(pg, domain)
    await seed_input_file(pg, uid, tid, size_bytes=5, state="deleted", file_name="a.txt")
    await seed_input_file(
        pg, uid, tid, size_bytes=9, direction="output", state="registered", file_name="a.txt"
    )
    async with owner_tx(app_engine, uid) as db:
        with pytest.raises(AgentCraftError) as ei:
            await read_input_bytes(storage, db, owner_id=uid, task_id=tid, file_name="a.txt")
    assert ei.value.code is ErrorCode.FILE_NOT_FOUND
    assert ei.value.http_status == 404


async def test_list_input_meta_for_callback(pg, app_engine, domain, storage, delete_root):
    """T7 元数据面：仅存活输入（墓碑/产物不可见），meta 键集钉死；非法 task_id
    段 400。"""
    uid, tid = await _uploading_task(pg, domain)
    async with owner_tx(app_engine, uid) as db:
        out = await upload_files(
            db, storage, owner_id=uid, task_id=tid, uploads=[("m1.txt", b"M1"), ("m2.txt", b"M22")]
        )
        await delete_file(db, owner_id=uid, task_id=tid, file_id=out["files"][0]["id"])
    await seed_input_file(
        pg, uid, tid, size_bytes=5, direction="output", state="registered", file_name="o.bin"
    )
    async with owner_tx(app_engine, uid) as db:
        meta = await list_input_meta(db, task_id=tid)
    assert [m["file_name"] for m in meta] == ["m2.txt"]
    assert set(meta[0]) == {"id", "file_name", "sha256", "size_bytes"}
    assert meta[0]["size_bytes"] == 3
    assert meta[0]["sha256"] == hashlib.sha256(b"M22").hexdigest()
    with pytest.raises(HTTPException) as ei:
        async with owner_tx(app_engine, uid) as db:
            await list_input_meta(db, task_id="not-a-uuid")
    assert ei.value.status_code == 400
