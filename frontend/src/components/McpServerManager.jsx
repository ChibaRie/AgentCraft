import { useCallback, useEffect, useState } from "react";
import { Plus, X, Wrench } from "@phosphor-icons/react";
import { request } from "../api/client.js";
import { formatDateTime } from "../lib/datetime.js";

const STATUS_LABELS = { draft: "草稿", published: "已发布", offline: "已下架" };

const TRANSPORT_LABELS = { stdio: "stdio 命令", "http-sse": "HTTP(S) 端点" };

function StatusBadge({ status }) {
  return <span className={`skill-status is-${status}`}>{STATUS_LABELS[status] || status}</span>;
}

function SensitiveBadge() {
  return <span className="skill-status is-offline" title="敏感工具：启用将记录授权时点">敏感</span>;
}

function EnvVarRows({ rows, onChange }) {
  function setRow(index, patch) {
    onChange(rows.map((row, i) => (i === index ? { ...row, ...patch } : row)));
  }
  return (
    <div className="field">
      <span className="field-label">环境变量（可选，仅写入加密信封，保存后不可再查看）</span>
      {rows.map((row, index) => (
        <div className="task-example-row" key={index}>
          <input
            className="field-input"
            style={{ maxWidth: 160 }}
            value={row.key}
            placeholder="变量名"
            aria-label={`环境变量名 ${index + 1}`}
            onChange={(event) => setRow(index, { key: event.target.value })}
          />
          <input
            className="field-input"
            value={row.value}
            placeholder="变量值"
            aria-label={`环境变量值 ${index + 1}`}
            onChange={(event) => setRow(index, { value: event.target.value })}
          />
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            aria-label={`删除环境变量 ${index + 1}`}
            onClick={() => onChange(rows.filter((_, i) => i !== index))}
          >
            <X size={13} aria-hidden="true" />
          </button>
        </div>
      ))}
      <button
        type="button"
        className="btn btn-ghost btn-sm"
        onClick={() => onChange([...rows, { key: "", value: "" }])}
      >
        <Plus size={13} aria-hidden="true" />
        添加变量
      </button>
    </div>
  );
}

function CreateServerForm({ onCreated, onCancel }) {
  const [form, setForm] = useState({
    name: "",
    description: "",
    transport: "stdio",
    command: "",
    url: "",
  });
  const [envRows, setEnvRows] = useState([]);
  const [error, setError] = useState("");
  const [isSaving, setIsSaving] = useState(false);

  function setField(name, value) {
    setForm((current) => ({ ...current, [name]: value }));
    setError("");
  }

  async function handleSubmit(event) {
    event.preventDefault();
    setError("");
    if (!form.name.trim() || !form.description.trim()) {
      setError("请填写名称与功能描述");
      return;
    }
    if (form.transport === "stdio" && !form.command.trim()) {
      setError("stdio 传输需要填写启动命令");
      return;
    }
    if (form.transport === "http-sse" && !form.url.trim()) {
      setError("http-sse 传输需要填写端点 URL");
      return;
    }
    const env_vars = Object.fromEntries(
      envRows
        .filter((row) => row.key.trim())
        .map((row) => [row.key.trim(), row.value])
    );
    setIsSaving(true);
    try {
      const payload = await request("/api/mcp/servers", {
        method: "POST",
        body: JSON.stringify({
          name: form.name.trim(),
          description: form.description.trim(),
          transport: form.transport,
          command: form.transport === "stdio" ? form.command.trim() : null,
          url: form.transport === "http-sse" ? form.url.trim() : null,
          env_vars: Object.keys(env_vars).length > 0 ? env_vars : null,
        }),
      });
      onCreated(payload.data);
    } catch (caught) {
      setError(caught.message || "创建失败，请稍后重试");
    } finally {
      setIsSaving(false);
    }
  }

  return (
    <form className="profile-card rise" onSubmit={handleSubmit} aria-label="注册 MCP Server">
      <h2 className="profile-card-title">注册 MCP Server</h2>
      <div className="field">
        <label className="field-label">
          名称
          <input
            className="field-input"
            value={form.name}
            placeholder="例如：文件系统"
            onChange={(event) => setField("name", event.target.value)}
          />
        </label>
      </div>
      <div className="field">
        <label className="field-label">
          功能描述
          <input
            className="field-input"
            value={form.description}
            placeholder="一句话描述这个 Server 提供的能力"
            onChange={(event) => setField("description", event.target.value)}
          />
        </label>
      </div>
      <div className="field">
        <label className="field-label">
          传输类型
          <select
            className="field-input"
            value={form.transport}
            onChange={(event) => setField("transport", event.target.value)}
          >
            {Object.entries(TRANSPORT_LABELS).map(([value, label]) => (
              <option value={value} key={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
      </div>
      {form.transport === "stdio" ? (
        <div className="field">
          <label className="field-label">
            启动命令（在 mcp-sandbox 沙箱内执行）
            <input
              className="field-input"
              value={form.command}
              placeholder="例如：mcp-server-filesystem /workspace"
              onChange={(event) => setField("command", event.target.value)}
            />
          </label>
        </div>
      ) : (
        <div className="field">
          <label className="field-label">
            端点 URL（streamable HTTP）
            <input
              className="field-input"
              value={form.url}
              placeholder="例如：http://mcp-sandbox:3000/mcp"
              onChange={(event) => setField("url", event.target.value)}
            />
          </label>
        </div>
      )}
      <EnvVarRows rows={envRows} onChange={setEnvRows} />
      <div className="form-alert" role="alert" hidden={!error} style={{ marginBottom: 12 }}>
        {error}
      </div>
      <div className="expert-form-footer">
        <button type="submit" className="btn btn-primary" disabled={isSaving}>
          {isSaving ? "创建中…" : "创建"}
        </button>
        <button type="button" className="btn btn-ghost" onClick={onCancel}>
          取消
        </button>
      </div>
    </form>
  );
}

function ToolRow({ serverId, tool, busy, onToggle }) {
  return (
    <li className="tool-row" key={tool.id}>
      <div className="tool-row-info">
        <div className="tool-row-head">
          <strong className="tool-row-name">{tool.name}</strong>
          {tool.sensitive && <SensitiveBadge />}
        </div>
        <p className="tool-row-desc">{tool.description}</p>
      </div>
      <div className="binding-row-actions">
        <button
          type="button"
          className={`switch ${tool.enabled ? "is-on" : ""}`}
          role="switch"
          aria-checked={tool.enabled}
          aria-label={`${tool.enabled ? "关闭" : "启用"} ${tool.name}`}
          disabled={busy}
          onClick={() => onToggle(serverId, tool, !tool.enabled)}
        >
          <span className="switch-knob" aria-hidden="true" />
          <span className="switch-label">{tool.enabled ? "已启用" : "未启用"}</span>
        </button>
      </div>
    </li>
  );
}

function ServerCard({ server, busy, expandedTools, onAction }) {
  const tools = expandedTools[server.id];
  return (
    <article className="skill-card rise">
      <header className="skill-card-head">
        <div className="skill-card-title">
          <h3>{server.name}</h3>
          <StatusBadge status={server.status} />
        </div>
        <div className="skill-card-actions">
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            disabled={busy}
            onClick={() => onAction("discover", server)}
          >
            发现工具
          </button>
          {server.status === "published" ? (
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              disabled={busy}
              onClick={() => onAction("offline", server)}
            >
              下架
            </button>
          ) : (
            <button
              type="button"
              className="btn btn-primary btn-sm"
              disabled={busy}
              onClick={() => onAction("publish", server)}
            >
              发布
            </button>
          )}
          <button
            type="button"
            className="btn btn-ghost btn-sm is-danger"
            disabled={busy}
            onClick={() => onAction("delete", server)}
          >
            删除
          </button>
        </div>
      </header>
      <p className="skill-card-desc">{server.description}</p>
      <p className="skill-card-meta">
        {TRANSPORT_LABELS[server.transport] || server.transport}
        {server.transport === "stdio"
          ? ` · ${server.command}`
          : ` · ${server.url || ""}`}
        {server.env_var_names?.length > 0 && ` · 环境变量: ${server.env_var_names.join("、")}`}
      </p>

      {server.confirmDelete && (
        <div className="confirm-strip" role="alert">
          <span>确认删除「{server.name}」？仍有绑定或任务快照引用时无法删除（可先下架止损）。</span>
          <span className="confirm-strip-actions">
            <button
              type="button"
              className="btn btn-primary btn-sm"
              disabled={busy}
              onClick={() => onAction("delete-confirm", server)}
            >
              确认删除
            </button>
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => onAction("delete-cancel", server)}
            >
              取消
            </button>
          </span>
        </div>
      )}

      {tools && (
        <div className="binding-strip" role="status">
          {tools.length === 0 ? (
            "未发现任何工具：请确认 Server 可连接后重新发现。"
          ) : (
            <ul className="binding-list" style={{ marginTop: 8 }}>
              {tools.map((tool) => (
                <ToolRow
                  key={tool.id}
                  serverId={server.id}
                  tool={tool}
                  busy={busy}
                  onToggle={(serverId, item, next) => onAction("tool", server, item, next)}
                />
              ))}
            </ul>
          )}
        </div>
      )}
    </article>
  );
}

export default function McpServerManager() {
  const [servers, setServers] = useState([]);
  const [total, setTotal] = useState(0);
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [pageNotice, setPageNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [isCreating, setIsCreating] = useState(false);
  const [expandedTools, setExpandedTools] = useState({});

  const loadServers = useCallback(async () => {
    setIsLoading(true);
    setLoadError("");
    try {
      const payload = await request("/api/mcp/servers?page=1&size=100");
      setServers(payload.data);
      setTotal(payload.total);
    } catch (error) {
      setLoadError(error.message || "加载失败，请稍后重试");
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    loadServers();
  }, [loadServers]);

  function patchServer(id, patch) {
    setServers((current) =>
      current.map((server) => (server.id === id ? { ...server, ...patch } : server))
    );
  }

  function clearTransient() {
    setPageNotice("");
    setServers((current) => current.map(({ confirmDelete: _flag, ...rest }) => rest));
  }

  async function run(action) {
    setBusy(true);
    try {
      await action();
    } catch (error) {
      setPageNotice(error.message || "操作失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  }

  async function handleAction(action, server, tool, next) {
    clearTransient();
    if (action === "delete") {
      setServers((current) =>
        current.map((item) => ({ ...item, confirmDelete: item.id === server.id }))
      );
      return;
    }
    if (action === "delete-cancel") {
      patchServer(server.id, { confirmDelete: false });
      return;
    }

    await run(async () => {
      if (action === "delete-confirm") {
        await request(`/api/mcp/servers/${server.id}`, { method: "DELETE" });
        setServers((current) => current.filter((item) => item.id !== server.id));
        setTotal((current) => Math.max(0, current - 1));
        setPageNotice(`「${server.name}」已删除。`);
      } else if (action === "discover") {
        const payload = await request(`/api/mcp/servers/${server.id}/discover`, {
          method: "POST",
        });
        setExpandedTools((current) => ({ ...current, [server.id]: payload.data.tools }));
        setPageNotice(
          `「${server.name}」发现 ${payload.data.tools.length} 个工具；新工具默认关闭，请逐一启用。`
        );
      } else if (action === "publish") {
        const payload = await request(`/api/mcp/servers/${server.id}/publish`, {
          method: "POST",
        });
        patchServer(server.id, payload.data);
        setPageNotice(`「${server.name}」已发布，可绑定到专家。`);
      } else if (action === "offline") {
        const payload = await request(`/api/mcp/servers/${server.id}/offline`, {
          method: "POST",
        });
        patchServer(server.id, payload.data);
        setPageNotice(`「${server.name}」已下架：既有任务对它的后续调用会被立即阻断。`);
      } else if (action === "tool") {
        const payload = await request(
          `/api/mcp/servers/${server.id}/tools/${tool.id}`,
          { method: "PUT", body: JSON.stringify({ enabled: next }) }
        );
        setExpandedTools((current) => ({
          ...current,
          [server.id]: (current[server.id] || []).map((item) =>
            item.id === tool.id ? payload.data : item
          ),
        }));
        if (!next) {
          setPageNotice(`「${tool.name}」已禁用：既有任务对它的后续调用会被立即阻断。`);
        }
      }
    });
  }

  if (isLoading) {
    return (
      <div className="skill-list">
        <div className="skill-card is-skeleton" aria-hidden="true">
          <div className="skeleton-line is-title" />
          <div className="skeleton-line" />
          <div className="skeleton-line is-short" />
        </div>
      </div>
    );
  }

  return (
    <>
      <div className="manage-toolbar rise" style={{ "--rise-index": 2 }}>
        <span className="manage-count">{`共 ${total} 个 MCP Server`}</span>
        <button
          type="button"
          className="btn btn-primary"
          onClick={() => {
            clearTransient();
            setIsCreating(true);
          }}
        >
          注册 MCP Server
        </button>
      </div>

      <div className="form-alert" role="alert" hidden={!pageNotice} style={{ marginBottom: 16 }}>
        {pageNotice}
      </div>
      <div className="form-alert" role="alert" hidden={!loadError} style={{ marginBottom: 16 }}>
        {loadError}
      </div>

      {isCreating && (
        <CreateServerForm
          onCreated={(created) => {
            setIsCreating(false);
            setServers((current) => [created, ...current]);
            setTotal((current) => current + 1);
            setPageNotice(`「${created.name}」已创建，先「发现工具」再发布。`);
          }}
          onCancel={() => setIsCreating(false)}
        />
      )}

      {servers.length === 0 && !isCreating ? (
        <div className="empty-state rise">
          <strong>还没有 MCP Server</strong>
          注册一个 Server 并发现工具，发布后即可绑定到专家，让 Agent 在对话中真实调用。
        </div>
      ) : (
        <div className="skill-list">
          {servers.map((server) => (
            <ServerCard
              key={server.id}
              server={server}
              busy={busy}
              expandedTools={expandedTools}
              onAction={handleAction}
            />
          ))}
        </div>
      )}
    </>
  );
}
