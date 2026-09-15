import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useAuth } from "../auth/AuthContext.jsx";
import { newIdempotencyKey, requestV2 } from "../api/v2/client.js";
import { V2_AUTHORING_EXPERTS } from "../api/v2/routes.js";
import { formatDateTime } from "../lib/datetime.js";

// V2 状态词表（实体 draft/published + latest revision pending_review/rejected）。
// 本页局部定义：lib/categories.js 为 V1 面（T11 领地），Phase 8 期间不动。
const STATUS_LABELS = {
  draft: "草稿",
  published: "已发布",
  pending_review: "审核中",
  rejected: "已驳回",
};

/**
 * 列表徽标派生：最新 revision 处于审核中/已驳回时优先展示（作者最关心最新一版
 * 的去向），否则回落实体状态（offline 后实体回到 draft，徽标自然显示草稿）。
 */
function deriveStatus(row) {
  const latestStatus = row.latest_revision?.status;
  if (latestStatus === "pending_review" || latestStatus === "rejected") {
    return latestStatus;
  }
  return row.status;
}

function StatusBadge({ status }) {
  return (
    <span className={`skill-status is-${status}`}>{STATUS_LABELS[status] || status}</span>
  );
}

/**
 * 非 expert_author 的整页 403 引导面（Sup §10.6：作者面写端点非 expert_author
 * 403 FORBIDDEN；页面级前置门避免无效请求）。三页同构，各自内联（领地纪律）。
 */
function AuthorGate() {
  return (
    <main className="app-main">
      <div className="empty-state rise" role="alert">
        <strong>403 · 无专家作者权限</strong>
        作者面需要 expert_author 权益：当前账号未获得授权。
        请使用已受邀并开通该权益的账号登录，或联系平台管理员授予。
      </div>
    </main>
  );
}

function ExpertRow({ expert, busy, confirmOpen, onAction }) {
  const status = deriveStatus(expert);
  const isPublished = expert.status === "published";
  return (
    <article className="skill-card rise">
      <header className="skill-card-head">
        <div className="skill-card-title">
          <h3>{expert.name}</h3>
          <StatusBadge status={status} />
        </div>
        <div className="skill-card-actions">
          {isPublished && (
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              disabled={busy}
              onClick={() => onAction("offline", expert)}
            >
              下架
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
      <p className="skill-card-meta">
        共 {expert.revision_count} 个版本 · 更新于 {formatDateTime(expert.latest_revision?.updated_at)}
      </p>

      {confirmOpen && (
        <div className="confirm-strip" role="alert">
          <span>
            确认删除「{expert.name}」？全部历史版本将一并移除；被任务引用时无法删除。
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
  const { v2User } = useAuth();
  const isAuthor = Boolean(v2User?.entitlements?.includes("expert_author"));
  const [experts, setExperts] = useState([]);
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [pageNotice, setPageNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirmId, setConfirmId] = useState(null);

  const loadExperts = useCallback(async () => {
    setIsLoading(true);
    setLoadError("");
    try {
      const result = await requestV2(V2_AUTHORING_EXPERTS);
      setExperts(result.data ?? []);
    } catch (error) {
      setLoadError(error.message || "加载失败，请稍后重试");
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (isAuthor) {
      loadExperts();
    }
  }, [isAuthor, loadExperts]);

  function patchExpert(entity) {
    // offline 响应 {entity}（brief 无 name）：仅回写实体级字段，本地展示字段保留
    setExperts((current) =>
      current.map((row) =>
        row.id === entity.id
          ? {
              ...row,
              status: entity.status,
              published_revision_id: entity.published_revision_id,
              updated_at: entity.updated_at,
            }
          : row
      )
    );
  }

  async function handleAction(action, expert) {
    setPageNotice("");
    if (action === "delete") {
      setConfirmId(expert.id);
      return;
    }
    if (action === "delete-cancel") {
      setConfirmId(null);
      return;
    }

    setBusy(true);
    try {
      if (action === "delete-confirm") {
        await requestV2(`${V2_AUTHORING_EXPERTS}/${expert.id}`, {
          method: "DELETE",
          idempotencyKey: newIdempotencyKey(),
        });
        setExperts((current) => current.filter((row) => row.id !== expert.id));
        setConfirmId(null);
      } else if (action === "offline") {
        const result = await requestV2(`${V2_AUTHORING_EXPERTS}/${expert.id}/offline`, {
          method: "POST",
          idempotencyKey: newIdempotencyKey(),
        });
        patchExpert(result.data.entity);
        setPageNotice(`「${expert.name}」已下架，专家中心立即不可见。`);
      }
    } catch (error) {
      setPageNotice(error.message || "操作失败，请稍后重试");
      if (error?.code === "ENTITY_IN_USE") {
        // 引用计数是确定性拒绝（不可重试），关闭确认条避免「再点一次」误导
        setConfirmId(null);
      }
    } finally {
      setBusy(false);
    }
  }

  if (!isAuthor) {
    return <AuthorGate />;
  }

  return (
    <main className="app-main">
      <header className="page-header rise">
        <h1 className="page-title">我的专家</h1>
        <p className="page-sub">创建并管理你的专家：保存草稿、提交审核，通过后进入专家中心。</p>
      </header>

      <div className="manage-toolbar rise" style={{ "--rise-index": 1 }}>
        <span className="manage-count">{isLoading ? "" : `共 ${experts.length} 位专家`}</span>
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
          点击「新建专家」创建第一位专家：填写人设与方法论并保存草稿，提交审核通过后
          即可在专家中心被召唤。
        </div>
      ) : (
        <div className="skill-list">
          {experts.map((expert) => (
            <ExpertRow
              key={expert.id}
              expert={expert}
              busy={busy}
              confirmOpen={confirmId === expert.id}
              onAction={handleAction}
            />
          ))}
        </div>
      )}
    </main>
  );
}
