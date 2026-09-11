/**
 * 表单字段原语（FE-T3 引入于 LoginPage，FE-T4 提为共享）：
 * label 包裹控件 + 可选错误行。仅在有错时渲染 role=alert——空告警对读屏器是噪音。
 */
export default function FormField({ label, error, children }) {
  return (
    <div className="field">
      <label className="field-label">
        {label}
        {children}
      </label>
      {error ? (
        <div className="field-error" role="alert">
          {error}
        </div>
      ) : null}
    </div>
  );
}
