import { useEffect, useRef, useState } from "react";
import { request } from "../api/client.js";

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
 * Skill 创建/编辑弹窗（P08 SkillEditor）。`skill` 为 null 时是创建模式。
 * draft/offline 可保存任意满足字段规则的内容；published 的内容级校验由后端执行。
 */
export default function SkillEditorModal({ skill, onClose, onSaved }) {
  const [form, setForm] = useState(skill ? { ...EMPTY_FORM, ...skill } : EMPTY_FORM);
  const [errors, setErrors] = useState({});
  const [formAlert, setFormAlert] = useState("");
  const [isSaving, setIsSaving] = useState(false);
  const isSavingRef = useRef(false);
  const dialogRef = useRef(null);

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
      // 只提交表单字段，避免把 id/status/created_at 等行数据带进请求体
      const body = Object.fromEntries(FIELDS.map((field) => [field.name, form[field.name] ?? ""]));
      body.input_requirements = body.input_requirements || null;
      const payload = skill
        ? await request(`/api/skills/${skill.id}`, { method: "PUT", body: JSON.stringify(body) })
        : await request("/api/skills", { method: "POST", body: JSON.stringify(body) });
      onSaved(payload.data);
    } catch (error) {
      setFormAlert(error.message || "保存失败，请稍后重试");
    } finally {
      setSaving(false);
    }
  }

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
        aria-label={skill ? "编辑 Skill" : "新建 Skill"}
        ref={dialogRef}
      >
        <header className="modal-header">
          <h2 className="modal-title">{skill ? `编辑「${skill.name}」` : "新建 Skill"}</h2>
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
              <div className="field-error" role="alert">
                {errors[field.name]}
              </div>
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
      </div>
    </div>
  );
}
