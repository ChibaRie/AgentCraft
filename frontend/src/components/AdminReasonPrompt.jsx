import { useEffect, useRef, useState } from "react";
import { V2ApiError, newIdempotencyKey } from "../api/v2/client.js";

/** reason 长度上限（后端 >2000 → 400 VALIDATION_ERROR，_deps.py；前端同值硬约束） */
export const ADMIN_REASON_MAX_LENGTH = 2000;

/** 幂等冲突固定文案（⑧：IDEMPOTENCY_CONFLICT 不直出服务端 message） */
export const IDEMPOTENCY_CONFLICT_COPY =
  "该操作已用相同幂等键提交过不同内容（操作原因参与幂等校验）。请关闭弹窗后重新发起，系统将使用新的幂等键。";

/**
 * 域 409 字面码文案映射（⑧，安全 Minor 3：域 409 绝不走幂等冲突文案）。
 * 字面码清单=勘察报告 §2：USER_STATUS_CONFLICT（suspend/unsuspend 状态冲突）、
 * ENTITLEMENT_ACTIVE（重复授予）、INVITATION_INVALID / INVITATION_CONSUMED
 * （邀请撤销/创建冲突）。导出供页面与 T12b 治理页复用。
 */
export const ADMIN_DOMAIN_409_COPY = {
  USER_STATUS_CONFLICT: "用户状态已发生变化，请刷新列表后重试",
  ENTITLEMENT_ACTIVE: "该用户已持有同类型的生效权限，无需重复授予",
  INVITATION_INVALID: "邀请已被撤销或不存在，请刷新列表后重试",
  INVITATION_CONSUMED: "邀请已被使用，无法撤销",
};

const FALLBACK_MESSAGE = "操作失败，请稍后重试";
const REASON_REQUIRED_MESSAGE = "请填写操作原因";

function describeError(error) {
  if (error instanceof V2ApiError) {
    if (error.code === "IDEMPOTENCY_CONFLICT") {
      return IDEMPOTENCY_CONFLICT_COPY;
    }
    if (error.status === 409 && ADMIN_DOMAIN_409_COPY[error.code]) {
      return ADMIN_DOMAIN_409_COPY[error.code];
    }
    return error.message || FALLBACK_MESSAGE;
  }
  return FALLBACK_MESSAGE;
}

/**
 * admin 危险操作统一 reason 弹窗（Phase 8 T12a Step 3）。
 *
 * 编排语义（R3）：打开弹窗生成一次幂等键；提交失败 → 弹窗保持打开、reason
 * 输入保留、重试**复用同一把键**；用户显式关闭再重开才生成新键。失败后修改
 * reason 再提交会换新键——reason 原样参与 request_hash（Sup §9.11.4），同键
 * 异载荷必 409 IDEMPOTENCY_CONFLICT，按构造规避。
 *
 * 409 分流（⑧）：IDEMPOTENCY_CONFLICT → 固定幂等冲突文案；域字面码 →
 * ADMIN_DOMAIN_409_COPY；其余 V2ApiError 直出服务端 message。
 *
 * onSubmit 契约：async ({reason, idempotencyKey}) => void，业务失败须 throw
 * （成功 resolve 后本组件回调 onClose，由父组件收口 open=false）。
 */
export default function AdminReasonPrompt({
  open,
  title,
  description,
  confirmLabel = "确认执行",
  cancelLabel = "取消",
  onSubmit,
  onClose,
}) {
  const [reason, setReason] = useState("");
  const [alert, setAlert] = useState("");
  const [busy, setBusy] = useState(false);
  const idempotencyKeyRef = useRef(null);
  const lastSubmittedReasonRef = useRef(null);
  const inFlightRef = useRef(false);

  // 每次显式打开生成新键（R3：重开才换键）；挂载即 open 的首开同走此处
  useEffect(() => {
    if (open) {
      idempotencyKeyRef.current = newIdempotencyKey();
      lastSubmittedReasonRef.current = null;
      setReason("");
      setAlert("");
      setBusy(false);
      inFlightRef.current = false;
    }
  }, [open]);

  // Escape 关闭（提交中不关闭，防误触中断受理中的请求观感——DangerZone 同源）
  useEffect(() => {
    if (!open) {
      return undefined;
    }
    function handleKeyDown(event) {
      if (event.key === "Escape" && !inFlightRef.current) {
        onClose();
      }
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [open, onClose]);

  if (!open) {
    return null;
  }

  function handleClose() {
    if (inFlightRef.current) {
      return;
    }
    onClose();
  }

  async function handleConfirm() {
    if (inFlightRef.current) {
      return;
    }
    const trimmed = reason.trim();
    if (!trimmed) {
      setAlert(REASON_REQUIRED_MESSAGE);
      return;
    }
    // reason 参与 request_hash：失败后修改 reason 再提交须换新键（防 409）
    if (
      lastSubmittedReasonRef.current !== null &&
      trimmed !== lastSubmittedReasonRef.current
    ) {
      idempotencyKeyRef.current = newIdempotencyKey();
    }
    lastSubmittedReasonRef.current = trimmed;

    inFlightRef.current = true;
    setBusy(true);
    setAlert("");
    try {
      await onSubmit({ reason: trimmed, idempotencyKey: idempotencyKeyRef.current });
      inFlightRef.current = false;
      onClose();
    } catch (error) {
      inFlightRef.current = false;
      // 失败：弹窗保持打开、输入保留（R3），重试复用同键
      setAlert(describeError(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-overlay">
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby="admin-reason-title">
        <h3 id="admin-reason-title" className="modal-title">
          {title}
        </h3>
        {description ? <p className="modal-body">{description}</p> : null}
        <div className="field">
          <label className="field-label" htmlFor="admin-reason-input">
            操作原因（必填，审计留痕）
          </label>
          <textarea
            id="admin-reason-input"
            className="field-input"
            rows={3}
            maxLength={ADMIN_REASON_MAX_LENGTH}
            value={reason}
            disabled={busy}
            onChange={(event) => {
              setReason(event.target.value);
              if (alert) {
                setAlert("");
              }
            }}
          />
          {alert ? (
            <p className="form-alert" role="alert">
              {alert}
            </p>
          ) : null}
        </div>
        <div className="modal-footer">
          <button type="button" className="btn btn-ghost" onClick={handleClose} disabled={busy}>
            {cancelLabel}
          </button>
          <button
            type="button"
            className="btn btn-primary"
            onClick={handleConfirm}
            disabled={busy}
          >
            {busy ? "提交中…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
