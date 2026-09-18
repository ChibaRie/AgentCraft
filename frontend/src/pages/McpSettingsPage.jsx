import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowLeft, Plus } from "@phosphor-icons/react";
import { useAuth } from "../auth/AuthContext.jsx";
import { V2ApiError, newIdempotencyKey, requestV2 } from "../api/v2/client.js";
import { V2_MCP_SERVERS } from "../api/v2/routes.js";
import { useRetryAfter } from "../hooks/useRetryAfter.js";

const FALLBACK_MESSAGE = "操作失败，请稍后重试";
const DISCOVER_RATE_LIMIT_TEMPLATE = "发现次数已达上限（每小时 10 次），{n}s 后可重试";

function describeError(cause) {
  return cause instanceof V2ApiError ? cause.message : FALLBACK_MESSAGE;
}

/** 注册表单：stdio（命令+参数+env）或 http（https URL）。 */
function AddMcpServerForm({ isSubmitting, error, onSubmit, onCancel }) {
  const [name, setName] = useState("");
  const [transportKind, setTransportKind] = useState("stdio");
  const [command, setCommand] = useState("");
  const [argsText, setArgsText] = useState("");
  const [envText, setEnvText] = useState("");
  const [url, setUrl] = useState("");

  function handleSubmit(event) {
    event.preventDefault();
    const payload = { name, transportKind };
    if (transportKind === "stdio") {
      payload.command = command;
      payload.args = argsText
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean);
      const env = {};
      envText
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean)
        .forEach((line) => {
          const idx = line.indexOf("=");
          if (idx > 0) {
            env[line.slice(0, idx).trim()] = line.slice(idx + 1);
          }
        });
      payload.env = env;
    } else {
      payload.url = url;
    }
    onSubmit(payload);
  }

  return (
    <form className="provider-form" onSubmit={handleSubmit}>
      <div className="field">
        <label className="field-label" htmlFor="mcp-name">
          名称
        </label>
        <input
          id="mcp-name"
          className="field-input"
          value={name}
          maxLength={80}
          required
          onChange={(event) => setName(event.target.value)}
        />
      </div>
      <div className="field">
        <label className="field-label" htmlFor="mcp-transport">
          传输方式
        </label>
        <select
          id="mcp-transport"
          className="field-input"
          value={transportKind}
          onChange={(event) => setTransportKind(event.target.value)}
        >
          <option value="stdio">stdio（沙箱容器内执行）</option>
          <option value="http">HTTP（streamable HTTP）</option>
        </select>
      </div>
      {transportKind === "stdio" ? (
        <>
          <div className="field">
            <label className="field-label" htmlFor="mcp-command">
              启动命令
            </label>
            <input
              id="mcp-command"
              className="field-input"
              value={command}
              maxLength={4096}
              required
              placeholder="例如 npx"
              onChange={(event) => setCommand(event.target.value)}
            />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="mcp-args">
              参数（每行一个）
            </label>
            <textarea
              id="mcp-args"
              className="field-input"
              rows={3}
              value={argsText}
              onChange={(event) => setArgsText(event.target.value)}
            />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="mcp-env">
              环境变量（每行 KEY=VALUE，值可选）
            </label>
            <textarea
              id="mcp-env"
              className="field-input"
              rows={3}
              value={envText}
              onChange={(event) => setEnvText(event.target.value)}
            />
          </div>
        </>
      ) : (
        <div className="field">
          <label className="field-label" htmlFor="mcp-url">
            MCP 地址（https）
          </label>
          <input
            id="mcp-url"
            className="field-input"
            value={url}
            maxLength={512}
            required
            placeholder="https://mcp.example.com"
            onChange={(event) => setUrl(event.target.value)}
          />
        </div>
      )}
      {error && (
        <div className="form-alert" role="alert">
          {error}
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

/** 单个 MCP server 卡片：启停 / 发现工具 / 删除。 */
function McpServerCard({
  server,
  tools,
  isDiscovering,
  discoverError,
  isRateLimited,
  retryAfter,
  isToggling,
  cardAlert,
  onDiscover,
  onToggle,
  onDelete,
}) {
  return (
    <li className="provider-card">
      <div className="provider-card-row">
        <div className="provider-card-main">
          <div className="provider-card-title">
            <strong>{server.name}</strong>
            <span className="provider-model-badge">{server.transport_kind}</span>
            {server.enabled ? (
              <span className="badge-default">已启用</span>
            ) : (
              <span className="badge-default">已停用</span>
            )}
          </div>
          <p className="provider-v2-meta">
            {server.has_command ? "命令材料已加密存储" : "HTTP 地址"}
          </p>
          {discoverError && (
            <div className="form-alert" role="alert">
              {discoverError}
            </div>
          )}
          {isRateLimited && (
            <p className="provider-test-result is-limited" role="status">
              {DISCOVER_RATE_LIMIT_TEMPLATE.replace("{n}", String(retryAfter))}
            </p>
          )}
          {tools && (
            <p className="provider-v2-meta">
              {tools.length > 0
                ? `已发现 ${tools.length} 个工具：${tools.map((t) => t.tool_name).join("、")}`
                : "未发现工具"}
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
            disabled={isDiscovering || isRateLimited}
            onClick={() => onDiscover(server)}
          >
            {isDiscovering ? "发现中…" : "发现工具"}
          </button>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            disabled={isToggling}
            onClick={() => onToggle(server)}
          >
            {server.enabled ? "停用" : "启用"}
          </button>
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => onDelete(server)}>
            删除
          </button>
        </div>
      </div>
    </li>
  );
}

/**
 * 用户 MCP 管理页（Phase 10 M7/M8）。
 *
 * 后端契约（backend/api/v2/mcp.py）：
 * - GET /api/mcp/servers → {data:[server]}（零命令/env 出参）；
 * - POST（幂等键；stdio 命令材料信封加密）；
 * - PUT /{id}（幂等键；name/enabled 三态）；
 * - DELETE /{id}（幂等键；物理删 + 挂载任务 kill switch 联动）；
 * - POST /{id}/discover（无幂等键；限流 10/h；502 失败统一面）。
 */
export default function McpSettingsPage() {
  const { v2User } = useAuth();
  const [servers, setServers] = useState(null);
  const [loadError, setLoadError] = useState(null);

  const [isAddOpen, setIsAddOpen] = useState(false);
  const [isCreating, setIsCreating] = useState(false);
  const [addError, setAddError] = useState(null);

  const [toolsById, setToolsById] = useState({});
  const [discoveringId, setDiscoveringId] = useState(null);
  const [discoverErrors, setDiscoverErrors] = useState({});
  const [rateLimited, setRateLimited] = useState(false);
  const { retryAfter, start: startRetryAfter } = useRetryAfter();

  const [togglingId, setTogglingId] = useState(null);
  const [cardAlerts, setCardAlerts] = useState({});

  const [deleteTarget, setDeleteTarget] = useState(null);
  const [isDeleting, setIsDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState(null);

  const load = useCallback(async () => {
    try {
      const result = await requestV2(V2_MCP_SERVERS);
      setServers(result.data ?? []);
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

  async function handleCreate(payload) {
    if (isCreating) {
      return;
    }
    setIsCreating(true);
    setAddError(null);
    try {
      const body = { name: payload.name, transport_kind: payload.transportKind };
      if (payload.transportKind === "stdio") {
        body.command = payload.command;
        if (payload.args.length > 0) {
          body.args = payload.args;
        }
        if (Object.keys(payload.env).length > 0) {
          body.env = payload.env;
        }
      } else {
        body.url = payload.url;
      }
      await requestV2(V2_MCP_SERVERS, {
        method: "POST",
        body,
        idempotencyKey: newIdempotencyKey(),
      });
      setIsAddOpen(false);
      await load();
    } catch (cause) {
      setAddError(describeError(cause));
    } finally {
      setIsCreating(false);
    }
  }

  async function handleDiscover(server) {
    if (discoveringId || (rateLimited && retryAfter > 0)) {
      return;
    }
    setDiscoveringId(server.id);
    setDiscoverErrors((current) => ({ ...current, [server.id]: null }));
    try {
      const result = await requestV2(`${V2_MCP_SERVERS}/${server.id}/discover`, {
        method: "POST",
      });
      setToolsById((current) => ({ ...current, [server.id]: result.data?.tools ?? [] }));
    } catch (cause) {
      if (cause instanceof V2ApiError && cause.status === 429) {
        startRetryAfter(cause.retryAfter);
        setRateLimited(true);
        return;
      }
      setDiscoverErrors((current) => ({ ...current, [server.id]: describeError(cause) }));
    } finally {
      setDiscoveringId(null);
    }
  }

  async function handleToggle(server) {
    if (togglingId) {
      return;
    }
    setTogglingId(server.id);
    setCardAlerts((current) => ({ ...current, [server.id]: undefined }));
    try {
      const result = await requestV2(`${V2_MCP_SERVERS}/${server.id}`, {
        method: "PUT",
        body: { enabled: !server.enabled },
        idempotencyKey: newIdempotencyKey(),
      });
      setServers((current) =>
        (current ?? []).map((row) => (row.id === server.id ? result.data : row))
      );
    } catch (cause) {
      setCardAlerts((current) => ({ ...current, [server.id]: describeError(cause) }));
    } finally {
      setTogglingId(null);
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
      await requestV2(`${V2_MCP_SERVERS}/${targetId}`, {
        method: "DELETE",
        idempotencyKey: newIdempotencyKey(),
      });
      setDeleteTarget(null);
      setServers((current) => (current ?? []).filter((row) => row.id !== targetId));
    } catch (cause) {
      setDeleteError(describeError(cause));
    } finally {
      setIsDeleting(false);
    }
  }

  if (!v2User) {
    return (
      <main className="page">
        <div className="provider-settings">
          <header className="page-header">
            <h1 className="page-title">MCP 服务器</h1>
          </header>
          <div className="provider-empty">
            <p>MCP 管理需要新版账户会话。</p>
            <p className="provider-empty-note">请登录后再来注册你自己的 MCP 服务器。</p>
          </div>
        </div>
      </main>
    );
  }

  return (
    <main className="page">
      <div className="provider-settings">
        <header className="page-header">
          <h1 className="page-title">MCP 服务器</h1>
          <p className="page-sub">
            注册你自己的 MCP 服务器（stdio 在一次性沙箱容器内执行，或 streamable
            HTTP）。命令与环境变量加密存储；建任务时可选择挂载。
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
            <Plus size={14} aria-hidden="true" /> 注册 MCP 服务器
          </button>
          <Link to="/profile" className="btn btn-ghost">
            <ArrowLeft size={14} aria-hidden="true" /> 返回个人中心
          </Link>
        </div>

        {isAddOpen && (
          <AddMcpServerForm
            isSubmitting={isCreating}
            error={addError}
            onSubmit={handleCreate}
            onCancel={() => {
              setAddError(null);
              setIsAddOpen(false);
            }}
          />
        )}

        {servers === null && !loadError && <p className="provider-empty-note">加载中…</p>}

        {servers !== null && servers.length === 0 && (
          <div className="provider-empty">
            <p>还没有注册 MCP 服务器。</p>
            <p className="provider-empty-note">
              注册后先「发现工具」，再在创建任务时选择挂载。
            </p>
          </div>
        )}

        {servers !== null && servers.length > 0 && (
          <ul className="provider-list">
            {servers.map((server) => (
              <McpServerCard
                key={server.id}
                server={server}
                tools={toolsById[server.id]}
                isDiscovering={discoveringId === server.id}
                discoverError={discoverErrors[server.id]}
                isRateLimited={rateLimited && retryAfter > 0}
                retryAfter={retryAfter}
                isToggling={togglingId === server.id}
                cardAlert={cardAlerts[server.id] ?? null}
                onDiscover={handleDiscover}
                onToggle={handleToggle}
                onDelete={setDeleteTarget}
              />
            ))}
          </ul>
        )}

        {deleteTarget && (
          <div className="modal-overlay" role="presentation">
            <div className="modal" role="dialog" aria-modal="true">
              <h2 className="modal-title">删除 MCP 服务器</h2>
              <p className="modal-body">
                将删除「{deleteTarget.name}」及其工具缓存。已挂载它的排队/运行中任务会被
                中止（kill switch）。
              </p>
              {deleteError && (
                <div className="form-alert" role="alert">
                  {deleteError}
                </div>
              )}
              <div className="modal-actions">
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={() => {
                    setDeleteError(null);
                    setDeleteTarget(null);
                  }}
                >
                  取消
                </button>
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={isDeleting}
                  onClick={handleConfirmDelete}
                >
                  {isDeleting ? "删除中…" : "确认删除"}
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </main>
  );
}
