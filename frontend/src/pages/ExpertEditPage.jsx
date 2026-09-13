import { useCallback, useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { Plus, X } from "@phosphor-icons/react";
import { request } from "../api/client.js";
import { CATEGORY_OPTIONS, STATUS_LABELS } from "../lib/categories.js";

const FIELD_RULES = {
  name: { min: 2, max: 30, trim: true, message: "名称需为 2-30 个字符" },
  description: { min: 10, max: 100, trim: true, message: "简介需为 10-100 个字符" },
};

const TASK_EXAMPLE_MAX = 5;
const TASK_EXAMPLE_CHARS = 50;

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
  if ((name === "persona" || name === "methodology") && !value.trim()) {
    return name === "persona" ? "请填写专家人设" : "请填写专家方法论";
  }
  if (name === "avatar_url" && value && !value.startsWith("http://") && !value.startsWith("https://")) {
    return "头像必须是 http/https 链接";
  }
  return "";
}

function SkillBindingPanel({ skills, candidates, busy, onBind, onToggle, onUnbind }) {
  const [selectedSkillId, setSelectedSkillId] = useState("");
  const boundIds = new Set(skills.map((skill) => skill.id));
  // 只有已发布的 Skill 可绑定（draft/offline 会被后端 400 拒绝）
  const available = candidates.filter(
    (skill) => skill.status === "published" && !boundIds.has(skill.id)
  );

  return (
    <section className="profile-card rise" style={{ "--rise-index": 2 }} aria-label="Skill 绑定">
      <h2 className="profile-card-title">Skill 绑定</h2>
      {skills.length === 0 ? (
        <p className="detail-prose">
          还未绑定 Skill。发布专家前需至少绑定并启用一个已发布的 Skill。
        </p>
      ) : (
        <ul className="binding-list">
          {skills.map((skill) => (
            <li className="binding-row" key={skill.id}>
              <div className="binding-row-main">
                <strong>{skill.name}</strong>
                <span className={`skill-status is-${skill.status}`}>
                  {STATUS_LABELS[skill.status] || skill.status}
                </span>
              </div>
              <div className="binding-row-actions">
                <button
                  type="button"
                  className={`switch ${skill.enabled ? "is-on" : ""}`}
                  role="switch"
                  aria-checked={skill.enabled}
                  aria-label={`${skill.enabled ? "关闭" : "启用"} ${skill.name}`}
                  disabled={busy}
                  onClick={() => onToggle(skill.id, !skill.enabled)}
                >
                  <span className="switch-knob" aria-hidden="true" />
                  <span className="switch-label">{skill.enabled ? "已启用" : "未启用"}</span>
                </button>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  disabled={busy}
                  onClick={() => onUnbind(skill.id)}
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
          value={selectedSkillId}
          aria-label="选择要绑定的 Skill"
          onChange={(event) => setSelectedSkillId(event.target.value)}
        >
          <option value="">选择已发布的 Skill…</option>
          {available.map((skill) => (
            <option value={skill.id} key={skill.id}>
              {skill.name}
            </option>
          ))}
        </select>
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          disabled={busy || !selectedSkillId}
          onClick={() => {
            onBind(Number(selectedSkillId));
            setSelectedSkillId("");
          }}
        >
          <Plus size={13} aria-hidden="true" />
          绑定
        </button>
      </div>
      <p className="binding-hint">绑定默认关闭；启用是独立动作，需 Skill 仍处于已发布状态。</p>
    </section>
  );
}

export default function ExpertEditPage({ expertId }) {
  const isNew = !expertId;
  const navigate = useNavigate();
  const location = useLocation();
  const [form, setForm] = useState({
    name: "",
    description: "",
    avatar_url: "",
    category: "",
    persona: "",
    methodology: "",
  });
  const [examples, setExamples] = useState([""]);
  const [status, setStatus] = useState("draft");
  const [boundSkills, setBoundSkills] = useState([]);
  const [mySkills, setMySkills] = useState([]);
  const [errors, setErrors] = useState({});
  const [formAlert, setFormAlert] = useState("");
  const [pageNotice, setPageNotice] = useState(location.state?.notice || "");
  const [isSaving, setIsSaving] = useState(false);
  const [bindBusy, setBindBusy] = useState(false);
  const [isLoading, setIsLoading] = useState(!isNew);

  const refreshBindings = useCallback(async () => {
    if (!expertId) {
      return;
    }
    const payload = await request(`/api/experts/${expertId}`);
    setBoundSkills(payload.data.skills);
    setStatus(payload.data.status);
  }, [expertId]);

  useEffect(() => {
    if (!expertId) {
      return;
    }
    let cancelled = false;
    async function load() {
      try {
        const [detail, skills] = await Promise.all([
          request(`/api/experts/${expertId}`),
          request("/api/skills?page=1&size=100"),
        ]);
        if (cancelled) {
          return;
        }
        const data = detail.data;
        setForm({
          name: data.name,
          description: data.description,
          avatar_url: data.avatar_url || "",
          category: data.category,
          persona: data.persona,
          methodology: data.methodology,
        });
        setExamples(data.task_examples?.length ? data.task_examples : [""]);
        setStatus(data.status);
        setBoundSkills(data.skills);
        setMySkills(skills.data);
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
  }, [expertId]);

  function setField(name, value) {
    setForm((current) => ({ ...current, [name]: value }));
    setErrors((current) => ({ ...current, [name]: "" }));
    setFormAlert("");
  }

  function setExample(index, value) {
    setExamples((current) => current.map((item, i) => (i === index ? value : item)));
  }

  function buildPayload() {
    const cleanedExamples = examples.map((item) => item.trim()).filter(Boolean);
    return {
      name: form.name.trim(),
      description: form.description.trim(),
      avatar_url: form.avatar_url.trim() || null,
      category: form.category,
      persona: form.persona,
      methodology: form.methodology,
      task_examples: cleanedExamples.length > 0 ? cleanedExamples : null,
    };
  }

  function validateAll() {
    const nextErrors = {};
    for (const name of Object.keys(FIELD_RULES)) {
      const message = validateExpertField(name, form[name]);
      if (message) {
        nextErrors[name] = message;
      }
    }
    nextErrors.category = validateExpertField("category", form.category);
    nextErrors.persona = validateExpertField("persona", form.persona);
    nextErrors.methodology = validateExpertField("methodology", form.methodology);
    nextErrors.avatar_url = validateExpertField("avatar_url", form.avatar_url.trim());
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
      const body = JSON.stringify(buildPayload());
      if (isNew) {
        const payload = await request("/api/experts", { method: "POST", body });
        // 通知经由路由 state 传递：navigate 会重挂载组件，组件内 state 会丢失
        navigate(`/my-experts/${payload.data.id}/edit`, {
          replace: true,
          state: { notice: `「${payload.data.name}」已创建，绑定并启用 Skill 后即可发布。` },
        });
      } else {
        const payload = await request(`/api/experts/${expertId}`, { method: "PUT", body });
        setPageNotice("已保存。");
        setStatus(payload.data.status);
      }
    } catch (error) {
      setFormAlert(error.message || "保存失败，请稍后重试");
    } finally {
      setIsSaving(false);
    }
  }

  async function handlePublish() {
    setFormAlert("");
    setPageNotice("");
    setIsSaving(true);
    try {
      const payload = await request(`/api/experts/${expertId}/publish`, { method: "POST" });
      setStatus(payload.data.status);
      setPageNotice("专家已发布，现在可以在专家中心被看到。");
    } catch (error) {
      setPageNotice(error.message || "发布失败，请稍后重试");
    } finally {
      setIsSaving(false);
    }
  }

  async function handleOffline() {
    setFormAlert("");
    setPageNotice("");
    setIsSaving(true);
    try {
      const payload = await request(`/api/experts/${expertId}/offline`, { method: "POST" });
      setStatus(payload.data.status);
      setPageNotice("专家已下架，不再出现在专家中心。");
    } catch (error) {
      setPageNotice(error.message || "下架失败，请稍后重试");
    } finally {
      setIsSaving(false);
    }
  }

  async function withBinding(action) {
    setBindBusy(true);
    setPageNotice("");
    try {
      await action();
      await refreshBindings();
      return true;
    } catch (error) {
      // 绑定操作发生在右侧面板，错误放到页面顶部通知保证可见
      setPageNotice(error.message || "操作失败，请稍后重试");
      return false;
    } finally {
      setBindBusy(false);
    }
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
                ? "填写专家的基本信息、人设与方法论。"
                : "编辑完成后保存；发布前需至少绑定并启用一个已发布 Skill。"}
            </p>
          </div>
          {!isNew && (
            <span className={`skill-status is-${status}`}>{STATUS_LABELS[status] || status}</span>
          )}
        </div>
      </header>

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
              <div className="field-error" role="alert">
                {errors.name}
              </div>
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
              <div className="field-error" role="alert">
                {errors.category}
              </div>
            </div>
          </div>

          <div className="field">
            <label className="field-label">
              简介
              <input
                className="field-input"
                value={form.description}
                placeholder="一句话介绍这位专家"
                aria-invalid={Boolean(errors.description)}
                onChange={(event) => setField("description", event.target.value)}
              />
            </label>
            <div className="field-error" role="alert">
              {errors.description}
            </div>
          </div>

          <div className="field">
            <label className="field-label">
              头像链接（可选）
              <input
                className="field-input"
                value={form.avatar_url}
                placeholder="粘贴图片链接，或留空使用默认头像"
                aria-invalid={Boolean(errors.avatar_url)}
                onChange={(event) => setField("avatar_url", event.target.value)}
              />
            </label>
            <div className="field-error" role="alert">
              {errors.avatar_url}
            </div>
          </div>

          <div className="field">
            <label className="field-label">
              人设
              <textarea
                className="field-input field-textarea"
                rows={5}
                value={form.persona}
                placeholder="描述专家的专业身份和角色"
                aria-invalid={Boolean(errors.persona)}
                onChange={(event) => setField("persona", event.target.value)}
              />
            </label>
            <div className="field-error" role="alert">
              {errors.persona}
            </div>
          </div>

          <div className="field">
            <label className="field-label">
              方法论
              <textarea
                className="field-input field-textarea"
                rows={5}
                value={form.methodology}
                placeholder="描述专家处理问题的工作方法"
                aria-invalid={Boolean(errors.methodology)}
                onChange={(event) => setField("methodology", event.target.value)}
              />
            </label>
            <div className="field-error" role="alert">
              {errors.methodology}
            </div>
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
            <div className="field-error" role="alert">
              {errors.examples}
            </div>
          </div>

          <div className="expert-form-footer">
            <button type="submit" className="btn btn-primary" disabled={isSaving}>
              {isSaving ? "保存中…" : "保存"}
            </button>
            {!isNew && status !== "published" && (
              <button
                type="button"
                className="btn btn-ghost"
                disabled={isSaving}
                onClick={handlePublish}
              >
                发布
              </button>
            )}
            {!isNew && status === "published" && (
              <button
                type="button"
                className="btn btn-ghost"
                disabled={isSaving}
                onClick={handleOffline}
              >
                下架
              </button>
            )}
          </div>
        </form>

        {isNew ? (
          <section className="profile-card rise" style={{ "--rise-index": 2 }} aria-label="Skill 绑定">
            <h2 className="profile-card-title">Skill 绑定</h2>
            <p className="detail-prose">保存专家后即可在这里绑定并启用已发布的 Skill。</p>
          </section>
        ) : (
          <SkillBindingPanel
            skills={boundSkills}
            candidates={mySkills}
            busy={bindBusy}
            onBind={async (skillId) => {
              // SkillBindingPanel 内部在调用 onBind 前已清空自身选择
              await withBinding(() =>
                request(`/api/experts/${expertId}/skills`, {
                  method: "POST",
                  body: JSON.stringify({ skill_id: skillId }),
                })
              );
            }}
            onToggle={(skillId, enabled) =>
              withBinding(() =>
                request(`/api/experts/${expertId}/skills/${skillId}`, {
                  method: "PUT",
                  body: JSON.stringify({ enabled }),
                })
              )
            }
            onUnbind={(skillId) =>
              withBinding(() =>
                request(`/api/experts/${expertId}/skills/${skillId}`, { method: "DELETE" })
              )
            }
          />
        )}
      </div>
    </main>
  );
}
