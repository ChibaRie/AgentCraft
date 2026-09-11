import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowLeft, CheckCircle, Plus } from "@phosphor-icons/react";
import { useAuth } from "../auth/AuthContext.jsx";
import { V2ApiError, newIdempotencyKey, requestV2 } from "../api/v2/client.js";
import { V2_PROVIDERS } from "../api/v2/routes.js";
import { useRetryAfter } from "../hooks/useRetryAfter.js";

const FALLBACK_MESSAGE = "操作失败，请稍后重试";
const REVOKED_MESSAGE = "Provider Key 不可用或已失效，请轮换 Key";
const TEST_RATE_LIMIT_TEMPLATE = "测试次数已达上限（每小时 10 次），{n}s 后可重试";
const CATALOG_HINT = "。目录可能已更新，请刷新页面后重试";

// 400 目录类错误族（契约 F2 §3 #4）：内联后端文案 + 提示刷新目录
const CATALOG_GATE_CODES = new Set(["VALIDATION_ERROR", "CATALOG_ITEM_DISABLED", "MODEL_NOT_ALLOWED"]);

function describeError(cause) {
  return cause instanceof V2ApiError ? cause.message : FALLBACK_MESSAGE;
}

/**
 * 测试连通性结果行（E9 三态 + 裸错误）。ok:false 是 200 数据而非异常；
 * KEY_VERSION_REVOKED 用固定引导文案并联动轮换表单（见 handleTest）。
 */
function TestResultLine({ testState }) {
  if (!testState || testState.kind === "idle" || testState.kind === "loading") {
    return null;
  }
  if (testState.kind === "ok") {
    return (
      <p className="provider-test-result is-ok">
        <CheckCircle size={12} weight="fill" aria-hidden="true" />
        {`正常 · ${testState.latencyMs}ms · 可见 ${testState.modelsVisible} 个模型`}
      </p>
    );
  }
  if (testState.kind === "fail") {
    return <p className="provider-test-result is-fail">{`连接失败（${testState.latencyMs}ms）`}</p>;
  }
  if (testState.kind === "revoked") {
    return <p className="provider-test-result is-fail">{REVOKED_MESSAGE}</p>;
  }
  return <p className="provider-test-result is-fail">{testState.message}</p>;
}

/**
 * 添加表单（目录化）：目录下拉 → 模型下拉（唯一数据源 = 所选目录白名单，
 * 原生 select 禁手输）→ api_key（password + 显示切换）→ 设为默认开关。
 * 契约：无 base_url/protocol 字段（出现即 400，前端根本不渲染）。
 */
function AddProviderForm({ catalog, isSubmitting, error, onSubmit, onCancel }) {
  const [catalogId, setCatalogId] = useState("");
  const [modelId, setModelId] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [isDefault, setIsDefault] = useState(false);
  const [showKey, setShowKey] = useState(false);

  const selectedCatalog = catalog.find((item) => item.id === catalogId) ?? null;
  const models = selectedCatalog?.models ?? [];

  function handleCatalogChange(event) {
    setCatalogId(event.target.value);
    setModelId(""); // 目录切换重置模型选择（旧白名单对新目录无意义）
  }

  function handleSubmit(event) {
    event.preventDefault();
    onSubmit({ catalogId, modelId, apiKey, isDefault });
  }

  return (
    <form className="provider-form" onSubmit={handleSubmit}>
      <h2 className="provider-form-title">添加 Provider</h2>
      <div className="field">
        <label className="field-label" htmlFor="provider-catalog">
          目录条目
        </label>
        <select
          id="provider-catalog"
          className="field-input"
          value={catalogId}
          required
          onChange={handleCatalogChange}
        >
          <option value="" disabled>
            请选择目录条目
          </option>
          {catalog.map((item) => (
            <option key={item.id} value={item.id}>
              {item.display_name}
            </option>
          ))}
        </select>
      </div>
      <div className="field">
        <label className="field-label" htmlFor="provider-model">
          模型（目录白名单）
        </label>
        <select
          id="provider-model"
          className="field-input"
          value={modelId}
          required
          disabled={!selectedCatalog}
          onChange={(event) => setModelId(event.target.value)}
        >
          <option value="" disabled>
            请选择模型
          </option>
          {models.map((model) => (
            <option key={model} value={model}>
              {model}
            </option>
          ))}
        </select>
        <p className="field-note">模型列表来自平台目录白名单，不可手动输入。</p>
      </div>
      <div className="field">
        <label className="field-label" htmlFor="provider-api-key">
          API Key
        </label>
        <div className="provider-key-input">
          <input
            id="provider-api-key"
            className="field-input"
            type={showKey ? "text" : "password"}
            value={apiKey}
            minLength={8}
            maxLength={4096}
            autoComplete="off"
            required
            onChange={(event) => setApiKey(event.target.value)}
          />
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => setShowKey((v) => !v)}>
            {showKey ? "隐藏" : "显示"}
          </button>
        </div>
      </div>
      <label className="provider-default-toggle">
        <input
          type="checkbox"
          checked={isDefault}
          onChange={(event) => setIsDefault(event.target.checked)}
        />
        设为我的默认（建任务时默认选中）
      </label>
      {error && (
        <div className="form-alert" role="alert">
          {error.message}
          {error.showCatalogHint ? CATALOG_HINT : null}
        </div>
      )}
      <div className="task-create-actions">
        <button type="button" className="btn btn-ghost" onClick={onCancel}>
          取消
        </button>
        <button type="submit" className="btn btn-primary" disabled={isSubmitting}>
          {isSubmitting ? "保存中…" : "保存"}
        </button>
      </div>
    </form>
  );
}

/**
 * 轮换行内表单：新 api_key（留空则不修改）+ 设为默认开关（可独立勾选）。
 * body 只含非空字段由页面级 handleRotateSubmit 组装（缺席语义钉死在测试）。
 * autoFocus：连通性测试 400 KEY_VERSION_REVOKED 时引导聚焦新 Key 输入。
 */
function RotationForm({ isSubmitting, error, onSubmit, onCancel }) {
  const [apiKey, setApiKey] = useState("");
  const [isDefault, setIsDefault] = useState(false);
  const [showKey, setShowKey] = useState(false);

  function handleSubmit(event) {
    event.preventDefault();
    onSubmit({ apiKey, isDefault });
  }

  return (
    <form className="provider-rotate-form" onSubmit={handleSubmit}>
      <div className="field">
        <label className="field-label" htmlFor="provider-rotate-key">
          新 API Key
        </label>
        <div className="provider-key-input">
          <input
            id="provider-rotate-key"
            className="field-input"
            type={showKey ? "text" : "password"}
            value={apiKey}
            minLength={8}
            maxLength={4096}
            autoComplete="off"
            placeholder="留空则不修改"
            autoFocus
            onChange={(event) => setApiKey(event.target.value)}
          />
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => setShowKey((v) => !v)}>
            {showKey ? "隐藏" : "显示"}
          </button>
        </div>
        <p className="field-note">留空则不修改 Key；勾选「设为默认」可独立切换默认位。</p>
      </div>
      <label className="provider-default-toggle">
        <input
          type="checkbox"
          checked={isDefault}
          onChange={(event) => setIsDefault(event.target.checked)}
        />
        设为默认
      </label>
      {error && (
        <div className="form-alert" role="alert">
          {error}
        </div>
      )}
      <div className="provider-rotate-actions">
        <button type="button" className="btn btn-ghost" onClick={onCancel}>
          取消
        </button>
        <button type="submit" className="btn btn-primary" disabled={isSubmitting}>
          {isSubmitting ? "保存中…" : "保存更改"}
        </button>
      </div>
    </form>
  );
}

/**
 * Provider 卡片：目录名 + 模型徽标 + Key 掩码/版本 + 默认徽标 + 操作行
 * （测试连通性 / 更换 Key / 设为默认（非默认时）/ 删除）。
 */
function ProviderCard({
  provider,
  testState,
  isRateLimited,
  retryAfter,
  isRotateOpen,
  rotateError,
  isRotating,
  isDefaulting,
  cardAlert,
  onTest,
  onToggleRotate,
  onRotateSubmit,
  onSetDefault,
  onDelete,
}) {
  const isTesting = testState?.kind === "loading";

  return (
    <li className="provider-card provider-card-v2">
      <div className="provider-card-row">
        <div className="provider-card-main">
          <div className="provider-card-title">
            <strong>{provider.catalog_display_name}</strong>
            <span className="provider-model-badge">{provider.model_id}</span>
            {provider.is_default && <span className="badge-default">默认</span>}
          </div>
          <p className="provider-v2-meta">
            <span className="provider-key-mask">{`••••${provider.key_last4}`}</span>
            <span className="provider-key-version">{`v${provider.key_version}`}</span>
          </p>
          <TestResultLine testState={testState} />
          {isRateLimited && (
            <p className="provider-test-result is-limited" role="status">
              {TEST_RATE_LIMIT_TEMPLATE.replace("{n}", String(retryAfter))}
            </p>
          )}
          {cardAlert && (
            <div className="form-alert" role="alert">
              {cardAlert}
            </div>
          )}
        </div>
        <div className="provider-card-actions">
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            disabled={isTesting || isRateLimited}
            onClick={() => onTest(provider)}
          >
            {isTesting ? (
              <>
                <span className="provider-test-spinner" aria-hidden="true" /> 测试中…
              </>
            ) : (
              "测试连通性"
            )}
          </button>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => onToggleRotate(provider)}
          >
            更换 Key
          </button>
          {!provider.is_default && (
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              disabled={isDefaulting}
              onClick={() => onSetDefault(provider)}
            >
              设为默认
            </button>
          )}
          <button
            type="button"
            className="btn btn-ghost btn-sm provider-delete"
            onClick={() => onDelete(provider)}
          >
            删除
          </button>
        </div>
      </div>
      {isRotateOpen && (
        <RotationForm
          isSubmitting={isRotating}
          error={rotateError}
          onSubmit={(payload) => onRotateSubmit(provider, payload)}
          onCancel={() => onToggleRotate(provider)}
        />
      )}
    </li>
  );
}

/**
 * Provider 设置页（BYOK 目录化，契约 = F2 §3；FE-T8 整体重写 V1 面）。
 *
 * 后端契约（只读参考，backend/api/v2/providers.py）：
 * - GET /catalog → {data:[{id,display_name,allowed_host,models}]}（仅 enabled）；
 * - GET /providers → {data:[ProviderOut]}（仅 active，created_at asc，无 total 信封）；
 * - POST（幂等键；400 CATALOG_ITEM_DISABLED/MODEL_NOT_ALLOWED、409 PROVIDER_DUPLICATE）；
 * - PUT /{id}（幂等键；api_key/is_default 缺席=不变——显式 null 400、布尔=覆盖）；
 * - DELETE /{id}（幂等键，软撤；列表只回 active 行）；POST /{id}/test（无幂等键
 *   ——D10 → {ok, latency_ms, models_visible}；400 KEY_VERSION_REVOKED；429 10/h）。
 *
 * 列表顺序唯一来源 = 后端 created_at asc：创建后整体 refetch（本地插入会跳位），
 * 轮换/设默认就地更新（默认互斥在本地镜像清位，防陈旧徽标）。
 * 幂等键策略：每次用户触发的提交现场生成（失败后重试即新键，规格 #8），
 * in-flight 闸门防双击重入。
 */
export default function ProviderSettingsPage() {
  const { v2User } = useAuth();
  const [catalog, setCatalog] = useState([]);
  const [providers, setProviders] = useState(null); // null = 装载中
  const [loadError, setLoadError] = useState(null);

  const [isAddOpen, setIsAddOpen] = useState(false);
  const [isCreating, setIsCreating] = useState(false);
  const [addError, setAddError] = useState(null); // {message, showCatalogHint}

  const [testStates, setTestStates] = useState({}); // id -> {kind, latencyMs?, modelsVisible?, message?}
  const [rateLimitedTestId, setRateLimitedTestId] = useState(null);
  const { retryAfter, start: startRetryAfter } = useRetryAfter();

  const [rotatingId, setRotatingId] = useState(null);
  const [isRotating, setIsRotating] = useState(false);
  const [rotateError, setRotateError] = useState(null);

  const [defaultingId, setDefaultingId] = useState(null);
  const [cardAlerts, setCardAlerts] = useState({}); // id -> message

  const [deleteTarget, setDeleteTarget] = useState(null);
  const [isDeleting, setIsDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState(null);

  // 数据装载：catalog + providers 并行（Promise.all，规格 #1）
  const load = useCallback(async () => {
    try {
      const [catalogResult, providersResult] = await Promise.all([
        requestV2(`${V2_PROVIDERS}/catalog`),
        requestV2(V2_PROVIDERS),
      ]);
      setCatalog(catalogResult.data ?? []);
      setProviders(providersResult.data ?? []);
      setLoadError(null);
    } catch (cause) {
      setLoadError(describeError(cause));
    }
  }, []);

  useEffect(() => {
    if (v2User) {
      load();
    }
  }, [v2User, load]);

  // 整体 refetch（仅 providers）：列表顺序唯一来源（created_at asc）
  const refreshProviders = useCallback(async () => {
    const result = await requestV2(V2_PROVIDERS);
    setProviders(result.data ?? []);
  }, []);

  /** 就地更新（PUT 200）：默认互斥镜像——后端已清其他行默认位，本地同步防陈旧徽标 */
  function applyInPlace(updated) {
    if (!updated || typeof updated !== "object" || !updated.id) {
      return;
    }
    setProviders((current) =>
      (current ?? []).map((row) => {
        if (row.id === updated.id) {
          return updated;
        }
        if (updated.is_default && row.is_default) {
          return { ...row, is_default: false };
        }
        return row;
      })
    );
  }

  async function handleCreate(payload) {
    if (isCreating) {
      return;
    }
    setIsCreating(true);
    setAddError(null);
    try {
      const body = {
        catalog_id: payload.catalogId,
        model_id: payload.modelId,
        api_key: payload.apiKey,
      };
      if (payload.isDefault) {
        body.is_default = true; // 缺席=不设默认；绝不显式发 false
      }
      await requestV2(V2_PROVIDERS, {
        method: "POST",
        body,
        idempotencyKey: newIdempotencyKey(), // 每次提交新键：失败后重试即新键
      });
      setIsAddOpen(false);
      await refreshProviders();
    } catch (cause) {
      setAddError({
        message: describeError(cause),
        showCatalogHint:
          cause instanceof V2ApiError &&
          cause.status === 400 &&
          CATALOG_GATE_CODES.has(cause.code),
      });
    } finally {
      setIsCreating(false);
    }
  }

  async function handleTest(provider) {
    if (retryAfter > 0 || testStates[provider.id]?.kind === "loading") {
      return;
    }
    setRateLimitedTestId(null);
    setTestStates((current) => ({ ...current, [provider.id]: { kind: "loading" } }));
    try {
      // D10：连通性测试不幂等——无幂等键（幂等义务表亦不含 /test）
      const result = await requestV2(`${V2_PROVIDERS}/${provider.id}/test`, {
        method: "POST",
      });
      const data = result.data ?? {};
      setTestStates((current) => ({
        ...current,
        [provider.id]: data.ok
          ? { kind: "ok", latencyMs: data.latency_ms, modelsVisible: data.models_visible }
          : { kind: "fail", latencyMs: data.latency_ms },
      }));
    } catch (cause) {
      if (cause instanceof V2ApiError && cause.status === 429) {
        startRetryAfter(cause.retryAfter); // 全局限额倒计时（按用户计）：当前卡按钮禁用；其它卡在 handleTest 入口静默早退
        setRateLimitedTestId(provider.id);
        setTestStates((current) => ({ ...current, [provider.id]: { kind: "idle" } }));
        return;
      }
      if (cause instanceof V2ApiError && cause.code === "KEY_VERSION_REVOKED") {
        setTestStates((current) => ({ ...current, [provider.id]: { kind: "revoked" } }));
        setRotatingId(provider.id); // 引导聚焦轮换表单（RotationForm autoFocus 新 Key 输入）
        return;
      }
      setTestStates((current) => ({
        ...current,
        [provider.id]: { kind: "error", message: describeError(cause) },
      }));
    }
  }

  function handleToggleRotate(provider) {
    setRotateError(null);
    setRotatingId((current) => (current === provider.id ? null : provider.id));
  }

  async function handleRotateSubmit(provider, payload) {
    if (isRotating) {
      return;
    }
    setIsRotating(true);
    setRotateError(null);
    try {
      const body = {};
      if (payload.apiKey) {
        body.api_key = payload.apiKey; // 留空 → 整键缺席（=不变；显式 null 会 400）
      }
      if (payload.isDefault) {
        body.is_default = true; // 未勾选 → 键整体缺席（显式 false 会静默清默认位）
      }
      const result = await requestV2(`${V2_PROVIDERS}/${provider.id}`, {
        method: "PUT",
        body,
        idempotencyKey: newIdempotencyKey(),
      });
      applyInPlace(result.data);
      setRotatingId(null);
    } catch (cause) {
      setRotateError(describeError(cause));
    } finally {
      setIsRotating(false);
    }
  }

  async function handleSetDefault(provider) {
    if (defaultingId) {
      return;
    }
    setDefaultingId(provider.id);
    setCardAlerts((current) => ({ ...current, [provider.id]: undefined }));
    try {
      const result = await requestV2(`${V2_PROVIDERS}/${provider.id}`, {
        method: "PUT",
        body: { is_default: true },
        idempotencyKey: newIdempotencyKey(),
      });
      applyInPlace(result.data);
    } catch (cause) {
      setCardAlerts((current) => ({ ...current, [provider.id]: describeError(cause) }));
    } finally {
      setDefaultingId(null);
    }
  }

  async function handleConfirmDelete() {
    if (isDeleting || !deleteTarget) {
      return;
    }
    const targetId = deleteTarget.id;
    setIsDeleting(true);
    setDeleteError(null);
    try {
      await requestV2(`${V2_PROVIDERS}/${targetId}`, {
        method: "DELETE",
        idempotencyKey: newIdempotencyKey(),
      });
      setDeleteTarget(null);
      setProviders((current) => (current ?? []).filter((row) => row.id !== targetId));
    } catch (cause) {
      if (cause instanceof V2ApiError && cause.status === 404) {
        // 竞态：行已被其他途径撤销——静默刷新收敛，不展示错误
        setDeleteTarget(null);
        try {
          await refreshProviders();
        } catch {
          // 静默：刷新失败不打扰用户（下次进入自愈）
        }
      } else {
        setDeleteError(describeError(cause));
      }
    } finally {
      setIsDeleting(false);
    }
  }

  // V2 gating：Provider 管理全走 cookie 会话（V2 面），V1-only 用户不暴露必 401 的操作面
  if (!v2User) {
    return (
      <main className="page">
        <div className="provider-settings">
          <header className="page-header">
            <h1 className="page-title">Provider 设置</h1>
          </header>
          <div className="provider-empty">
            <p>Provider 管理需要新版账户会话。</p>
            <p className="provider-empty-note">请以新版账户登录后再来配置模型服务。</p>
          </div>
        </div>
      </main>
    );
  }

  return (
    <main className="page">
      <div className="provider-settings">
        <header className="page-header">
          <h1 className="page-title">Provider 设置</h1>
          <p className="page-sub">
            从平台目录选择模型服务并绑定你自己的 Key（BYOK）。Key 加密存储、仅写入，
            可随时测试连通性；配置变更不影响进行中的任务。
          </p>
        </header>

        {loadError && (
          <div className="form-alert" role="alert">
            {loadError}
          </div>
        )}

        <div className="provider-toolbar">
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => setIsAddOpen((open) => !open)}
          >
            <Plus size={14} aria-hidden="true" /> 添加 Provider
          </button>
          <Link to="/profile" className="btn btn-ghost">
            <ArrowLeft size={14} aria-hidden="true" /> 返回个人中心
          </Link>
        </div>

        {isAddOpen && (
          <AddProviderForm
            catalog={catalog}
            isSubmitting={isCreating}
            error={addError}
            onSubmit={handleCreate}
            onCancel={() => {
              setAddError(null); // 关闭即清残留错误，重开时表单干净
              setIsAddOpen(false);
            }}
          />
        )}

        {providers === null && !loadError && (
          <p className="provider-empty-note">加载中…</p>
        )}

        {providers !== null && providers.length === 0 && (
          <div className="provider-empty">
            <p>还没有自定义 Provider。</p>
            <p className="provider-empty-note">
              未配置时任务使用部署者的系统默认；从上方目录添加一个你自己的模型服务即可。
            </p>
          </div>
        )}

        {providers !== null && providers.length > 0 && (
          <ul className="provider-list">
            {providers.map((provider) => (
              <ProviderCard
                key={provider.id}
                provider={provider}
                testState={testStates[provider.id] ?? null}
                isRateLimited={rateLimitedTestId === provider.id && retryAfter > 0}
                retryAfter={retryAfter}
                isRotateOpen={rotatingId === provider.id}
                rotateError={rotatingId === provider.id ? rotateError : null}
                isRotating={isRotating}
                isDefaulting={defaultingId === provider.id}
                cardAlert={cardAlerts[provider.id] ?? null}
                onTest={handleTest}
                onToggleRotate={handleToggleRotate}
                onRotateSubmit={handleRotateSubmit}
                onSetDefault={handleSetDefault}
                onDelete={setDeleteTarget}
              />
            ))}
          </ul>
        )}

        {deleteTarget && (
          // 弹层壳复用 global.css .modal-overlay/.modal 原语（DangerZone 同源）
          <div
            className="modal-overlay"
            onPointerDown={(event) => {
              if (event.target === event.currentTarget && !isDeleting) {
                setDeleteTarget(null);
              }
            }}
          >
            <div
              className="modal v2-danger-dialog"
              role="dialog"
              aria-modal="true"
              aria-labelledby="provider-delete-title"
            >
              <h3 className="v2-danger-dialog-title" id="provider-delete-title">
                撤销 Provider
              </h3>
              <p className="v2-danger-dialog-sub">
                即将撤销{" "}
                <strong>
                  {deleteTarget.catalog_display_name} · {deleteTarget.model_id}
                </strong>
                。
              </p>
              <p className="v2-danger-dialog-sub">
                撤销后该 Provider 立即失效，关联的未开始任务将被终止。
              </p>
              {deleteError && (
                <div className="form-alert" role="alert">
                  {deleteError}
                </div>
              )}
              <div className="v2-danger-dialog-actions">
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={() => setDeleteTarget(null)}
                >
                  取消
                </button>
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={isDeleting}
                  onClick={handleConfirmDelete}
                >
                  {isDeleting ? "撤销中…" : "确认撤销"}
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </main>
  );
}
