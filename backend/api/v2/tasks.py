"""V2 任务域路由（Phase 6 T8a/T8b）：CRUD/files/artifacts/quota + messages/
events/SSE 补拉重连 + 幂等 + 限流。

契约出处：Sup §1.2/§1.3/§4/§7、Phase 6 计划 D11/D12/D13/D14/D19
（docs/superpowers/plans/2026-09-14-v2-phase-6-task-domain.md）。模块纪律：

- **事务收口**：服务函数不 begin 不 commit——本模块一律 owner_session 单事务
  （set_current_owner GUC 与业务写同事务，RLS 生效前提）；
- **幂等（D13）**：写端点独立会话 ``begin``（提交式自持事务，过期行清理随其
  COMMIT 落库）→ 命中原样重放（无论当前任务状态）→ 未命中进 owner_session
  业务事务，``store`` 与业务写**同事务**收口；route = 具体请求路径（含资源
  ID，Sup §7 不做模板归并）；request_hash 见各端点（multipart 只哈希元数据，
  契约 C2 从严）；
- **限流（D12）**：enforce 自管事务独立提交，以 app 裸会话调用（reports.py
  同型）——task_create 30/天/用户、upload 60/h/用户、send_message 60/h/用户、
  sse_connect 60/h/用户·任务（主体 [user, task] 组合）。**T8b 裁决**：
  send_message 限流挂接在幂等 begin **之后**（体内调用而非 Depends）——幂等
  命中重放无条件下（Sup §1.2:25）优先于限流短路，命中重放的请求不消耗新窗口；
  其余端点维持「限流先于幂等」的 T8a 依赖序（缺 key 请求不消耗窗口）；
- **SSE（Sup §1.3/D11）**：流建立序 = 限流（sse_connect）→ 认证（依赖）→
  任务存在门（404 统一 JSON，流建立前短路）→ streams.register（先于重放——
  注册与重放间隙的事件留在订阅缓冲，无补拉间隙）→ DB 重放 sequence>after →
  meta 帧（初始 watermark）→ 合并排空（重放与缓冲按 sequence 去重）→ 实时；
  持久帧 ``id: <event_sequence>``；15s 心跳注释行 ``: ping``；
  ``X-Accel-Buffering: no`` 禁代理缓冲；断连 finally unsubscribe。D11 帧映射
  与流机制在 ``task_sse.py``（每事件恰一帧、id 单调——round_failed/
  round_cancelled 的 status_changed 帧由同事务相邻 status_changed 事件承载，
  全部现行写路径均成对写入；T9 回写登记）；
- **abort/complete 状态码映射**：服务返回 ``{"task"}`` → 200；``{"task",
  "round": {"state": "cancelling"}}`` → 202（D14 running 分支），store 落库的
  status_code 与映射一致（重放返回原状态码）；
- **DELETE post-commit 物理删 task-storage**（T3 裁决：物理删归路由）——
  owner_session 提交后调 ``runtime.storage.delete_task_storage``（幂等，残余
  归 sweep_terminal_cleanup 兜底）；
- **provider 快照路由层单点构造**（T3 审查顺延 M1 收口）：D14 四键全部取自
  ``ResolvedProvider`` 同源字段（``_snapshot_from_resolved``），provider_id
  一致性由构造保证——服务层 ``_require_provider_snapshot`` 三键校验之外无
  第二来源可漂移。
"""

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from backend.api.v2.task_schemas import (
    TaskCommitRequest,
    TaskCreateRequest,
    TaskSendMessageRequest,
)
from backend.api.v2.task_sse import _event_frame, _stream_registry, _task_event_stream
from backend.v2 import idempotency, task_file_service, task_service, task_views
from backend.v2.idempotency import require_key_header, subject_user
from backend.v2.provider_service import ResolvedProvider, resolve_task_provider
from backend.v2.rate_limit import enforce, hmac_subject
from backend.v2.runtime import V2Runtime, get_v2_runtime, owner_session
from backend.v2.session_service import V2AuthContext, get_v2_auth
from backend.v2.task_artifacts import download_headers, list_artifacts, resolve_download
from backend.v2.task_views import _parse_id, _require_live_task, get_task_quota_view

router = APIRouter()

# 列表分页默认（服务层校验 1≤size≤100；Sup §7 列表信封附 total/page/size）
_DEFAULT_PAGE = 1
_DEFAULT_PAGE_SIZE = 20


# ---------------------------------------------------------------------------
# 共享路由助手（须定义在路由装饰器之前——默认参数装饰期求值）
# ---------------------------------------------------------------------------


async def _enforce_task_create_limit(
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> None:
    """任务创建限流（D12 task_create 30/天/用户；enforce 先于一切业务，app 裸会话）。"""
    async with runtime.app_factory() as db:
        subjects = [hmac_subject("user", str(user_ctx.user.id))]
        await enforce(db, scope="task_create", subjects=subjects)


async def _enforce_upload_limit(
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> None:
    """文件上传限流（D12 upload 60/h/用户；enforce 先于 multipart 解析与业务）。"""
    async with runtime.app_factory() as db:
        subjects = [hmac_subject("user", str(user_ctx.user.id))]
        await enforce(db, scope="upload", subjects=subjects)


def _snapshot_from_resolved(rp: ResolvedProvider) -> dict:
    """D14 快照四键单点构造（与 ResolvedProvider 一一对应；见模块 docstring）。"""
    return {
        "provider_id": rp.provider_id,
        "provider_catalog_id": rp.catalog_id,
        "provider_model_id": rp.model_id,
        "provider_key_version": rp.key_version,
    }


async def _idem_begin(
    runtime: V2Runtime, *, request: Request, user_id: str, key: str, req_hash: str
) -> JSONResponse | None:
    """独立会话幂等查询（提交式自持事务）：命中 → 原样重放载荷；未命中/过期 → None。

    重放纪律（D13/Sup §1.2:25）：幂等命中先于一切状态校验；response_json 仅含
    JSON body（不捕获 Set-Cookie）。
    """
    async with runtime.app_factory() as db:
        hit = await idempotency.begin(
            db,
            subject_hash=subject_user(user_id),
            route=request.url.path,
            key=key,
            req_hash=req_hash,
        )
    if hit is None:
        return None
    return JSONResponse(status_code=hit["status_code"], content=hit["response_json"])


async def _idem_store(
    db,
    *,
    request: Request,
    user_id: str,
    key: str,
    req_hash: str,
    status_code: int,
    payload: dict,
) -> None:
    """幂等记录落库（不 commit——调用方 owner_session 事务块收口，与业务写同事务）。"""
    await idempotency.store(
        db,
        subject_hash=subject_user(user_id),
        route=request.url.path,
        key=key,
        req_hash=req_hash,
        status_code=status_code,
        response_json=payload,
    )


# ---------------------------------------------------------------------------
# 任务 CRUD
# ---------------------------------------------------------------------------


@router.post("/tasks")
async def create_task(
    payload: TaskCreateRequest,
    request: Request,
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    idem_key: str = Depends(require_key_header),
    runtime: V2Runtime = Depends(get_v2_runtime),
    _limit: None = Depends(_enforce_task_create_limit),
) -> JSONResponse:
    """创建任务（D14 create 形状，201）：provider 经 resolve_task_provider
    （缺省回退/错误码沿现行语义）→ 快照单点构造 → create_task。"""
    uid = str(user_ctx.user.id)
    body = payload.model_dump(exclude_unset=True)
    req_hash = idempotency.request_hash(body)
    replay = await _idem_begin(
        runtime, request=request, user_id=uid, key=idem_key, req_hash=req_hash
    )
    if replay is not None:
        return replay
    async with owner_session(runtime, uid) as db:
        rp = await resolve_task_provider(db, user_id=uid, provider_id=payload.provider_id)
        result = await task_service.create_task(
            db,
            owner_id=uid,
            expert_revision_id=payload.expert_revision_id,
            provider_id=rp.provider_id,
            initial_message=payload.initial_message,
            provider_snapshot=_snapshot_from_resolved(rp),
        )
        await _idem_store(
            db,
            request=request,
            user_id=uid,
            key=idem_key,
            req_hash=req_hash,
            status_code=201,
            payload={"data": result},
        )
    return JSONResponse(status_code=201, content={"data": result})


@router.get("/tasks")
async def list_tasks(
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    page: int = _DEFAULT_PAGE,
    size: int = _DEFAULT_PAGE_SIZE,
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """owner 任务列表（分页 created_at DESC 稳定序；deleted 不可见）。"""
    uid = str(user_ctx.user.id)
    async with owner_session(runtime, uid) as db:
        result = await task_service.list_tasks(db, owner_id=uid, page=page, size=size)
    return JSONResponse(status_code=200, content={"data": result})


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: str,
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """任务快照（D14 视图；缺失/他人/已删除统一 404）。"""
    uid = str(user_ctx.user.id)
    async with owner_session(runtime, uid) as db:
        result = await task_service.get_task_view(db, owner_id=uid, task_id=task_id)
    return JSONResponse(status_code=200, content={"data": {"task": result}})


@router.delete("/tasks/{task_id}")
async def delete_task(
    task_id: str,
    request: Request,
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    idem_key: str = Depends(require_key_header),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """删除任务（D14 delete 形状）：重放原响应；幂等记录过期后统一 404。

    物理删 task-storage 在 owner_session 提交（账面先行）之后执行（T3 裁决：
    delete_task_storage 归路由 post-commit；幂等，无 I/O 异常面）。"""
    uid = str(user_ctx.user.id)
    req_hash = idempotency.request_hash(None)  # 无请求体
    replay = await _idem_begin(
        runtime, request=request, user_id=uid, key=idem_key, req_hash=req_hash
    )
    if replay is not None:
        return replay
    async with owner_session(runtime, uid) as db:
        result = await task_service.delete_task(db, owner_id=uid, task_id=task_id)
        await _idem_store(
            db,
            request=request,
            user_id=uid,
            key=idem_key,
            req_hash=req_hash,
            status_code=200,
            payload={"data": result},
        )
    runtime.storage.delete_task_storage(result["task"]["id"])
    return JSONResponse(status_code=200, content={"data": result})


@router.post("/tasks/{task_id}/input/commit")
async def commit_input(
    task_id: str,
    payload: TaskCommitRequest,
    request: Request,
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    idem_key: str = Depends(require_key_header),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """输入冻结（D14 commit 形状，200）：manifest 规范化哈希 + staged→committed
    + task_root + 初始轮 pending + uploading→queued。"""
    uid = str(user_ctx.user.id)
    body = payload.model_dump(exclude_unset=True)
    req_hash = idempotency.request_hash(body)
    replay = await _idem_begin(
        runtime, request=request, user_id=uid, key=idem_key, req_hash=req_hash
    )
    if replay is not None:
        return replay
    async with owner_session(runtime, uid) as db:
        result = await task_service.commit_input(
            db, owner_id=uid, task_id=task_id, manifest=payload.manifest
        )
        await _idem_store(
            db,
            request=request,
            user_id=uid,
            key=idem_key,
            req_hash=req_hash,
            status_code=200,
            payload={"data": result},
        )
    return JSONResponse(status_code=200, content={"data": result})


async def _terminal_intent(
    terminal_fn,
    task_id: str,
    request: Request,
    user_ctx: V2AuthContext,
    idem_key: str,
    runtime: V2Runtime,
) -> JSONResponse:
    """abort/complete 共用路由体（D19 意图位；映射：服务结果含 ``round`` 键
    → 202，否则 200；store 的 status_code 与映射一致，重放同态返回）。"""
    uid = str(user_ctx.user.id)
    req_hash = idempotency.request_hash(None)
    replay = await _idem_begin(
        runtime, request=request, user_id=uid, key=idem_key, req_hash=req_hash
    )
    if replay is not None:
        return replay
    async with owner_session(runtime, uid) as db:
        result = await terminal_fn(db, owner_id=uid, task_id=task_id)
        status_code = 202 if "round" in result else 200
        await _idem_store(
            db,
            request=request,
            user_id=uid,
            key=idem_key,
            req_hash=req_hash,
            status_code=status_code,
            payload={"data": result},
        )
    return JSONResponse(status_code=status_code, content={"data": result})


@router.post("/tasks/{task_id}/abort")
async def abort_task(
    task_id: str,
    request: Request,
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    idem_key: str = Depends(require_key_header),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """用户取消（Sup §1.2:29）：queued/ready 直翻 200；running 写意图位 +
    活跃轮 cancelling → 202 ``{data: {task, round: {state: "cancelling"}}}``。"""
    return await _terminal_intent(
        task_service.abort_task, task_id, request, user_ctx, idem_key, runtime
    )


@router.post("/tasks/{task_id}/complete")
async def complete_task(
    task_id: str,
    request: Request,
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    idem_key: str = Depends(require_key_header),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """优雅收尾（Sup §1.2:30）：ready/queued 直翻 200；running 意图位分支 → 202。"""
    return await _terminal_intent(
        task_service.complete_task, task_id, request, user_ctx, idem_key, runtime
    )


# ---------------------------------------------------------------------------
# 任务文件（输入面）
# ---------------------------------------------------------------------------


@router.post("/tasks/{task_id}/files")
async def upload_task_files(
    task_id: str,
    request: Request,
    files: list[UploadFile] = File(...),
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    idem_key: str = Depends(require_key_header),
    runtime: V2Runtime = Depends(get_v2_runtime),
    _limit: None = Depends(_enforce_upload_limit),
) -> JSONResponse:
    """批量上传输入文件（staged；200）：幂等 request_hash 只哈希元数据
    （file_name + size + 声明序——契约 C2 从严，文件体不参与哈希）。"""
    uid = str(user_ctx.user.id)
    uploads: list[tuple[str, bytes]] = []
    for f in files:
        uploads.append((f.filename or "", await f.read()))
    metadata = [{"file_name": name, "size": len(content)} for name, content in uploads]
    req_hash = idempotency.request_hash({"files": metadata})
    replay = await _idem_begin(
        runtime, request=request, user_id=uid, key=idem_key, req_hash=req_hash
    )
    if replay is not None:
        return replay
    async with owner_session(runtime, uid) as db:
        result = await task_file_service.upload_files(
            db, runtime.storage, owner_id=uid, task_id=task_id, uploads=uploads
        )
        await _idem_store(
            db,
            request=request,
            user_id=uid,
            key=idem_key,
            req_hash=req_hash,
            status_code=200,
            payload={"data": result},
        )
    return JSONResponse(status_code=200, content={"data": result})


@router.get("/tasks/{task_id}/files")
async def list_task_files(
    task_id: str,
    direction: str,
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """输入/产物文件列表（?direction=input|output；含 sha256/size/state）。"""
    uid = str(user_ctx.user.id)
    async with owner_session(runtime, uid) as db:
        items = await task_file_service.list_files(
            db, owner_id=uid, task_id=task_id, direction=direction
        )
    return JSONResponse(status_code=200, content={"data": items})


@router.delete("/tasks/{task_id}/files/{file_id}")
async def delete_task_file(
    task_id: str,
    file_id: str,
    request: Request,
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    idem_key: str = Depends(require_key_header),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """删除 staged 输入文件（D7a：uploading 限定；幂等，重放原响应）。"""
    uid = str(user_ctx.user.id)
    req_hash = idempotency.request_hash(None)
    replay = await _idem_begin(
        runtime, request=request, user_id=uid, key=idem_key, req_hash=req_hash
    )
    if replay is not None:
        return replay
    async with owner_session(runtime, uid) as db:
        result = await task_file_service.delete_file(
            db, owner_id=uid, task_id=task_id, file_id=file_id
        )
        await _idem_store(
            db,
            request=request,
            user_id=uid,
            key=idem_key,
            req_hash=req_hash,
            status_code=200,
            payload={"data": result},
        )
    return JSONResponse(status_code=200, content={"data": result})


# ---------------------------------------------------------------------------
# 产物（T7 管道消费面）
# ---------------------------------------------------------------------------


@router.get("/tasks/{task_id}/artifacts")
async def list_task_artifacts(
    task_id: str,
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """产物列表（D7g：含 produced_in_round_id；任务缺失/他人/已删除统一 404）。"""
    uid = str(user_ctx.user.id)
    async with owner_session(runtime, uid) as db:
        items = await list_artifacts(db, owner_id=uid, task_id=task_id)
    return JSONResponse(status_code=200, content={"data": items})


@router.get("/tasks/{task_id}/artifacts/{file_id}/download")
async def download_task_artifact(
    task_id: str,
    file_id: str,
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> FileResponse:
    """产物下载（resolve_download → artifacts 登记副本；attachment + nosniff 头）。"""
    uid = str(user_ctx.user.id)
    async with owner_session(runtime, uid) as db:
        path, meta = await resolve_download(
            db, runtime.storage, owner_id=uid, task_id=task_id, file_id=file_id
        )
    return FileResponse(
        path,
        media_type="application/octet-stream",
        headers=download_headers(meta["file_name"]),
    )


# ---------------------------------------------------------------------------
# 配额视图
# ---------------------------------------------------------------------------


@router.get("/quota")
async def get_owner_quota(
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """owner 四维用量与上限（get_quota_view；权威判定仍在写事务内）。"""
    uid = str(user_ctx.user.id)
    async with owner_session(runtime, uid) as db:
        result = await task_service.get_quota_view(db, owner_id=uid)
    return JSONResponse(status_code=200, content={"data": result})


@router.get("/tasks/{task_id}/quota")
async def get_task_quota(
    task_id: str,
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """任务粒度用量视图（D7g：当前用量/上限/输入冻结状态）。"""
    uid = str(user_ctx.user.id)
    async with owner_session(runtime, uid) as db:
        result = await get_task_quota_view(db, owner_id=uid, task_id=task_id)
    return JSONResponse(status_code=200, content={"data": result})


# ---------------------------------------------------------------------------
# 消息发送（T8b：幂等 → 限流 → ready 门 + 活跃轮闸）
# ---------------------------------------------------------------------------


async def _enforce_send_message_limit(runtime: V2Runtime, user_id: str) -> None:
    """消息发送限流（D12 send_message 60/h/用户；enforce 自管事务独立提交）。

    体内调用而非 Depends：挂接位置在幂等 begin **之后**（T8b 裁决，见模块
    docstring）——命中重放的请求原样返回 202 原响应且不消耗新窗口（幂等重放
    无条件下优先于限流短路，Sup §1.2:25）。"""
    async with runtime.app_factory() as db:
        await enforce(db, scope="send_message", subjects=[hmac_subject("user", user_id)])


@router.post("/tasks/{task_id}/messages")
async def send_task_message(
    task_id: str,
    payload: TaskSendMessageRequest,
    request: Request,
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    idem_key: str = Depends(require_key_header),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """发送下一条用户消息（Sup §1.2:25，202）：幂等命中重放原响应（无论当前
    任务状态）→ 限流（send_message）→ ready 且无活跃轮（活跃/排队轮 429
    TASK_ROUND_BUSY + Retry-After；one_active_round 唯一索引即并发闸门，服务
    层预检 + IntegrityError 双保险）→ 预算校验（D7d）→ message 落库 +
    message_saved → ready→queued + status_changed → round(pending) +
    round_queued → 202 {data:{message, event_sequence, round_id}}。"""
    uid = str(user_ctx.user.id)
    body = payload.model_dump(exclude_unset=True)
    req_hash = idempotency.request_hash(body)
    replay = await _idem_begin(
        runtime, request=request, user_id=uid, key=idem_key, req_hash=req_hash
    )
    if replay is not None:
        return replay
    await _enforce_send_message_limit(runtime, uid)
    async with owner_session(runtime, uid) as db:
        result = await task_service.send_message(
            db, owner_id=uid, task_id=task_id, content=payload.content
        )
        await _idem_store(
            db,
            request=request,
            user_id=uid,
            key=idem_key,
            req_hash=req_hash,
            status_code=202,
            payload={"data": result},
        )
    return JSONResponse(status_code=202, content={"data": result})


# ---------------------------------------------------------------------------
# 补拉读面（T8b：messages 全量正文 / events 事实序列 + 快照）
# ---------------------------------------------------------------------------

_DEFAULT_MESSAGES_LIMIT = 50
_MAX_MESSAGES_LIMIT = 200
_DEFAULT_EVENTS_LIMIT = 200
_MAX_EVENTS_LIMIT = 1000


@router.get("/tasks/{task_id}/messages")
async def list_task_messages_route(
    task_id: str,
    after: int = Query(0, ge=0),
    limit: int = Query(_DEFAULT_MESSAGES_LIMIT, ge=1, le=_MAX_MESSAGES_LIMIT),
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """消息补拉（Sup §1.2:26）：?after=<event_seq>&limit=（默认 50 上限 200），
    event_sequence 升序全量正文；不可用于状态恢复（事实面是 /events）。"""
    uid = str(user_ctx.user.id)
    async with owner_session(runtime, uid) as db:
        items = await task_views.list_task_messages(
            db, owner_id=uid, task_id=task_id, after=after, limit=limit
        )
    return JSONResponse(status_code=200, content={"data": items})


@router.get("/tasks/{task_id}/events")
async def list_task_events_route(
    task_id: str,
    after: int = Query(0, ge=0),
    limit: int = Query(_DEFAULT_EVENTS_LIMIT, ge=1, le=_MAX_EVENTS_LIMIT),
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """事实补拉（Sup §1.2:27）：?after=<seq>&limit=（默认 200 上限 1000）；
    after > watermark 时事件空集 + 当前快照 {status, event_sequence}（仍随附）。
    事件是状态恢复的唯一事实源；帧形状与 SSE 重放一致（D11 映射共用，
    每项外加 ``sequence`` 传输键供客户端水位推进）。"""
    uid = str(user_ctx.user.id)
    async with owner_session(runtime, uid) as db:
        data = await task_views.list_task_events(
            db, owner_id=uid, task_id=task_id, after=after, limit=limit
        )
    frames = [
        {"sequence": seq, **frame}
        for seq, event_type, payload_json in data["events"]
        if (frame := _event_frame(seq, event_type, payload_json)) is not None
    ]
    return JSONResponse(
        status_code=200,
        content={"data": {"events": frames, "snapshot": data["snapshot"]}},
    )


# ---------------------------------------------------------------------------
# SSE 实时事件流（T8b：D11 帧映射 + 合并排空 + 心跳——机制层在 task_sse.py）
# ---------------------------------------------------------------------------


async def _enforce_sse_connect_limit(
    task_id: str,
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> None:
    """SSE 连接建立限流（D12 sse_connect 60/h/用户·任务）：组合主体
    [HMAC(user), HMAC(task)]（Sup §7 重连风暴上限；任一达限即整组 429）。"""
    tid = str(_parse_id(task_id, "task_id"))
    async with runtime.app_factory() as db:
        await enforce(
            db,
            scope="sse_connect",
            subjects=[
                hmac_subject("user", str(user_ctx.user.id)),
                hmac_subject("task", tid),
            ],
        )


@router.get("/tasks/{task_id}/events/stream")
async def stream_task_events(
    task_id: str,
    after: int = Query(0, ge=0),
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    runtime: V2Runtime = Depends(get_v2_runtime),
    _limit: None = Depends(_enforce_sse_connect_limit),
) -> StreamingResponse:
    """SSE 实时事件流（Sup §1.3）：?after=<seq> 从水位起订阅。流建立前的校验
    失败（限流 429 / 任务 404）返回统一 JSON 错误信封（非 SSE）；流内事件序 =
    meta → 重放持久帧 → 实时帧。客户端断连不中断轮（detached 执行模型）。"""
    uid = str(user_ctx.user.id)
    tid = str(_parse_id(task_id, "task_id"))
    streams = _stream_registry(runtime)
    async with owner_session(runtime, uid) as db:
        await _require_live_task(db, task_id)  # 缺失/他人/已删除统一 404（流外短路）
    return StreamingResponse(
        _task_event_stream(runtime, streams, uid, tid, after),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
