import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowLeft, CheckCircle, PencilSimple, Plus, Trash } from "@phosphor-icons/react";
import { request } from "../api/client.js";

const EMPTY_FORM = {
  name: "",
  protocol: "openai",
  base_url: "",
  api_key: "",
  model_id: "",
  is_default: false,
};

/**
 * P10 Provider 设置页（BYOK，手册 §7.7 双模式）。
 * Key 仅写入：编辑时留空 = 不变；响应只有尾 4 位掩码提示。
 * faux 为内置默认测试项，不经本页管理。
 */
export default function ProviderSettingsPage() {
  const [providers, setProviders] = useState([]);
  const [loadError, setLoadError] = useState(null);
  const [form, setForm] = useState(null); // null = 列表态；对象 = 新建/编辑表单
  const [editingId, setEditingId] = useState(null);
  const [notice, setNotice] = useState(null);
  const [confirmDeleteId, setConfirmDeleteId] = useState(null);
  const [isSaving, setIsSaving] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const payload = await request("/api/providers");
      setProviders(payload.data);
      setLoadError(null);
    } catch (cause) {
      setLoadError(cause.message);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  function openCreate() {
    setEditingId(null);
    setForm({ ...EMPTY_FORM });
    setNotice(null);
  }

  function openEdit(provider) {
    setEditingId(provider.id);
    setForm({
      name: provider.name,
      protocol: provider.protocol,
      base_url: provider.base_url,
      api_key: "", // 留空 = 不修改
      model_id: provider.model_id,
      is_default: provider.is_default,
    });
    setNotice(null);
  }

  async function handleSave(event) {
    event.preventDefault();
    if (!form || isSaving) {
      return;
    }
    setIsSaving(true);
    setNotice(null);
    try {
      const body = {
        name: form.name,
        protocol: form.protocol,
        base_url: form.base_url,
        model_id: form.model_id,
        is_default: form.is_default,
      };
      // Key 三态：新建空串=无 Key（本机免钥端点）；编辑留空=不变
      if (editingId === null) {
        body.api_key = form.api_key || null;
        await request("/api/providers", { method: "POST", body: JSON.stringify(body) });
      } else {
        if (form.api_key) {
          body.api_key = form.api_key;
        }
        await request(`/api/providers/${editingId}`, { method: "PUT", body: JSON.stringify(body) });
      }
      setForm(null);
      setEditingId(null);
      await refresh();
    } catch (cause) {
      setNotice(cause.message);
    } finally {
      setIsSaving(false);
    }
  }

  async function handleDelete(providerId) {
    try {
      await request(`/api/providers/${providerId}`, { method: "DELETE" });
      setConfirmDeleteId(null);
      await refresh();
    } catch (cause) {
      setNotice(cause.message);
    }
  }

  async function handleSetDefault(providerId) {
    try {
      await request(`/api/providers/${providerId}`, {
        method: "PUT",
        body: JSON.stringify({ is_default: true }),
      });
      await refresh();
    } catch (cause) {
      setNotice(cause.message);
    }
  }

  return (
    <main className="page">
      <div className="provider-settings">
        <header className="page-header">
          <h1 className="page-title">Provider 设置</h1>
          <p className="page-sub">
            配置你自己的模型服务（OpenAI 兼容端点）。Key 加密存储、仅写入；
            新建任务时可选定，配置变更不影响进行中的任务。
          </p>
        </header>

        {loadError && (
          <div className="form-alert" role="alert">
            {loadError}
          </div>
        )}
        {notice && (
          <div className="form-alert" role="alert">
            {notice}
          </div>
        )}

        {!form && (
          <div className="provider-toolbar">
            <button type="button" className="btn btn-primary" onClick={openCreate}>
              <Plus size={14} aria-hidden="true" /> 添加 Provider
            </button>
            <Link to="/profile" className="btn btn-ghost">
              <ArrowLeft size={14} aria-hidden="true" /> 返回个人中心
            </Link>
          </div>
        )}

        {form && (
          <form className="provider-form" onSubmit={handleSave}>
            <h2 className="provider-form-title">
              {editingId === null ? "添加 Provider" : "编辑 Provider"}
            </h2>
            <div className="field">
              <label className="field-label" htmlFor="provider-name">
                名称（如：DeepSeek 主力）
              </label>
              <input
                id="provider-name"
                className="field-input"
                value={form.name}
                minLength={2}
                maxLength={30}
                required
                onChange={(event) => setForm({ ...form, name: event.target.value })}
              />
            </div>
            <div className="field">
              <label className="field-label" htmlFor="provider-protocol">
                协议
              </label>
              <select
                id="provider-protocol"
                className="field-input"
                value={form.protocol}
                onChange={(event) => setForm({ ...form, protocol: event.target.value })}
              >
                <option value="openai">OpenAI 兼容（DeepSeek / Ollama / vLLM…）</option>
              </select>
            </div>
            <div className="field">
              <label className="field-label" htmlFor="provider-base-url">
                Base URL
              </label>
              <input
                id="provider-base-url"
                className="field-input"
                value={form.base_url}
                maxLength={500}
                placeholder="https://api.deepseek.com/v1"
                required
                onChange={(event) => setForm({ ...form, base_url: event.target.value })}
              />
            </div>
            <div className="field">
              <label className="field-label" htmlFor="provider-api-key">
                API Key {editingId !== null && "（留空 = 保持不变）"}
              </label>
              <input
                id="provider-api-key"
                className="field-input"
                type="password"
                value={form.api_key}
                maxLength={4096}
                placeholder={editingId !== null ? "••••••••" : "sk-…（本机免钥端点可留空）"}
                autoComplete="off"
                onChange={(event) => setForm({ ...form, api_key: event.target.value })}
              />
            </div>
            <div className="field">
              <label className="field-label" htmlFor="provider-model">
                模型 ID
              </label>
              <input
                id="provider-model"
                className="field-input"
                value={form.model_id}
                maxLength={100}
                placeholder="deepseek-chat"
                required
                onChange={(event) => setForm({ ...form, model_id: event.target.value })}
              />
            </div>
            <label className="provider-default-toggle">
              <input
                type="checkbox"
                checked={form.is_default}
                onChange={(event) => setForm({ ...form, is_default: event.target.checked })}
              />
              设为我的默认（建任务时默认选中）
            </label>
            <div className="task-create-actions">
              <button
                type="button"
                className="btn btn-ghost"
                onClick={() => {
                  setForm(null);
                  setEditingId(null);
                }}
              >
                取消
              </button>
              <button type="submit" className="btn btn-primary" disabled={isSaving}>
                {isSaving ? "保存中…" : "保存"}
              </button>
            </div>
          </form>
        )}

        {!form && providers.length === 0 && !loadError && (
          <div className="provider-empty">
            <p>还没有自定义 Provider。</p>
            <p className="provider-empty-note">
              未配置时任务使用部署者的系统默认；联调可随时使用内置的 faux
              测试引擎（无需任何 Key）。
            </p>
          </div>
        )}

        {!form && providers.length > 0 && (
          <ul className="provider-list">
            {providers.map((provider) => (
              <li className="provider-card" key={provider.id}>
                <div className="provider-card-main">
                  <div className="provider-card-title">
                    <strong>{provider.name}</strong>
                    {provider.is_default && (
                      <span className="badge badge-default">
                        <CheckCircle size={12} aria-hidden="true" /> 默认
                      </span>
                    )}
                  </div>
                  <p className="provider-card-meta">
                    {provider.protocol} · {provider.base_url} · {provider.model_id}
                  </p>
                  <p className="provider-card-key">Key：{provider.api_key_hint || "未设置"}</p>
                </div>
                <div className="provider-card-actions">
                  {!provider.is_default && (
                    <button
                      type="button"
                      className="btn btn-ghost btn-sm"
                      onClick={() => handleSetDefault(provider.id)}
                    >
                      设为默认
                    </button>
                  )}
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm"
                    onClick={() => openEdit(provider)}
                  >
                    <PencilSimple size={13} aria-hidden="true" /> 编辑
                  </button>
                  {confirmDeleteId === provider.id ? (
                    <>
                      <button
                        type="button"
                        className="btn btn-primary btn-sm"
                        onClick={() => handleDelete(provider.id)}
                      >
                        确认删除
                      </button>
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm"
                        onClick={() => setConfirmDeleteId(null)}
                      >
                        取消
                      </button>
                    </>
                  ) : (
                    <button
                      type="button"
                      className="btn btn-ghost btn-sm provider-delete"
                      onClick={() => setConfirmDeleteId(provider.id)}
                    >
                      <Trash size={13} aria-hidden="true" /> 删除
                    </button>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </main>
  );
}
