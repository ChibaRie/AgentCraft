import { useEffect, useRef, useState } from "react";
import { useAuth } from "../auth/AuthContext.jsx";
import { newIdempotencyKey, requestV2 } from "../api/v2/client.js";
import { V2_AUTHORING_SKILLS } from "../api/v2/routes.js";

// 字段边界照 §9.8.2 skill content_json（与后端 SkillContentPayload 一致；
// 写模型 extra=forbid——请求体只含下列九字段，永不携带 hash 字段）
const FIELD_RULES = {
  name: { min: 2, max: 30, trim: true, rangeMessage: "名称需为 2-30 个字符" },
  description: { min: 10, max: 200, trim: true, rangeMessage: "描述需为 10-200 个字符" },
  role: { min: 5, max: 200, trim: false, rangeMessage: "AI 角色需为 5-200 个字符" },
  use_case: { min: 20, max: 5000, trim: false, rangeMessage: "使用场景需为 20-5000 个字符" },
  goal: { min: 20, max: 5000, trim: false, rangeMessage: "任务目标需为 20-5000 个字符" },
  steps: { min: 20, max: 5000, trim: false, rangeMessage: "工作步骤需为 20-5000 个字符" },
  input_requirements: {
    min: 0,
    max: 5000,
    trim: false,
    optional: true,
    rangeMessage: "输入要求不能超过 5000 字符",
  },
  output_requirements: {
    min: 20,
    max: 5000,
    trim: false,
    rangeMessage: "输出要求需为 20-5000 个字符",
  },
  constraints: { min: 20, max: 5000, trim: false, rangeMessage: "约束需为 20-5000 个字符" },
};

const FIELDS = [
  { name: "name", label: "名称", kind: "input", placeholder: "给 Skill 起个名字" },
  { name: "description", label: "描述", kind: "input", placeholder: "一句话说明 Skill 用途" },
  { name: "role", label: "AI 角色", kind: "input", placeholder: "描述 AI 应扮演的角色" },
  { name: "use_case", label: "使用场景", kind: "textarea", placeholder: "描述什么情况下使用该 Skill" },
  { name: "goal", label: "任务目标", kind: "textarea", placeholder: "描述该 Skill 要达成的目标" },
  { name: "steps", label: "工作步骤", kind: "textarea", placeholder: "描述完成任务的工作步骤" },
  {
    name: "input_requirements",
    label: "输入要求（可选）",
    kind: "textarea",
    placeholder: "描述输入数据要求",
  },
  {
    name: "output_requirements",
    label: "输出要求",
    kind: "textarea",
    placeholder: "描述输出格式与质量要求",
  },
  { name: "constraints", label: "约束", kind: "textarea", placeholder: "描述行为限制和禁止事项" },
];

const EMPTY_FORM = Object.fromEntries(FIELDS.map((field) => [field.name, ""]));

const REQUIRED_HINTS = {
  use_case: "请填写使用场景",
  role: "请填写 AI 角色",
  goal: "请填写任务目标",
  steps: "请填写工作步骤",
  output_requirements: "请填写输出要求",
  constraints: "请填写约束",
  name: "请填写名称",
  description: "请填写描述",
};

export function validateSkillField(name, value) {
  const rule = FIELD_RULES[name];
  const text = rule.trim ? value.trim() : value;
  if (!rule.optional && !text.trim()) {
    return REQUIRED_HINTS[name] || "请填写该字段";
  }
  if (text.length < rule.min || text.length > rule.max) {
    return rule.rangeMessage;
  }
  return "";
}

/**
 * Skill 创建/编辑弹窗（V2 作者面）。`skillId` 为 null 时是创建模式；
 * 编辑模式自取 detail（GET /api/v2/skills/{id}）以最新 revision 的 content_json 回填。
 * 保存 = POST/PUT 全量 content_json（draft 覆写 / 自动新 draft 由服务端两段式裁决），
 * 幂等键必带（Sup §9.8.1 写端点家族）；提审在列表页卡片上进行，弹窗只管内容。
 */
export default function SkillEditorModal({ skillId, onClose, onSaved }) {
  const { v2User } = useAuth();
  const isAuthor = Boolean(v2User?.entitlements?.includes("expert_author"));
  const isEdit = Boolean(skillId);
  const [form, setForm] = useState(EMPTY_FORM);
  const [isLoadingSkill, setIsLoadingSkill] = useState(isEdit);
  const [errors, setErrors] = useState({});
  const [formAlert, setFormAlert] = useState("");
  const [isSaving, setIsSaving] = useState(false);
  const isSavingRef = useRef(false);
  const dialogRef = useRef(null);

  useEffect(() => {
    if (!isEdit || !isAuthor) {
      return;
    }
    let cancelled = false;
    async function load() {
      try {
        const result = await requestV2(`${V2_AUTHORING_SKILLS}/${skillId}`);
        if (cancelled) {
          return;
        }
        const revisions = result.data?.revisions ?? [];
        const latest = revisions.length > 0 ? revisions[revisions.length - 1] : null;
        const content = latest?.content_json ?? {};
        setForm({ ...EMPTY_FORM, ...content });
      } catch (error) {
        if (!cancelled) {
          setFormAlert(error.message || "加载 Skill 失败");
        }
      } finally {
        if (!cancelled) {
          setIsLoadingSkill(false);
        }
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [isEdit, isAuthor, skillId]);

  useEffect(() => {
    dialogRef.current?.querySelector("input, textarea")?.focus();
    function handleKeyDown(event) {
      if (event.key === "Escape" && !isSavingRef.current) {
        onClose();
      }
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  function setSaving(value) {
    isSavingRef.current = value;
    setIsSaving(value);
  }

  function setField(name, value) {
    setForm((current) => ({ ...current, [name]: value }));
    setErrors((current) => ({ ...current, [name]: "" }));
    setFormAlert("");
  }

  async function handleSubmit(event) {
    event.preventDefault();
    const nextErrors = {};
    for (const field of FIELDS) {
      const message = validateSkillField(field.name, form[field.name] ?? "");
      if (message) {
        nextErrors[field.name] = message;
      }
    }
    setErrors(nextErrors);
    if (Object.values(nextErrors).some(Boolean)) {
      return;
    }

    setSaving(true);
    setFormAlert("");
    try {
      // 只提交表单九字段（content_json 全量替换语义）；input_requirements 空串归 null
      const body = Object.fromEntries(FIELDS.map((field) => [field.name, form[field.name] ?? ""]));
      body.input_requirements = body.input_requirements || null;
      if (isEdit) {
        await requestV2(`${V2_AUTHORING_SKILLS}/${skillId}`, {
          method: "PUT",
          body,
          idempotencyKey: newIdempotencyKey(),
        });
      } else {
        await requestV2(V2_AUTHORING_SKILLS, {
          method: "POST",
          body,
          idempotencyKey: newIdempotencyKey(),
        });
      }
      onSaved();
    } catch (error) {
      setFormAlert(error.message || "保存失败，请稍后重试");
    } finally {
      setSaving(false);
    }
  }

  const loadedForm = !isEdit || !isLoadingSkill;

  return (
    <div
      className="modal-overlay"
      onPointerDown={(event) => {
        if (event.target === event.currentTarget && !isSavingRef.current) {
          onClose();
        }
      }}
    >
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label={isEdit ? "编辑 Skill" : "新建 Skill"}
        ref={dialogRef}
      >
        <header className="modal-header">
          <h2 className="modal-title">
            {isEdit ? (form.name ? `编辑「${form.name}」` : "编辑 Skill") : "新建 Skill"}
          </h2>
          <button
            type="button"
            className="modal-close"
            aria-label="关闭"
            disabled={isSaving}
            onClick={onClose}
          >
            ×
          </button>
        </header>
        {!loadedForm ? (
          <div className="modal-body">
            <p className="detail-prose">正在加载 Skill 内容…</p>
          </div>
        ) : (
          <form className="modal-body" onSubmit={handleSubmit} noValidate>
            <div className="form-alert" role="alert" hidden={!formAlert}>
              {formAlert}
            </div>
            {FIELDS.map((field) => (
              <div className="field" key={field.name}>
                <label className="field-label">
                  {field.label}
                  {field.kind === "input" ? (
                    <input
                      className="field-input"
                      value={form[field.name] ?? ""}
                      placeholder={field.placeholder}
                      aria-invalid={Boolean(errors[field.name])}
                      onChange={(event) => setField(field.name, event.target.value)}
                    />
                  ) : (
                    <textarea
                      className="field-input field-textarea"
                      rows={field.name === "steps" ? 4 : 3}
                      value={form[field.name] ?? ""}
                      placeholder={field.placeholder}
                      aria-invalid={Boolean(errors[field.name])}
                      onChange={(event) => setField(field.name, event.target.value)}
                    />
                  )}
                </label>
                {errors[field.name] && (
                  <div className="field-error" role="alert">
                    {errors[field.name]}
                  </div>
                )}
              </div>
            ))}
            <footer className="modal-footer">
              <button type="button" className="btn btn-ghost" onClick={onClose} disabled={isSaving}>
                取消
              </button>
              <button type="submit" className="btn btn-primary" disabled={isSaving}>
                {isSaving ? "保存中…" : "保存"}
              </button>
            </footer>
          </form>
        )}
      </div>
    </div>
  );
}
