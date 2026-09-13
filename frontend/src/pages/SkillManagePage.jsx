import { useCallback, useEffect, useRef, useState } from "react";
import { CheckCircle, Warning } from "@phosphor-icons/react";
import { useAuth } from "../auth/AuthContext.jsx";
import { request } from "../api/client.js";
import { formatDateTime } from "../lib/datetime.js";
import SkillEditorModal from "../components/SkillEditorModal.jsx";

const STATUS_LABELS = { draft: "草稿", published: "已发布", offline: "已下架" };

function StatusBadge({ status }) {
  return <span className={`skill-status is-${status}`}>{STATUS_LABELS[status] || status}</span>;
}

function IssueList({ issues }) {
  return (
    <ul className="issue-list">
      {issues.map((issue, index) => (
        <li className={`issue-row is-${issue.level.toLowerCase()}`} key={index}>
          <span className="issue-level">{issue.level}</span>
          <span className="issue-text">
            <strong>{issue.field}</strong> {issue.message}
          </span>
        </li>
      ))}
    </ul>
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

function SkillCard({ skill, busy, validation, binding, onAction }) {
  const isPublished = skill.status === "published";
  const report = validation?.skillId === skill.id ? validation : null;
  const experts = binding?.skillId === skill.id ? binding.experts : null;

  return (
    <article className="skill-card rise">
      <header className="skill-card-head">
        <div className="skill-card-title">
          <h3>{skill.name}</h3>
          <StatusBadge status={skill.status} />
        </div>
        <div className="skill-card-actions">
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            disabled={busy}
            onClick={() => onAction("validate", skill)}
          >
            校验
          </button>
          {isPublished ? (
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              disabled={busy}
              onClick={() => onAction("offline", skill)}
            >
              下架
            </button>
          ) : (
            <button
              type="button"
              className="btn btn-primary btn-sm"
              disabled={busy}
              onClick={() => onAction("publish", skill)}
            >
              发布
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
            className="btn btn-ghost btn-sm"
            disabled={busy}
            onClick={() => onAction("binding", skill)}
          >
            绑定情况
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
      <p className="skill-card-desc">{skill.description}</p>
      <p className="skill-card-meta">更新于 {formatDateTime(skill.updated_at)}</p>

      {skill.confirmDelete && (
        <div className="confirm-strip" role="alert">
          <span>确认删除「{skill.name}」？已绑定到专家的 Skill 无法删除。</span>
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

      {report && (
        <div
          className={`validate-report ${report.valid ? "is-valid" : "is-invalid"}`}
          role="status"
        >
          <div className="validate-report-head">
            {report.valid ? (
              <CheckCircle size={15} weight="fill" aria-hidden="true" />
            ) : (
              <Warning size={15} weight="fill" aria-hidden="true" />
            )}
            {report.valid ? "校验通过，可发布或进入绑定流程" : "校验未通过，请修正以下问题"}
          </div>
          {report.issues.length > 0 && <IssueList issues={report.issues} />}
        </div>
      )}

      {experts && (
        <div className="binding-strip" role="status">
          {experts.length === 0
            ? "尚未绑定到任何专家。发布后可在专家编辑页绑定。"
            : `已绑定 ${experts.length} 个专家：${experts.map((expert) => expert.name).join("、")}`}
        </div>
      )}
    </article>
  );
}

export default function SkillManagePage() {
  const { isReady, isAuthenticated } = useAuth();
  const [skills, setSkills] = useState([]);
  const [total, setTotal] = useState(0);
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [pageNotice, setPageNotice] = useState("");
  const [editorSkill, setEditorSkill] = useState(null); // null=关闭, {}=新建, {…}=编辑
  const [validation, setValidation] = useState(null);
  const [binding, setBinding] = useState(null);
  const [busy, setBusy] = useState(false);
  const [isImporting, setIsImporting] = useState(false);
  const fileInputRef = useRef(null);

  async function handleImportFile(event) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) {
      return;
    }
    clearInline();
    setIsImporting(true);
    try {
      const formData = new FormData();
      formData.append("file", file);
      const payload = await request("/api/skills/import", {
        method: "POST",
        body: formData,
      });
      const imported = payload.data.skill;
      setSkills((current) => [imported, ...current]);
      setTotal((current) => current + 1);
      setPageNotice(
        payload.data.files.length > 0
          ? `已导入「${imported.name}」草稿（${payload.data.files.length} 个包文件已存档）；请检查各栏目后保存。`
          : `已导入「${imported.name}」草稿；请检查 LLM 拆解结果后保存。`
      );
      setEditorSkill(imported); // 打开编辑器预审 LLM 填充结果
    } catch (error) {
      setPageNotice(error.message || "导入失败，请稍后重试");
    } finally {
      setIsImporting(false);
    }
  }

  const loadSkills = useCallback(async () => {
    setIsLoading(true);
    setLoadError("");
    try {
      // 课程级数据量：一次取 100 条上限；计数展示服务端 total
      const payload = await request("/api/skills?page=1&size=100");
      setSkills(payload.data);
      setTotal(payload.total);
    } catch (error) {
      setLoadError(error.message || "加载失败，请稍后重试");
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (isReady && isAuthenticated) {
      loadSkills();
    }
  }, [isReady, isAuthenticated, loadSkills]);

  function patchSkill(updated) {
    setSkills((current) =>
      current.map((skill) => (skill.id === updated.id ? { ...skill, ...updated } : skill))
    );
  }

  function clearInline() {
    setValidation(null);
    setBinding(null);
    setPageNotice("");
    // 删除确认条是瞬时状态：任何后续动作都不应让它在别的流程里残留
    setSkills((current) => current.map(({ confirmDelete: _flag, ...rest }) => rest));
  }

  async function handleAction(action, skill) {
    clearInline();
    if (action === "edit") {
      setEditorSkill(skill);
      return;
    }
    if (action === "delete") {
      setSkills((current) =>
        current.map((item) => ({ ...item, confirmDelete: item.id === skill.id }))
      );
      return;
    }
    if (action === "delete-cancel") {
      patchSkill({ id: skill.id, confirmDelete: false });
      return;
    }

    setBusy(true);
    try {
      if (action === "delete-confirm") {
        await request(`/api/skills/${skill.id}`, { method: "DELETE" });
        setSkills((current) => current.filter((item) => item.id !== skill.id));
        setTotal((current) => Math.max(0, current - 1));
      } else if (action === "validate") {
        const payload = await request(`/api/skills/${skill.id}/validate`, { method: "POST" });
        setValidation({ skillId: skill.id, ...payload.data });
      } else if (action === "publish") {
        const payload = await request(`/api/skills/${skill.id}/publish`, { method: "POST" });
        patchSkill(payload.data);
        setPageNotice(`「${skill.name}」已发布，可绑定到专家并启用。`);
      } else if (action === "offline") {
        const payload = await request(`/api/skills/${skill.id}/offline`, { method: "POST" });
        patchSkill(payload.data);
        setPageNotice(`「${skill.name}」已下架，不再接受新的绑定。`);
      } else if (action === "binding") {
        const payload = await request(`/api/skills/${skill.id}`);
        setBinding({ skillId: skill.id, experts: payload.data.bound_experts });
      }
    } catch (error) {
      if (error.code === "SKILL_INVALID") {
        setValidation({
          skillId: skill.id,
          valid: false,
          issues: [
            {
              field: "发布",
              rule: "invalid",
              level: "ERROR",
              message: error.message,
            },
          ],
        });
      } else {
        setPageNotice(error.message || "操作失败，请稍后重试");
      }
    } finally {
      setBusy(false);
    }
  }

  function handleSaved(saved) {
    setEditorSkill(null);
    if (skills.some((skill) => skill.id === saved.id)) {
      // 以服务端返回为准重建该行，避免残留 confirmDelete 等本地瞬时字段
      setSkills((current) =>
        current.map((skill) => (skill.id === saved.id ? saved : skill))
      );
    } else {
      setSkills((current) => [saved, ...current]);
      setTotal((current) => current + 1);
    }
    setPageNotice(`「${saved.name}」已保存。`);
  }

  return (
    <main className="app-main">
      <header className="page-header rise">
        <h1 className="page-title">Skill 管理</h1>
        <p className="page-sub">
          把能力封装为可复用的 Skill：校验通过后发布，才能绑定到你的专家。
        </p>
      </header>

      <div className="manage-toolbar rise" style={{ "--rise-index": 1 }}>
        <span className="manage-count">{isLoading ? "" : `共 ${total} 个 Skill`}</span>
        <div className="manage-toolbar-actions">
          <button
            type="button"
            className="btn btn-ghost"
            disabled={isImporting}
            onClick={() => fileInputRef.current?.click()}
            title="上传 .md 由 LLM 拆解填充，或 .zip 包（scripts/assets/references）"
          >
            {isImporting ? "导入中…" : "导入 Skill"}
          </button>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => {
              clearInline();
              setEditorSkill({});
            }}
          >
            新建 Skill
          </button>
        </div>
        <input
          ref={fileInputRef}
          type="file"
          accept=".md,.markdown,.zip"
          style={{ display: "none" }}
          aria-label="选择要导入的 Skill 文件"
          onChange={handleImportFile}
        />
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
          点击「新建 Skill」创建第一个能力包：定义角色、目标、步骤与约束，校验通过后发布，
          才能绑定到专家。
        </div>
      ) : (
        <div className="skill-list">
          {skills.map((skill) => (
            <SkillCard
              key={skill.id}
              skill={skill}
              busy={busy}
              validation={validation}
              binding={binding}
              onAction={handleAction}
            />
          ))}
        </div>
      )}

      {editorSkill !== null && (
        <SkillEditorModal
          skill={editorSkill.id ? editorSkill : null}
          onClose={() => setEditorSkill(null)}
          onSaved={handleSaved}
        />
      )}
    </main>
  );
}
