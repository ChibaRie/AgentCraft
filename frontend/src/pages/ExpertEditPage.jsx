import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { Plus, X } from "@phosphor-icons/react";
import { useAuth } from "../auth/AuthContext.jsx";
import { newIdempotencyKey, requestV2 } from "../api/v2/client.js";
import { V2_AUTHORING_EXPERTS, V2_AUTHORING_SKILLS } from "../api/v2/routes.js";
import { CATEGORY_OPTIONS } from "../lib/categories.js";

const STATUS_LABELS = {
  draft: "草稿",
  published: "已发布",
  pending_review: "审核中",
  rejected: "已驳回",
};

/**
 * 提审工具固定集（前端常量）：对齐 backend/engine/platform_tools.py 的
 * container-kind 工具（@version "1"）。check_code_style（kind="harness"）
 * 静态排除——T6 服务端闸（submit tools 含 harness-kind → 400 VALIDATION_ERROR，
 * Sup §10.6）的前端对齐，安全审查 I-7。契约无作者面工具目录端点，清单即固定集。
 */
const SUBMITTABLE_TOOLS = [
  { tool_id: "read_task_file", version: "1", label: "读取任务输入文件（read_task_file）" },
  { tool_id: "write_output_file", version: "1", label: "写入产物文件（write_output_file）" },
  { tool_id: "list_task_files", version: "1", label: "列出任务输入文件（list_task_files）" },
  { tool_id: "query_task_state", version: "1", label: "查询任务状态（query_task_state）" },
];

const FIELD_RULES = {
  name: { min: 2, max: 30, trim: true, message: "名称需为 2-30 个字符" },
  description: { min: 10, max: 100, trim: true, message: "简介需为 10-100 个字符" },
};

const PERSONA_MAX = 8000;
const METHODOLOGY_MAX = 8000;
const AVATAR_URL_MAX = 512;
const TASK_EXAMPLE_MAX = 5;
const TASK_EXAMPLE_CHARS = 50;
const SKILL_REFS_MAX = 20;

function validateExpertField(name, value) {
  const rule = FIELD_RULES[name];
  if (rule) {
    const text = rule.trim ? value.trim() : value;
    if (text.length < rule.min || text.length > rule.max) {
      return rule.message;
    }
    return "";
  }
  if (name === "category" && !value) {
    return "请选择专家分类";
  }
  if (name === "persona" || name === "methodology") {
    const text = value.trim();
    if (!text) {
      return name === "persona" ? "请填写专家人设" : "请填写专家方法论";
    }
    const max = name === "persona" ? PERSONA_MAX : METHODOLOGY_MAX;
    if (text.length > max) {
      return name === "persona"
        ? `人设不能超过 ${PERSONA_MAX} 个字符`
        : `方法论不能超过 ${METHODOLOGY_MAX} 个字符`;
    }
  }
  if (name === "avatar_url" && value) {
    if (value.length > AVATAR_URL_MAX) {
      return `头像链接不能超过 ${AVATAR_URL_MAX} 个字符`;
    }
    if (!value.startsWith("http://") && !value.startsWith("https://")) {
      return "头像必须是 http/https 链接";
    }
  }
  return "";
}

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

/** 非 expert_author 整页 403 引导面（与 MyExpertsPage/SkillManagePage 同构）。 */
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

function SkillRefsPanel({ candidates, refs, busy, onToggle }) {
  const candidateById = new Map(candidates.map((skill) => [skill.id, skill]));
  const selectedIds = new Set(refs.map((ref) => ref.skill_id));
  // 既有引用不在候选集（如他人 published skill）：保留展示，防止保存时静默丢失
  const unknownRefs = refs.filter((ref) => !candidateById.has(ref.skill_id));
  const selectable = candidates.filter((skill) => skill.published_revision_id);

  return (
    <section className="profile-card rise" style={{ "--rise-index": 2 }} aria-label="Skill 引用">
      <h2 className="profile-card-title">Skill 引用</h2>
      {selectable.length === 0 && unknownRefs.length === 0 ? (
        <p className="detail-prose">还没有可引用的已发布 Skill；可先到 Skill 管理创建并提审。</p>
      ) : (
        <ul className="binding-list">
          {selectable.map((skill) => (
            <li className="binding-row" key={skill.id}>
              <label className="binding-row-main">
                <input
                  type="checkbox"
                  aria-label={skill.name}
                  checked={selectedIds.has(skill.id)}
                  disabled={busy}
                  onChange={() => onToggle(skill)}
                />
                <strong>{skill.name}</strong>
              </label>
            </li>
          ))}
          {unknownRefs.map((ref) => (
            <li className="binding-row" key={ref.skill_id}>
              <label className="binding-row-main">
                <input type="checkbox" aria-label={ref.skill_id} checked disabled readOnly />
                <strong>{ref.skill_id.slice(0, 8)}…</strong>
                <span className="binding-hint">不在候选集，保留既有引用</span>
              </label>
            </li>
          ))}
        </ul>
      )}
      <p className="binding-hint">
        仅可勾选已发布的 Skill；引用钉住其当前发布版本，最多 {SKILL_REFS_MAX} 个。
      </p>
    </section>
  );
}

export default function ExpertEditPage({ expertId }) {
  const isNew = !expertId;
  const navigate = useNavigate();
  const location = useLocation();
  const { v2User } = useAuth();
  const isAuthor = Boolean(v2User?.entitlements?.includes("expert_author"));

  const [form, setForm] = useState({
    name: "",
    description: "",
    avatar_url: "",
    category: "",
    persona: "",
    methodology: "",
  });
  const [examples, setExamples] = useState([""]);
  const [skillRefs, setSkillRefs] = useState([]);
  const [selectedTools, setSelectedTools] = useState([]);
  const [candidateSkills, setCandidateSkills] = useState([]);
  const [entityStatus, setEntityStatus] = useState(null);
  const [latestRevision, setLatestRevision] = useState(null);
  const [errors, setErrors] = useState({});
  const [formAlert, setFormAlert] = useState("");
  const [pageNotice, setPageNotice] = useState(location.state?.notice || "");
  const [isSaving, setIsSaving] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [isLoading, setIsLoading] = useState(!isNew);

  // 详情装载（仅编辑模式；先于候选装载声明——测试按声明序编排 mock 队列）
  useEffect(() => {
    if (!isAuthor || isNew) {
      return;
    }
    let cancelled = false;
    async function load() {
      try {
        const result = await requestV2(`${V2_AUTHORING_EXPERTS}/${expertId}`);
        if (cancelled) {
          return;
        }
        const data = result.data;
        const revisions = data.revisions ?? [];
        const latest = revisions.length > 0 ? revisions[revisions.length - 1] : null;
        const content = latest?.content_json ?? {};
        setEntityStatus(data.expert.status);
        setLatestRevision(latest);
        setForm({
          name: content.name ?? "",
          description: content.description ?? "",
          avatar_url: content.avatar_url ?? "",
          category: content.category ?? "",
          persona: content.persona ?? "",
          methodology: content.methodology ?? "",
        });
        setExamples(content.task_examples?.length ? content.task_examples : [""]);
        setSkillRefs((content.skill_refs ?? []).map((ref) => ({ ...ref })));
      } catch (error) {
        if (!cancelled) {
          setFormAlert(error.message || "加载专家失败");
        }
      } finally {
        if (!cancelled) {
          setIsLoading(false);
        }
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [isAuthor, isNew, expertId]);

  // 候选 Skill（全平台 published 公开枚举：Phase 9 T2 §10.11(a)，替代旧「本人
  // published 集」裁量；ref 钉 published_revision_id。失败不阻塞编辑，软提示）
  useEffect(() => {
    if (!isAuthor) {
      return;
    }
    let cancelled = false;
    async function loadCandidates() {
      try {
        const result = await requestV2(`${V2_AUTHORING_SKILLS}/public`);
        if (!cancelled) {
          setCandidateSkills(result.data ?? []);
        }
      } catch (error) {
        if (!cancelled) {
          setCandidateSkills([]);
          setFormAlert(`候选 Skill 加载失败：${error.message}。仍可保存，但无法调整引用。`);
        }
      }
    }
    loadCandidates();
    return () => {
      cancelled = true;
    };
  }, [isAuthor]);

  function setField(name, value) {
    setForm((current) => ({ ...current, [name]: value }));
    setErrors((current) => ({ ...current, [name]: "" }));
    setFormAlert("");
  }

  function setExample(index, value) {
    setExamples((current) => current.map((item, i) => (i === index ? value : item)));
  }

  function toggleSkillRef(skill) {
    setSkillRefs((current) => {
      if (current.some((ref) => ref.skill_id === skill.id)) {
        return current.filter((ref) => ref.skill_id !== skill.id);
      }
      if (current.length >= SKILL_REFS_MAX) {
        return current;
      }
      // ref 钉实体当前发布版本：approve 断言 skill_revision published，
      // latest 可能是发布后新开的 draft，不能取 latest_revision
      return [...current, { skill_id: skill.id, revision_id: skill.published_revision_id }];
    });
  }

  function toggleTool(toolId) {
    setSelectedTools((current) =>
      current.includes(toolId) ? current.filter((id) => id !== toolId) : [...current, toolId]
    );
  }

  function buildContent() {
    const cleanedExamples = examples.map((item) => item.trim()).filter(Boolean);
    return {
      name: form.name.trim(),
      description: form.description.trim(),
      category: form.category,
      avatar_url: form.avatar_url.trim() || null,
      persona: form.persona,
      methodology: form.methodology,
      task_examples: cleanedExamples,
      skill_refs: skillRefs.map(({ skill_id, revision_id }) => ({ skill_id, revision_id })),
    };
  }

  function validateAll() {
    const nextErrors = {};
    for (const name of [...Object.keys(FIELD_RULES), "category", "persona", "methodology", "avatar_url"]) {
      const message = validateExpertField(name, form[name]);
      if (message) {
        nextErrors[name] = message;
      }
    }
    if (examples.filter((item) => item.trim()).some((item) => item.trim().length > TASK_EXAMPLE_CHARS)) {
      nextErrors.examples = `任务示例每条不超过 ${TASK_EXAMPLE_CHARS} 个字符`;
    }
    setErrors(nextErrors);
    return !Object.values(nextErrors).some(Boolean);
  }

  async function handleSave(event) {
    event.preventDefault();
    setFormAlert("");
    if (!validateAll()) {
      return;
    }
    setIsSaving(true);
    try {
      const content = buildContent();
      if (isNew) {
        const result = await requestV2(V2_AUTHORING_EXPERTS, {
          method: "POST",
          body: content,
          idempotencyKey: newIdempotencyKey(),
        });
        // 通知经由路由 state 传递：navigate 会重挂载组件，组件内 state 会丢失
        navigate(`/my-experts/${result.data.entity.id}/edit`, {
          replace: true,
          state: { notice: `「${result.data.revision.content_json.name}」已创建为草稿。完善内容后提交审核。` },
        });
      } else {
        const wasDraft = latestRevision?.status === "draft";
        const result = await requestV2(`${V2_AUTHORING_EXPERTS}/${expertId}`, {
          method: "PUT",
          body: content,
          idempotencyKey: newIdempotencyKey(),
        });
        setLatestRevision(result.data.revision);
        setEntityStatus(result.data.entity.status);
        setPageNotice(
          wasDraft
            ? "已保存：草稿内容已更新。"
            : `已保存为新草稿版本（第 ${result.data.revision.revision_no} 版）。审核通过后替换线上版本。`
        );
      }
    } catch (error) {
      setFormAlert(error.message || "保存失败，请稍后重试");
    } finally {
      setIsSaving(false);
    }
  }

  async function handleSubmitReview() {
    if (!latestRevision) {
      return;
    }
    setFormAlert("");
    setPageNotice("");
    setIsSubmitting(true);
    try {
      const result = await requestV2(
        `${V2_AUTHORING_EXPERTS}/${expertId}/revisions/${latestRevision.revision_id}/submit`,
        {
          method: "POST",
          body: { tools: selectedTools.map((toolId) => ({ tool_id: toolId, version: "1" })) },
          idempotencyKey: newIdempotencyKey(),
        }
      );
      setLatestRevision(result.data.revision);
      setPageNotice(
        `已提交审核（第 ${result.data.revision.revision_no} 版）。审核通过后将在专家中心可见。`
      );
    } catch (error) {
      setPageNotice(error.message || "提交失败，请稍后重试");
    } finally {
      setIsSubmitting(false);
    }
  }

  async function handleOffline() {
    setFormAlert("");
    setPageNotice("");
    setBusy(true);
    try {
      const result = await requestV2(`${V2_AUTHORING_EXPERTS}/${expertId}/offline`, {
        method: "POST",
        idempotencyKey: newIdempotencyKey(),
      });
      setEntityStatus(result.data.entity.status);
      setPageNotice("专家已下架，专家中心立即不可见。");
    } catch (error) {
      setPageNotice(error.message || "下架失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  }

  async function handleDeleteConfirm() {
    setFormAlert("");
    setPageNotice("");
    setBusy(true);
    try {
      await requestV2(`${V2_AUTHORING_EXPERTS}/${expertId}`, {
        method: "DELETE",
        idempotencyKey: newIdempotencyKey(),
      });
      navigate("/my-experts", { state: { notice: `「${form.name}」已删除。` } });
    } catch (error) {
      setPageNotice(error.message || "删除失败，请稍后重试");
      if (error?.code === "ENTITY_IN_USE") {
        setConfirmDelete(false);
      }
    } finally {
      setBusy(false);
    }
  }

  if (!isAuthor) {
    return <AuthorGate />;
  }

  // 编辑模式：等待详情加载完成再渲染表单，避免加载结果覆盖用户正在输入的内容
  if (isLoading) {
    return (
      <main className="app-main">
        <div className="profile-card is-skeleton" aria-hidden="true">
          <div className="skeleton-line is-title" />
          <div className="skeleton-line" />
          <div className="skeleton-line" />
          <div className="skeleton-line is-short" />
        </div>
      </main>
    );
  }

  return (
    <main className="app-main">
      <header className="page-header rise">
        <div className="page-header-row">
          <div>
            <h1 className="page-title">{isNew ? "新建专家" : "编辑专家"}</h1>
            <p className="page-sub">
              {isNew
                ? "填写专家信息保存为草稿，完善后提交平台审核。"
                : "保存即全量替换草稿内容；提审后由平台审核，通过后发布。"}
            </p>
          </div>
          {!isNew && entityStatus && (
            <StatusBadge
              status={deriveStatus({ status: entityStatus, latest_revision: latestRevision })}
            />
          )}
        </div>
        {!isNew && (
          <div className="skill-card-actions">
            {entityStatus === "published" && (
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                disabled={busy}
                onClick={handleOffline}
              >
                下架
              </button>
            )}
            <button
              type="button"
              className="btn btn-ghost btn-sm is-danger"
              disabled={busy}
              onClick={() => setConfirmDelete(true)}
            >
              删除
            </button>
          </div>
        )}
      </header>

      {!isNew && confirmDelete && (
        <div className="confirm-strip" role="alert">
          <span>确认删除「{form.name}」？全部历史版本将一并移除；被任务引用时无法删除。</span>
          <span className="confirm-strip-actions">
            <button
              type="button"
              className="btn btn-primary btn-sm"
              disabled={busy}
              onClick={handleDeleteConfirm}
            >
              确认删除
            </button>
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => setConfirmDelete(false)}
            >
              取消
            </button>
          </span>
        </div>
      )}

      <div className="form-alert" role="alert" hidden={!pageNotice} style={{ marginBottom: 16 }}>
        {pageNotice}
      </div>

      <div className="expert-edit-grid">
        <form className="profile-card rise" style={{ "--rise-index": 1 }} onSubmit={handleSave} noValidate>
          <div className="form-alert" role="alert" hidden={!formAlert} style={{ marginBottom: 14 }}>
            {formAlert}
          </div>
          <div className="expert-form-row">
            <div className="field">
              <label className="field-label">
                名称
                <input
                  className="field-input"
                  value={form.name}
                  placeholder="给专家起个名字"
                  aria-invalid={Boolean(errors.name)}
                  onChange={(event) => setField("name", event.target.value)}
                />
              </label>
              {errors.name && (
                <div className="field-error" role="alert">
                  {errors.name}
                </div>
              )}
            </div>
            <div className="field">
              <label className="field-label">
                分类
                <select
                  className="field-input"
                  value={form.category}
                  aria-invalid={Boolean(errors.category)}
                  onChange={(event) => setField("category", event.target.value)}
                >
                  <option value="">选择专家所属分类</option>
                  {CATEGORY_OPTIONS.map((option) => (
                    <option value={option.value} key={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </label>
              {errors.category && (
                <div className="field-error" role="alert">
                  {errors.category}
                </div>
              )}
            </div>
          </div>

          <div className="field">
            <label className="field-label">
              简介
              <input
                className="field-input"
                value={form.description}
                placeholder="一句话介绍这位专家（10-100 字）"
                aria-invalid={Boolean(errors.description)}
                onChange={(event) => setField("description", event.target.value)}
              />
            </label>
            {errors.description && (
              <div className="field-error" role="alert">
                {errors.description}
              </div>
            )}
          </div>

          <div className="field">
            <label className="field-label">
              头像链接（可选）
              <input
                className="field-input"
                value={form.avatar_url}
                placeholder="粘贴 http/https 图片链接，留空使用默认头像"
                aria-invalid={Boolean(errors.avatar_url)}
                onChange={(event) => setField("avatar_url", event.target.value)}
              />
            </label>
            {errors.avatar_url && (
              <div className="field-error" role="alert">
                {errors.avatar_url}
              </div>
            )}
          </div>

          <div className="field">
            <label className="field-label">
              人设
              <textarea
                className="field-input field-textarea"
                rows={5}
                value={form.persona}
                placeholder="描述专家的专业身份和角色（最多 8000 字）"
                aria-invalid={Boolean(errors.persona)}
                onChange={(event) => setField("persona", event.target.value)}
              />
            </label>
            {errors.persona && (
              <div className="field-error" role="alert">
                {errors.persona}
              </div>
            )}
          </div>

          <div className="field">
            <label className="field-label">
              方法论
              <textarea
                className="field-input field-textarea"
                rows={5}
                value={form.methodology}
                placeholder="描述专家处理问题的工作方法（最多 8000 字）"
                aria-invalid={Boolean(errors.methodology)}
                onChange={(event) => setField("methodology", event.target.value)}
              />
            </label>
            {errors.methodology && (
              <div className="field-error" role="alert">
                {errors.methodology}
              </div>
            )}
          </div>

          <div className="field">
            <span className="field-label">任务示例（可选，最多 {TASK_EXAMPLE_MAX} 条）</span>
            {examples.map((example, index) => (
              <div className="task-example-row" key={index}>
                <input
                  className="field-input"
                  value={example}
                  placeholder={`示例 ${index + 1}，例如：整理本周技术周报`}
                  maxLength={TASK_EXAMPLE_CHARS + 10}
                  aria-label={`任务示例 ${index + 1}`}
                  onChange={(event) => setExample(index, event.target.value)}
                />
                {examples.length > 1 && (
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm"
                    aria-label={`删除示例 ${index + 1}`}
                    onClick={() => setExamples((current) => current.filter((_, i) => i !== index))}
                  >
                    <X size={13} aria-hidden="true" />
                  </button>
                )}
              </div>
            ))}
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              disabled={examples.length >= TASK_EXAMPLE_MAX}
              onClick={() => setExamples((current) => [...current, ""])}
            >
              <Plus size={13} aria-hidden="true" />
              添加示例
            </button>
            {errors.examples && (
              <div className="field-error" role="alert">
                {errors.examples}
              </div>
            )}
          </div>

          {!isNew && (
            <fieldset className="field">
              <legend className="field-label">提审工具（可选）</legend>
              <div className="task-example-row">
                {SUBMITTABLE_TOOLS.map((tool) => (
                  <label className="binding-row-main" key={tool.tool_id}>
                    <input
                      type="checkbox"
                      aria-label={tool.label}
                      checked={selectedTools.includes(tool.tool_id)}
                      disabled={isSubmitting}
                      onChange={() => toggleTool(tool.tool_id)}
                    />
                    <span>{tool.label}</span>
                  </label>
                ))}
              </div>
              <p className="binding-hint">
                平台内建工具不可由作者提交（提交时将被服务端拒绝）。
              </p>
            </fieldset>
          )}

          <div className="expert-form-footer">
            <button type="submit" className="btn btn-primary" disabled={isSaving}>
              {isSaving ? "保存中…" : "保存"}
            </button>
            {!isNew && latestRevision?.status === "draft" && (
              <button
                type="button"
                className="btn btn-ghost"
                disabled={isSubmitting || isSaving}
                onClick={handleSubmitReview}
              >
                {isSubmitting ? "提交中…" : "提交审核"}
              </button>
            )}
          </div>
          {!isNew && latestRevision?.status === "draft" && (
            <p className="binding-hint">提审前请先保存：审核针对最近一次保存的草稿内容。</p>
          )}
        </form>

        <SkillRefsPanel
          candidates={candidateSkills}
          refs={skillRefs}
          busy={isSaving}
          onToggle={toggleSkillRef}
        />
      </div>
    </main>
  );
}
