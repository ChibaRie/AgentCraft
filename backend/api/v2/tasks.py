"""V2 任务域路由（Phase 6 T8a）：CRUD/files/artifacts/quota + 幂等 + 限流。

契约出处：Sup §1.2/§4/§7、Phase 6 计划 D12/D13/D14/D19
（docs/superpowers/plans/2026-09-14-v2-phase-6-task-domain.md）。messages/events/
SSE 归 T8b 追加本模块。模块纪律：

- **事务收口**：服务函数不 begin 不 commit——本模块一律 owner_session 单事务
  （set_current_owner GUC 与业务写同事务，RLS 生效前提）；
- **幂等（D13）**：写端点独立会话 ``begin``（提交式自持事务，过期行清理随其
  COMMIT 落库）→ 命中原样重放（无论当前任务状态）→ 未命中进 owner_session
  业务事务，``store`` 与业务写**同事务**收口；route = 具体请求路径（含资源
  ID，Sup §7 不做模板归并）；request_hash 见各端点（multipart 只哈希元数据，
  契约 C2 从严）；
- **限流（D12）**：enforce 自管事务独立提交，在任何业务之前以 app 裸会话调用
  （reports.py 同型依赖）——task_create 30/天/用户、upload 60/h/用户；
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

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from backend.api.v2.task_schemas import TaskCommitRequest, TaskCreateRequest
from backend.v2 import idempotency, task_file_service, task_service
from backend.v2.idempotency import require_key_header, subject_user
from backend.v2.provider_service import ResolvedProvider, resolve_task_provider
from backend.v2.rate_limit import enforce, hmac_subject
from backend.v2.runtime import V2Runtime, get_v2_runtime, owner_session
from backend.v2.session_service import V2AuthContext, get_v2_auth
from backend.v2.task_artifacts import download_headers, list_artifacts, resolve_download
from backend.v2.task_views import get_task_quota_view

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
