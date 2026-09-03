import { useEffect, useState } from "react";
import { Plus } from "@phosphor-icons/react";
import { request } from "../api/client.js";

const STATUS_LABELS = { draft: "草稿", published: "已发布", offline: "已下架" };

/**
 * 专家编辑页 MCP 绑定面板（§8.2 P07 MCPBindingPanel）。
 * 只展示 Server 名称/状态/绑定开关，不展示连接信息（§6.3）。
 * 绑定默认关闭；启用是显式动作。
 */
export default function McpBindingPanel({ expertId, bindings, busy, onChanged }) {
  const [candidates, setCandidates] = useState([]);
  const [selectedServerId, setSelectedServerId] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const payload = await request("/api/mcp/servers?page=1&size=100");
        if (!cancelled) {
          setCandidates(payload.data);
        }
      } catch (caught) {
        if (!cancelled) {
          setError(caught.message || "加载 MCP Server 列表失败");
        }
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, []);

  const boundIds = new Set(bindings.map((item) => item.id));
  const available = candidates.filter(
    (server) => server.status === "published" && !boundIds.has(server.id)
  );

  async function run(action) {
    setError("");
    try {
      await action();
      await onChanged();
    } catch (caught) {
      setError(caught.message || "操作失败，请稍后重试");
    }
  }

  return (
    <section className="profile-card rise" style={{ "--rise-index": 3 }} aria-label="MCP 绑定">
      <h2 className="profile-card-title">MCP 绑定</h2>
      <p className="binding-hint">
        绑定已发布的 MCP Server 并启用后，Agent 在对话中可真实调用其工具；Server 下架或工具禁用会立即阻断既有任务的调用。
      </p>
      {bindings.length === 0 ? (
        <p className="detail-prose">还未绑定 MCP Server。注册并发布 Server 后可在此绑定。</p>
      ) : (
        <ul className="binding-list">
          {bindings.map((server) => (
            <li className="binding-row" key={server.id}>
              <div className="binding-row-main">
                <strong>{server.name}</strong>
                <span className={`skill-status is-${server.status}`}>
                  {STATUS_LABELS[server.status] || server.status}
                </span>
              </div>
              <div className="binding-row-actions">
                <button
                  type="button"
                  className={`switch ${server.enabled ? "is-on" : ""}`}
                  role="switch"
                  aria-checked={server.enabled}
                  aria-label={`${server.enabled ? "关闭" : "启用"} ${server.name}`}
                  disabled={busy}
                  onClick={() =>
                    run(() =>
                      request(`/api/experts/${expertId}/mcp/${server.id}`, {
                        method: "PUT",
                        body: JSON.stringify({ enabled: !server.enabled }),
                      })
                    )
                  }
                >
                  <span className="switch-knob" aria-hidden="true" />
                  <span className="switch-label">{server.enabled ? "已启用" : "未启用"}</span>
                </button>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  disabled={busy}
                  onClick={() =>
                    run(() =>
                      request(`/api/experts/${expertId}/mcp/${server.id}`, { method: "DELETE" })
                    )
                  }
                >
                  解绑
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      <div className="binding-add">
        <select
          className="field-input binding-select"
          value={selectedServerId}
          aria-label="选择要绑定的 MCP Server"
          onChange={(event) => setSelectedServerId(event.target.value)}
        >
          <option value="">选择已发布的 MCP Server…</option>
          {available.map((server) => (
            <option value={server.id} key={server.id}>
              {server.name}
            </option>
          ))}
        </select>
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          disabled={busy || !selectedServerId}
          onClick={() => {
            const serverId = Number(selectedServerId);
            run(() =>
              request(`/api/experts/${expertId}/mcp`, {
                method: "POST",
                body: JSON.stringify({ server_id: serverId }),
              })
            );
            setSelectedServerId("");
          }}
        >
          <Plus size={13} aria-hidden="true" />
          绑定
        </button>
      </div>
      <div className="field-error" role="alert">
        {error}
      </div>
    </section>
  );
}
