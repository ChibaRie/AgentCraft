import { useCallback, useEffect, useState } from "react";
import { useAuth } from "../auth/AuthContext.jsx";
import { newIdempotencyKey, requestV2 } from "../api/v2/client.js";
import { V2_AUTHORING_SKILLS } from "../api/v2/routes.js";
import { formatDateTime } from "../lib/datetime.js";
import SkillEditorModal from "../components/SkillEditorModal.jsx";

// V2 状态词表（本页局部定义：lib/categories.js 为 V1 面，Phase 8 期间不动）
const STATUS_LABELS = {
  draft: "草稿",
  published: "已发布",
  pending_review: "审核中",
  rejected: "已驳回",
};

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

/** 非 expert_author 整页 403 引导面（与 MyExpertsPage/ExpertEditPage 同构）。 */
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

function SkeletonCard() {
  return (
    <div className="skill-card is-skeleton" aria-hidden="true">
      <div className="skeleton-line is-title" />
      <div className="skeleton-line" />
      <div className="skeleton-line is-short" />
    </div>
  );
}

function SkillCard({ skill, busy, confirmOpen, onAction }) {
  const status = deriveStatus(skill);
  const canSubmit = skill.latest_revision?.status === "draft";
  return (
    <article className="skill-card rise">
      <header className="skill-card-head">
        <div className="skill-card-title">
          <h3>{skill.name}</h3>
          <StatusBadge status={status} />
        </div>
        <div className="skill-card-actions">
          {canSubmit && (
            <button
              type="button"
              className="btn btn-primary btn-sm"
              disabled={busy}
              onClick={() => onAction("submit", skill)}
            >
              提交审核
            </button>
          )}
          {skill.status === "published" && (
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              disabled={busy}
              onClick={() => onAction("offline", skill)}
            >
              下架
            </button>
          )}
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            disabled={busy}
            onClick={() => onAction("edit", skill)}
          >
            编辑
          </button>
          <button
            type="button"
            className="btn btn-ghost btn-sm is-danger"
            disabled={busy}
            onClick={() => onAction("delete", skill)}
          >
            删除
          </button>
        </div>
      </header>
      <p className="skill-card-meta">
        共 {skill.revision_count} 个版本 · 更新于 {formatDateTime(skill.latest_revision?.updated_at)}
      </p>

      {confirmOpen && (
        <div className="confirm-strip" role="alert">
          <span>
            确认删除「{skill.name}」？全部历史版本将一并移除；被专家引用时无法删除。
          </span>
          <span className="confirm-strip-actions">
            <button
              type="button"
              className="btn btn-primary btn-sm"
              disabled={busy}
              onClick={() => onAction("delete-confirm", skill)}
            >
              确认删除
            </button>
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => onAction("delete-cancel", skill)}
            >
              取消
            </button>
          </span>
        </div>
      )}
    </article>
  );
}

export default function SkillManagePage() {
  const { v2User } = useAuth();
  const isAuthor = Boolean(v2User?.entitlements?.includes("expert_author"));
  const [skills, setSkills] = useState([]);
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [pageNotice, setPageNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirmId, setConfirmId] = useState(null);
  // null=关闭, {}=新建, {id}=编辑（弹窗自取 detail 以最新 revision 回填）
  const [editorSkill, setEditorSkill] = useState(null);

  const loadSkills = useCallback(async () => {
    setIsLoading(true);
    setLoadError("");
    try {
      const result = await requestV2(V2_AUTHORING_SKILLS);
      setSkills(result.data ?? []);
    } catch (error) {
      setLoadError(error.message || "加载失败，请稍后重试");
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (isAuthor) {
      loadSkills();
    }
  }, [isAuthor, loadSkills]);

  function patchSkill(entity) {
    setSkills((current) =>
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

  async function handleAction(action, skill) {
    setPageNotice("");
    if (action === "edit") {
      setEditorSkill(skill.id ? { id: skill.id } : {});
      return;
    }
    if (action === "delete") {
      setConfirmId(skill.id);
      return;
    }
    if (action === "delete-cancel") {
      setConfirmId(null);
      return;
    }

    setBusy(true);
    try {
      if (action === "delete-confirm") {
        await requestV2(`${V2_AUTHORING_SKILLS}/${skill.id}`, {
          method: "DELETE",
          idempotencyKey: newIdempotencyKey(),
        });
        setSkills((current) => current.filter((row) => row.id !== skill.id));
        setConfirmId(null);
      } else if (action === "submit") {
        // skill 域 submit 载荷固定 {tools:[]}（工具集属专家域，skill 传 tools 400）
        const result = await requestV2(
          `${V2_AUTHORING_SKILLS}/${skill.id}/revisions/${skill.latest_revision.revision_id}/submit`,
          {
            method: "POST",
            body: { tools: [] },
            idempotencyKey: newIdempotencyKey(),
          }
        );
        const revision = result.data.revision;
        setSkills((current) =>
          current.map((row) =>
            row.id === skill.id ? { ...row, latest_revision: { ...row.latest_revision, ...revision } } : row
          )
        );
        setPageNotice(`「${skill.name}」已提交审核（第 ${revision.revision_no} 版）。`);
      } else if (action === "offline") {
        const result = await requestV2(`${V2_AUTHORING_SKILLS}/${skill.id}/offline`, {
          method: "POST",
          idempotencyKey: newIdempotencyKey(),
        });
        patchSkill(result.data.entity);
        setPageNotice(`「${skill.name}」已下架，不再被专家引用候选。`);
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

  function handleSaved() {
    setEditorSkill(null);
    // 列表行形状（list item）与 create/PUT 响应（entity+revision）不同构：
    // 整体 refetch 保证行形状单一来源
    loadSkills();
  }

  if (!isAuthor) {
    return <AuthorGate />;
  }

  return (
    <main className="app-main">
      <header className="page-header rise">
        <h1 className="page-title">Skill 管理</h1>
        <p className="page-sub">
          把能力封装为可复用的 Skill：保存草稿、提交审核，通过后即可被专家引用。
        </p>
      </header>

      <div className="manage-toolbar rise" style={{ "--rise-index": 1 }}>
        <span className="manage-count">{isLoading ? "" : `共 ${skills.length} 个 Skill`}</span>
        <button
          type="button"
          className="btn btn-primary"
          onClick={() => {
            setPageNotice("");
            setEditorSkill({});
          }}
        >
          新建 Skill
        </button>
      </div>

      <div className="form-alert" role="alert" hidden={!pageNotice} style={{ marginBottom: 16 }}>
        {pageNotice}
      </div>
      <div className="form-alert" role="alert" hidden={!loadError} style={{ marginBottom: 16 }}>
        {loadError}
      </div>

      {isLoading ? (
        <div className="skill-list">
          <SkeletonCard />
          <SkeletonCard />
          <SkeletonCard />
        </div>
      ) : skills.length === 0 ? (
        <div className="empty-state rise">
          <strong>还没有 Skill</strong>
          点击「新建 Skill」创建第一个能力包：定义角色、目标、步骤与约束，保存草稿并提交审核，
          通过后即可被专家引用。
        </div>
      ) : (
        <div className="skill-list">
          {skills.map((skill) => (
            <SkillCard
              key={skill.id}
              skill={skill}
              busy={busy}
              confirmOpen={confirmId === skill.id}
              onAction={handleAction}
            />
          ))}
        </div>
      )}

      {editorSkill !== null && (
        <SkillEditorModal
          skillId={editorSkill.id ?? null}
          onClose={() => setEditorSkill(null)}
          onSaved={handleSaved}
        />
      )}
    </main>
  );
}
