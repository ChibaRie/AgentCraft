import { act, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { V2ApiError } from "../api/v2/client.js";
import AdminReasonPrompt, {
  ADMIN_DOMAIN_409_COPY,
  IDEMPOTENCY_CONFLICT_COPY,
} from "./AdminReasonPrompt.jsx";

// AdminReasonPrompt 统一 reason 弹窗编排（Phase 8 T12a，测试清单③⑧）：
// R3 语义——失败重试不换键不换 reason（保留输入），显式重开弹窗才换键；
// ⑧ 409 按 error.code 分流——IDEMPOTENCY_CONFLICT 走幂等冲突文案，
// 域 409（USER_STATUS_CONFLICT 等）走域文案，绝不误用幂等冲突文案（安全 Minor 3）。

/** 受控宿主：open 态真实可控（重开换键语义需要真实开关循环） */
function Host({ onSubmit, open: initialOpen = true }) {
  const [open, setOpen] = useState(initialOpen);
  return (
    <div>
      <button type="button" onClick={() => setOpen(true)}>
        重开弹窗
      </button>
      <AdminReasonPrompt
        open={open}
        title="测试操作"
        description="该操作将写入审计"
        confirmLabel="确认执行"
        onSubmit={onSubmit}
        onClose={() => setOpen(false)}
      />
    </div>
  );
}

const REASON_LABEL = "操作原因（必填，审计留痕）";

async function fillReasonAndConfirm(reason) {
  fireEvent.change(screen.getByLabelText(REASON_LABEL), { target: { value: reason } });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
  });
}

beforeEach(() => {
  vi.resetAllMocks();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("reason 收集与必填校验", () => {
  it("空 reason（含纯空白）→ 不提交、提示必填", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(<Host onSubmit={onSubmit} />);

    await fillReasonAndConfirm("   ");
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByRole("alert").textContent).toContain("请填写操作原因");
  });

  it("reason 输入上限 2000 字符（maxLength 硬约束）", () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(<Host onSubmit={onSubmit} />);
    expect(screen.getByLabelText(REASON_LABEL).getAttribute("maxlength")).toBe("2000");
  });

  it("open=false 不渲染弹窗", () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(
      <AdminReasonPrompt
        open={false}
        title="x"
        onSubmit={onSubmit}
        onClose={vi.fn()}
      />
    );
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("R3 幂等键语义（③）", () => {
  it("失败重试：同键、reason 保留（输入不清空）", async () => {
    const onSubmit = vi
      .fn()
      .mockRejectedValueOnce(new V2ApiError("VALIDATION_ERROR", "服务端拒绝", 400))
      .mockResolvedValueOnce(undefined);
    const onClose = vi.fn();
    render(
      <AdminReasonPrompt open title="x" onSubmit={onSubmit} onClose={onClose} />
    );

    await fillReasonAndConfirm("暂停违规账号");
    expect(onSubmit).toHaveBeenCalledTimes(1);
    const firstKey = onSubmit.mock.calls[0][0].idempotencyKey;
    expect(firstKey).toBeTruthy();
    // 失败：弹窗保持打开、reason 保留
    expect(screen.getByRole("alert").textContent).toContain("服务端拒绝");
    expect(screen.getByLabelText(REASON_LABEL).value).toBe("暂停违规账号");

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
    });
    expect(onSubmit).toHaveBeenCalledTimes(2);
    expect(onSubmit.mock.calls[1][0]).toEqual({
      reason: "暂停违规账号",
      idempotencyKey: firstKey,
    });
    // 成功 → 关闭回调
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("显式重开弹窗 → 生成新键（R3：重开才换键）", async () => {
    const onSubmit = vi
      .fn()
      .mockRejectedValueOnce(new V2ApiError("VALIDATION_ERROR", "失败", 400))
      .mockResolvedValueOnce(undefined);
    render(<Host onSubmit={onSubmit} />);

    await fillReasonAndConfirm("第一次原因");
    const firstKey = onSubmit.mock.calls[0][0].idempotencyKey;

    // 关闭（onClose → open=false）后重开
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    fireEvent.click(screen.getByRole("button", { name: "重开弹窗" }));
    await fillReasonAndConfirm("第二次原因");

    expect(onSubmit).toHaveBeenCalledTimes(2);
    const secondKey = onSubmit.mock.calls[1][0].idempotencyKey;
    expect(secondKey).toBeTruthy();
    expect(secondKey).not.toBe(firstKey);
  });

  it("失败后修改 reason 再提交 → 换新键（reason 参与 request_hash，防 409）", async () => {
    const onSubmit = vi
      .fn()
      .mockRejectedValueOnce(new V2ApiError("VALIDATION_ERROR", "失败", 400))
      .mockResolvedValueOnce(undefined);
    render(
      <AdminReasonPrompt open title="x" onSubmit={onSubmit} onClose={vi.fn()} />
    );

    await fillReasonAndConfirm("原因 A");
    await fillReasonAndConfirm("原因 B");

    expect(onSubmit).toHaveBeenCalledTimes(2);
    expect(onSubmit.mock.calls[0][0].reason).toBe("原因 A");
    expect(onSubmit.mock.calls[1][0].reason).toBe("原因 B");
    expect(onSubmit.mock.calls[1][0].idempotencyKey).not.toBe(
      onSubmit.mock.calls[0][0].idempotencyKey
    );
  });
});

describe("409 按 error.code 分流（⑧，安全 Minor 3）", () => {
  it("IDEMPOTENCY_CONFLICT → 固定幂等冲突文案（服务端 message 不直出）", async () => {
    const onSubmit = vi
      .fn()
      .mockRejectedValue(
        new V2ApiError("IDEMPOTENCY_CONFLICT", "幂等冲突", 409)
      );
    render(<AdminReasonPrompt open title="x" onSubmit={onSubmit} onClose={vi.fn()} />);

    await fillReasonAndConfirm("原因");
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toBe(IDEMPOTENCY_CONFLICT_COPY);
    expect(alert.textContent).not.toContain("幂等冲突");
  });

  it.each([
    "USER_STATUS_CONFLICT",
    "ENTITLEMENT_ACTIVE",
    "INVITATION_INVALID",
    "INVITATION_CONSUMED",
  ])("域 409 %s → 域文案，不走幂等冲突文案", async (code) => {
    const onSubmit = vi
      .fn()
      .mockRejectedValue(new V2ApiError(code, "服务端原文", 409));
    render(<AdminReasonPrompt open title="x" onSubmit={onSubmit} onClose={vi.fn()} />);

    await fillReasonAndConfirm("原因");
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toBe(ADMIN_DOMAIN_409_COPY[code]);
    expect(alert.textContent).not.toBe(IDEMPOTENCY_CONFLICT_COPY);
  });

  it("其它 V2ApiError → 直出服务端 message；非 V2ApiError → 兜底文案", async () => {
    const onSubmit = vi
      .fn()
      .mockRejectedValueOnce(new V2ApiError("VALIDATION_ERROR", "天数越界", 400))
      .mockRejectedValueOnce(new Error("boom"));
    render(<AdminReasonPrompt open title="x" onSubmit={onSubmit} onClose={vi.fn()} />);

    await fillReasonAndConfirm("原因");
    expect(screen.getByRole("alert").textContent).toBe("天数越界");

    await fillReasonAndConfirm("原因");
    expect(screen.getByRole("alert").textContent).toBe("操作失败，请稍后重试");
  });
});

describe("提交闸门与取消", () => {
  it("提交中：确认/取消均禁用（防重入、防中断观感）", async () => {
    let resolveSubmit;
    const onSubmit = vi.fn(
      () => new Promise((resolve) => (resolveSubmit = resolve))
    );
    render(<AdminReasonPrompt open title="x" onSubmit={onSubmit} onClose={vi.fn()} />);

    fireEvent.change(screen.getByLabelText(REASON_LABEL), { target: { value: "原因" } });
    fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
    await act(async () => {});
    // 提交中按钮文案切换为「提交中…」且禁用，取消同步禁用
    expect(screen.getByRole("button", { name: "提交中…" }).disabled).toBe(true);
    expect(screen.getByRole("button", { name: "取消" }).disabled).toBe(true);

    await act(async () => {
      resolveSubmit();
    });
  });

  it("取消 → onClose 调用（未提交）", () => {
    const onClose = vi.fn();
    render(<AdminReasonPrompt open title="x" onSubmit={vi.fn()} onClose={onClose} />);
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
