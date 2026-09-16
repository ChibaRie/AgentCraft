import { useCallback, useEffect, useRef, useState } from "react";
import AdminReasonPrompt from "../../components/AdminReasonPrompt.jsx";
import { useAdminGate } from "../../components/RequireAdmin.jsx";
import {
  killSwitchTool,
  listCatalogProviders,
  listCatalogTools,
  updateCatalogProvider,
  updateCatalogTool,
} from "../../api/v2/admin.js";
import { newIdempotencyKey, V2ApiError } from "../../api/v2/client.js";
import { useRetryAfter } from "../../hooks/useRetryAfter.js";

const FALLBACK_MESSAGE = "加载失败，请稍后重试";
const REASON_LABEL = "操作原因（必填，审计留痕）";

function describeError(error) {
  return error instanceof Error ? error.message : FALLBACK_MESSAGE;
}

/**
 * 目录与 kill-switch 页（Phase 8 T12b Step 3）。
 *
 * 三分区：①Provider 目录（含停用条目全量视图）启停 + models 白名单编辑器
 * （整表替换心智提示——PUT 语义为清单整体覆盖，收缩不回溯存量）；②平台工具
 * 启停（label 由后端未登记组合回退 tool_id，前端原样渲染）；③kill-switch
 * 危险壳：单版本确认（输入完整版本号防误触）+ reason + 429 Retry-After 倒计时
 * （复用 useRetryAfter；重试同幂等键——重放不耗窗 R4）+ termination 回执
 * （null=executor 缺位/重放基线，渲染「仅停用目录条目」非失败提示——R2）。
 */
export default function CatalogPage() {
  const gate = useAdminGate();
  const gateRef = useRef(gate);
  gateRef.current = gate;

  const [providers, setProviders] = useState(null);
  const [tools, setTools] = useState(null);
  const [loading, setLoading] = useState(true);
  const [alert, setAlert] = useState("");

  // 统一 reason 弹窗编排：{kind:"provider-toggle"|"provider-models"|"tool-toggle", ...}
  const [prompt, setPrompt] = useState(null);
  // models 编辑器弹窗：{provider, draft}
  const [editor, setEditor] = useState(null);

  // kill-switch 危险壳：{toolId, version, label}
  const [kill, setKill] = useState(null);
  const [killConfirm, setKillConfirm] = useState("");
  const [killReason, setKillReason] = useState("");
  const [killAlert, setKillAlert] = useState("");
  const [killBusy, setKillBusy] = useState(false);
  const [killReceipt, setKillReceipt] = useState(null);
  const killKeyRef = useRef(null);
  const { retryAfter, start: startRetryAfter } = useRetryAfter();

  const load = useCallback(async () => {
    setLoading(true);
    setAlert("");
    try {
      const [providerResult, toolResult] = await Promise.all([
        listCatalogProviders(),
        listCatalogTools(),
      ]);
      setProviders(providerResult.data ?? { items: [], total: 0 });
      setTools(toolResult.data ?? { items: [], total: 0 });
    } catch (error) {
      if (gateRef.current.reportAdminError(error, load)) {
        return;
      }
      setAlert(describeError(error));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // 危险壳每次打开生成新幂等键；429 倒计时后的重试**复用同键**（R3/R4）
  useEffect(() => {
    if (kill) {
      killKeyRef.current = newIdempotencyKey();
      setKillConfirm("");
      setKillReason("");
      setKillAlert("");
    }
  }, [kill]);

  async function handlePromptSubmit({ reason, idempotencyKey }) {
    if (prompt.kind === "provider-toggle") {
      await updateCatalogProvider({
        providerId: prompt.provider.id,
        enabled: prompt.enabled,
        reason,
        idempotencyKey,
      });
    } else if (prompt.kind === "provider-models") {
      await updateCatalogProvider({
        providerId: prompt.provider.id,
        models: prompt.models,
        reason,
        idempotencyKey,
      });
    } else {
      await updateCatalogTool({
        toolId: prompt.tool.tool_id,
        version: prompt.tool.version,
        enabled: prompt.enabled,
        reason,
        idempotencyKey,
      });
    }
    setPrompt(null);
    await load();
  }

  function handleModelsNext() {
    const models = editor.draft
      .split("\n")
      .map((line) => line.trim())
      .filter((line) => line !== "");
    if (models.length === 0) {
      setAlert("白名单不能为空（如需全量下线请使用停用开关）");
      return;
    }
    setAlert("");
    setPrompt({ kind: "provider-models", provider: editor.provider, models });
    setEditor(null);
  }

  function openKill(tool) {
    setKillReceipt(null);
    setKill({ toolId: tool.tool_id, version: tool.version, label: tool.label });
  }

  function closeKill() {
    setKill(null);
    setKillConfirm("");
    setKillReason("");
    setKillAlert("");
  }

  async function handleKillSubmit() {
    if (killBusy || retryAfter > 0) {
      return;
    }
    setKillBusy(true);
    setKillAlert("");
    try {
      const result = await killSwitchTool({
        toolId: kill.toolId,
        version: kill.version,
        reason: killReason.trim(),
        idempotencyKey: killKeyRef.current,
      });
      setKillReceipt(result.data ?? null);
      closeKill();
      await load();
    } catch (error) {
      if (gateRef.current.reportAdminError(error, handleKillSubmit)) {
        closeKill();
        return;
      }
      if (error instanceof V2ApiError && error.status === 429) {
        startRetryAfter(error.retryAfter);
        setKillAlert(error.message || "操作过于频繁，请稍后再试");
      } else {
        setKillAlert(describeError(error));
      }
    } finally {
      setKillBusy(false);
    }
  }

  const providerItems = providers?.items ?? [];
  const toolItems = tools?.items ?? [];
  const killTermination = killReceipt?.termination ?? null;

  return (
    <>
      <section className="profile-card rise" style={{ "--rise-index": 0 }}>
        <h2 className="profile-card-title">Provider 目录</h2>
        {alert ? (
          <p className="form-alert" role="alert">
            {alert}
          </p>
        ) : null}
        {loading ? <p className="page-sub">加载中…</p> : null}
        {!loading && providerItems.length === 0 ? (
          <p className="page-sub">目录为空</p>
        ) : null}
        {!loading && providerItems.length > 0 ? (
          <table className="admin-table">
            <thead>
              <tr>
                <th scope="col">名称</th>
                <th scope="col">允许主机</th>
                <th scope="col">模型白名单</th>
                <th scope="col">状态</th>
                <th scope="col">操作</th>
              </tr>
            </thead>
            <tbody>
              {providerItems.map((provider) => (
                <tr key={provider.id}>
                  <td>{provider.display_name}</td>
                  <td>{provider.allowed_host}</td>
                  <td>{(provider.models ?? []).join("、") || "—"}</td>
                  <td>{provider.enabled ? "启用" : "停用"}</td>
                  <td>
                    <div className="admin-actions">
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm"
                        onClick={() =>
                          setPrompt({ kind: "provider-toggle", provider, enabled: !provider.enabled })
                        }
                      >
                        {provider.enabled ? "停用 Provider" : "启用 Provider"}
                      </button>
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm"
                        onClick={() =>
                          setEditor({ provider, draft: (provider.models ?? []).join("\n") })
                        }
                      >
                        编辑白名单
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
      </section>

      <section className="profile-card rise" style={{ "--rise-index": 1 }}>
        <h2 className="profile-card-title">平台工具</h2>
        {!loading && toolItems.length === 0 ? <p className="page-sub">目录为空</p> : null}
        {!loading && toolItems.length > 0 ? (
          <table className="admin-table">
            <thead>
              <tr>
                <th scope="col">工具</th>
                <th scope="col">tool_id</th>
                <th scope="col">版本</th>
                <th scope="col">权限</th>
                <th scope="col">状态</th>
                <th scope="col">操作</th>
              </tr>
            </thead>
            <tbody>
              {toolItems.map((tool) => (
                <tr key={`${tool.tool_id}@${tool.version}`}>
                  <td>{tool.label}</td>
                  <td>{tool.tool_id}</td>
                  <td>{tool.version}</td>
                  <td>{(tool.permissions ?? []).join("、") || "—"}</td>
                  <td>{tool.enabled ? "启用" : "停用"}</td>
                  <td>
                    <div className="admin-actions">
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm"
                        onClick={() =>
                          setPrompt({ kind: "tool-toggle", tool, enabled: !tool.enabled })
                        }
                      >
                        {tool.enabled ? "停用" : "启用"}
                      </button>
                      <button
                        type="button"
                        className="btn btn-ghost is-danger btn-sm"
                        onClick={() => openKill(tool)}
                      >
                        Kill Switch
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}

        <div className="danger-zone">
          <h3>Kill Switch（单版本粒度）</h3>
          <p>
            停用指定 tool_id@version 并联动终止其 queued/running 任务（aborted(tool_revoked)）。
            该操作限流 admin_kill_switch 10 次/小时；确认壳要求输入完整版本号，防误触。
            在上方工具列表按行点击「Kill Switch」发起。
          </p>
          {killReceipt ? (
            <div className="admin-notice" role="status">
              <p>
                kill-switch 已执行：{killReceipt.tool_id}@{killReceipt.version} 已停用
                {killReceipt.already_in_state ? "（目标本已停用，幂等短路）" : ""}。
              </p>
              {killTermination ? (
                <ul>
                  <li>停止执行：{killTermination.stopped ?? 0}</li>
                  <li>终止任务：{(killTermination.aborted_task_ids ?? []).length}</li>
                </ul>
              ) : (
                <p>
                  executor 缺位或重放基线：本次仅停用目录条目，未联动终止运行中任务（非失败；重试幂等自愈）。
                </p>
              )}
            </div>
          ) : null}
        </div>
      </section>

      {/* models 白名单编辑器（整表替换心智提示） */}
      {editor ? (
        <div className="modal-overlay">
          <div className="modal" role="dialog" aria-modal="true" aria-labelledby="admin-models-title">
            <h3 id="admin-models-title" className="modal-title">
              编辑白名单：{editor.provider.display_name}
            </h3>
            <p className="modal-body">
              白名单为整表替换语义：提交后以下清单将整体覆盖现有白名单（收缩不回溯存量引用）。
            </p>
            <div className="field">
              <label className="field-label" htmlFor="admin-models-input">
                模型白名单（每行一个）
                <textarea
                  id="admin-models-input"
                  className="field-input"
                  rows={5}
                  value={editor.draft}
                  onChange={(event) => setEditor({ ...editor, draft: event.target.value })}
                />
              </label>
            </div>
            <div className="modal-footer">
              <button type="button" className="btn btn-ghost" onClick={() => setEditor(null)}>
                取消
              </button>
              <button type="button" className="btn btn-primary" onClick={handleModelsNext}>
                下一步
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {/* kill-switch 危险壳：单版本确认 + reason + 429 倒计时 */}
      {kill ? (
        <div className="modal-overlay">
          <div className="modal v2-danger-dialog" role="dialog" aria-modal="true" aria-labelledby="admin-kill-title">
            <h3 id="admin-kill-title" className="modal-title">
              Kill Switch：{kill.label}（{kill.toolId}@{kill.version}）
            </h3>
            <p className="modal-body">
              将停用该工具版本并联动终止其 queued/running 任务。操作写入审计。
            </p>
            <div className="field">
              <label className="field-label" htmlFor="admin-kill-confirm">
                版本号确认
                <input
                  id="admin-kill-confirm"
                  className="field-input"
                  value={killConfirm}
                  placeholder={kill.version}
                  onChange={(event) => setKillConfirm(event.target.value)}
                />
              </label>
              <p className="field-note">输入完整版本号 {kill.version} 以启用确认按钮（单版本粒度防误触）。</p>
            </div>
            <div className="field">
              <label className="field-label" htmlFor="admin-kill-reason">
                {REASON_LABEL}
                <textarea
                  id="admin-kill-reason"
                  className="field-input"
                  rows={3}
                  maxLength={2000}
                  value={killReason}
                  disabled={killBusy}
                  onChange={(event) => {
                    setKillReason(event.target.value);
                    if (killAlert) {
                      setKillAlert("");
                    }
                  }}
                />
              </label>
            </div>
            {killAlert ? (
              <p className="form-alert" role="alert">
                {killAlert}
              </p>
            ) : null}
            {retryAfter > 0 ? (
              <p className="v2-retry-hint" role="status">
                触发限流，请等待 {retryAfter} 秒后重试
              </p>
            ) : null}
            <div className="modal-footer">
              <button type="button" className="btn btn-ghost" onClick={closeKill} disabled={killBusy}>
                取消
              </button>
              <button
                type="button"
                className="btn btn-primary"
                disabled={
                  killBusy ||
                  retryAfter > 0 ||
                  killConfirm !== kill.version ||
                  killReason.trim() === ""
                }
                onClick={handleKillSubmit}
              >
                {killBusy ? "提交中…" : "确认执行"}
              </button>
            </div>
          </div>
        </div>
      ) : null}

      <AdminReasonPrompt
        open={prompt !== null}
        title={
          prompt?.kind === "provider-toggle"
            ? `${prompt.enabled ? "启用" : "停用"} Provider`
            : prompt?.kind === "provider-models"
              ? "更新模型白名单"
              : `${prompt?.enabled ? "启用" : "停用"} 平台工具`
        }
        description={
          prompt?.kind === "provider-toggle"
            ? `目标：${prompt.provider.display_name}（${prompt.provider.allowed_host}）。该操作写入审计并要求填写原因。`
            : prompt?.kind === "provider-models"
              ? `目标：${prompt.provider.display_name}；新白名单：${(prompt.models ?? []).join("、")}（整表替换）。该操作写入审计并要求填写原因。`
              : prompt?.tool
                ? `目标：${prompt.tool.label}（${prompt.tool.tool_id}@${prompt.tool.version}）。该操作写入审计并要求填写原因。`
                : ""
        }
        onSubmit={handlePromptSubmit}
        onClose={() => setPrompt(null)}
      />
    </>
  );
}
