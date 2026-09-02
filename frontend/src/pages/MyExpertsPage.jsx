import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useAuth } from "../auth/AuthContext.jsx";
import { request } from "../api/client.js";
import { formatDateTime } from "../lib/datetime.js";
import { CATEGORY_LABELS, STATUS_LABELS } from "../lib/categories.js";

function ExpertRow({ expert, busy, onAction }) {
  const isPublished = expert.status === "published";
  return (
    <article className="skill-card rise">
      <header className="skill-card-head">
        <div className="skill-card-title">
          <h3>{expert.name}</h3>
          <span className={`skill-status is-${expert.status}`}>
            {STATUS_LABELS[expert.status] || expert.status}
          </span>
          <span className="role-badge">{CATEGORY_LABELS[expert.category] || expert.category}</span>
        </div>
        <div className="skill-card-actions">
          {isPublished ? (
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              disabled={busy}
              onClick={() => onAction("offline", expert)}
            >
              下架
            </button>
          ) : (
            <button
              type="button"
              className="btn btn-primary btn-sm"
              disabled={busy}
              onClick={() => onAction("publish", expert)}
            >
              发布
            </button>
          )}
          <Link to={`/my-experts/${expert.id}/edit`} className="btn btn-ghost btn-sm">
            编辑
          </Link>
          <button
            type="button"
            className="btn btn-ghost btn-sm is-danger"
            disabled={busy}
            onClick={() => onAction("delete", expert)}
          >
            删除
          </button>
        </div>
      </header>
      <p className="skill-card-desc">{expert.description}</p>
      <p className="skill-card-meta">更新于 {formatDateTime(expert.updated_at)}</p>

      {expert.confirmDelete && (
        <div className="confirm-strip" role="alert">
          <span>
            确认删除「{expert.name}」？专家下存在任务时无法删除，绑定关系会一并移除。
          </span>
          <span className="confirm-strip-actions">
            <button
              type="button"
              className="btn btn-primary btn-sm"
              disabled={busy}
              onClick={() => onAction("delete-confirm", expert)}
            >
              确认删除
            </button>
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => onAction("delete-cancel", expert)}
            >
              取消
            </button>
          </span>
        </div>
      )}
    </article>
  );
}

export default function MyExpertsPage() {
  const { isReady, isAuthenticated } = useAuth();
  const [experts, setExperts] = useState([]);
  const [total, setTotal] = useState(0);
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [pageNotice, setPageNotice] = useState("");
  const [busy, setBusy] = useState(false);

  const loadExperts = useCallback(async () => {
    setIsLoading(true);
    setLoadError("");
    try {
      const payload = await request("/api/experts?page=1&size=100");
      setExperts(payload.data);
      setTotal(payload.total);
    } catch (error) {
      setLoadError(error.message || "加载失败，请稍后重试");
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (isReady && isAuthenticated) {
      loadExperts();
    }
  }, [isReady, isAuthenticated, loadExperts]);

  function patchExpert(updated) {
    setExperts((current) =>
      current.map((expert) => (expert.id === updated.id ? updated : expert))
    );
  }

  function clearInline() {
    setPageNotice("");
    setExperts((current) => current.map(({ confirmDelete: _flag, ...rest }) => rest));
  }

  async function handleAction(action, expert) {
    clearInline();
    if (action === "delete") {
      setExperts((current) =>
        current.map((item) => ({ ...item, confirmDelete: item.id === expert.id }))
      );
      return;
    }
    if (action === "delete-cancel") {
      patchExpert({ ...expert, confirmDelete: false });
      return;
    }

    setBusy(true);
    try {
      if (action === "delete-confirm") {
        await request(`/api/experts/${expert.id}`, { method: "DELETE" });
        setExperts((current) => current.filter((item) => item.id !== expert.id));
        setTotal((current) => Math.max(0, current - 1));
      } else if (action === "publish") {
        const payload = await request(`/api/experts/${expert.id}/publish`, { method: "POST" });
        patchExpert(payload.data);
        setPageNotice(`「${expert.name}」已发布，现在可以在专家中心被看到。`);
      } else if (action === "offline") {
        const payload = await request(`/api/experts/${expert.id}/offline`, { method: "POST" });
        patchExpert(payload.data);
        setPageNotice(`「${expert.name}」已下架，不再出现在专家中心。`);
      }
    } catch (error) {
      setPageNotice(error.message || "操作失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="app-main">
      <header className="page-header rise">
        <h1 className="page-title">我的专家</h1>
        <p className="page-sub">创建并管理你的专家：配置人设与方法论，绑定 Skill 后发布。</p>
      </header>

      <div className="manage-toolbar rise" style={{ "--rise-index": 1 }}>
        <span className="manage-count">{isLoading ? "" : `共 ${total} 位专家`}</span>
        <Link to="/my-experts/new" className="btn btn-primary">
          新建专家
        </Link>
      </div>

      <div className="form-alert" role="alert" hidden={!pageNotice} style={{ marginBottom: 16 }}>
        {pageNotice}
      </div>
      <div className="form-alert" role="alert" hidden={!loadError} style={{ marginBottom: 16 }}>
        {loadError}
      </div>

      {isLoading ? (
        <div className="skill-list">
          {[0, 1].map((index) => (
            <div className="skill-card is-skeleton" key={index} aria-hidden="true">
              <div className="skeleton-line is-title" />
              <div className="skeleton-line" />
              <div className="skeleton-line is-short" />
            </div>
          ))}
        </div>
      ) : experts.length === 0 ? (
        <div className="empty-state rise">
          <strong>还没有专家</strong>
          点击「新建专家」创建第一位专家：填写人设与方法论，绑定已启用的 Skill 后发布到专家中心。
        </div>
      ) : (
        <div className="skill-list">
          {experts.map((expert) => (
            <ExpertRow key={expert.id} expert={expert} busy={busy} onAction={handleAction} />
          ))}
        </div>
      )}
    </main>
  );
}
