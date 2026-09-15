import { useCallback, useEffect, useRef, useState } from "react";
import AdminReasonPrompt from "../../components/AdminReasonPrompt.jsx";
import { useAdminGate } from "../../components/RequireAdmin.jsx";
import { createInvitation, listInvitations, revokeInvitation } from "../../api/v2/admin.js";

const FALLBACK_MESSAGE = "加载失败，请稍后重试";

/** 邀请四态（后端 _STATUS_FILTERS）+ 全部（无过滤） */
const STATUS_FILTERS = [
  { value: "", label: "全部" },
  { value: "open", label: "待使用" },
  { value: "consumed", label: "已使用" },
  { value: "expired", label: "已过期" },
  { value: "revoked", label: "已撤销" },
];

const STATUS_LABELS = Object.fromEntries(
  STATUS_FILTERS.filter(({ value }) => value).map(({ value, label }) => [value, label])
);

const EXPIRES_OPTIONS = [1, 2, 3, 4, 5, 6, 7];

function statusLabel(status) {
  return STATUS_LABELS[status] ?? status;
}

/**
 * 邀请管理页（Phase 8 T12a Step 4）。
 *
 * - 创建：email + expires_in_days(1..7) 表单 → AdminReasonPrompt 收集 reason →
 *   POST /api/admin/invitations（幂等键由弹窗编排）。成功提示「激活邮件已发出」
 *   ——明文 token 只走邮件 outbox（Sup:132），UI 任何位置绝不渲染 token。
 * - 列表：四态过滤（open/consumed/expired/revoked）；撤销仅对 open/expired
 *   （过期未消费可撤，释放邮箱槽）开放，经 AdminReasonPrompt 二次确认。
 * - 403 数据面（ADMIN_MFA_REQUIRED/FORBIDDEN）上报 RequireAdmin gate 接管。
 */
export default function InvitationsPage() {
  const gate = useAdminGate();
  // latest-ref：403 上报始终取最新 gate API，且不令 load 随上下文刷新而重建
  // （避免无关 v2User 刷新触发列表重载）
  const gateRef = useRef(gate);
  gateRef.current = gate;
  const [statusFilter, setStatusFilter] = useState("");
  const [list, setList] = useState(null); // {items,total,page,size}
  const [loading, setLoading] = useState(true);
  const [alert, setAlert] = useState("");

  const [form, setForm] = useState({ email: "", expiresInDays: "7" });
  const [formAlert, setFormAlert] = useState("");
  const [createOpen, setCreateOpen] = useState(false);

  const [revokeTarget, setRevokeTarget] = useState(null); // 邀请行
  const [notice, setNotice] = useState("");

  const load = useCallback(
    async (status = statusFilter) => {
      setLoading(true);
      setAlert("");
      try {
        const result = await listInvitations({ status: status || undefined });
        setList(result.data ?? { items: [], total: 0, page: 1, size: 20 });
      } catch (error) {
        if (gateRef.current.reportAdminError(error, () => load(status))) {
          return;
        }
        setAlert(error instanceof Error ? error.message : FALLBACK_MESSAGE);
      } finally {
        setLoading(false);
      }
    },
    [statusFilter]
  );

  useEffect(() => {
    // load 随 statusFilter 重建，一次变更恰好触发一次装载
    load(statusFilter);
  }, [load, statusFilter]);

  function handleFilterChange(event) {
    setStatusFilter(event.target.value);
  }

  function handleOpenCreate() {
    const email = form.email.trim();
    if (!email || !email.includes("@")) {
      setFormAlert("请输入有效的电子邮箱");
      return;
    }
    setFormAlert("");
    setCreateOpen(true);
  }

  async function handleCreateSubmit({ reason, idempotencyKey }) {
    const result = await createInvitation({
      email: form.email.trim(),
      expiresInDays: Number(form.expiresInDays),
      reason,
      idempotencyKey,
    });
    setCreateOpen(false);
    setForm({ email: "", expiresInDays: "7" });
    // Sup:132：响应只含 id/email/expires_at——明文 token 仅经邮件投递，
    // 此处（及任何 UI 位置）绝不展示 token。
    const email = result.data?.email ?? form.email.trim();
    setNotice(`邀请已创建，激活邮件已发出至 ${email}（${result.data?.expires_at ?? ""} 前有效）。`);
    await load(statusFilter);
  }

  async function handleRevokeSubmit({ reason, idempotencyKey }) {
    const target = revokeTarget;
    await revokeInvitation({ invitationId: target.id, reason, idempotencyKey });
    setRevokeTarget(null);
    setNotice(`已撤销 ${target.email} 的邀请。`);
    await load(statusFilter);
  }

  const items = list?.items ?? [];

  return (
    <>
      <section className="profile-card rise" style={{ "--rise-index": 0 }}>
        <h2 className="profile-card-title">创建邀请</h2>
        {notice ? (
          <p className="page-sub" role="status">
            {notice}
          </p>
        ) : null}
        {formAlert ? (
          <p className="form-alert" role="alert">
            {formAlert}
          </p>
        ) : null}
        <div className="provider-form">
          <div className="field">
            <label className="field-label" htmlFor="invitation-email">
              电子邮箱
              <input
                id="invitation-email"
                className="field-input"
                type="email"
                value={form.email}
                onChange={(event) => setForm({ ...form, email: event.target.value })}
                placeholder="member@example.com"
              />
            </label>
          </div>
          <div className="field">
            <label className="field-label" htmlFor="invitation-expires">
              有效期（天）
              <select
                id="invitation-expires"
                className="field-input"
                value={form.expiresInDays}
                onChange={(event) => setForm({ ...form, expiresInDays: event.target.value })}
              >
                {EXPIRES_OPTIONS.map((days) => (
                  <option key={days} value={String(days)}>
                    {days} 天
                  </option>
                ))}
              </select>
            </label>
          </div>
          <div className="profile-security-actions">
            <button type="button" className="btn btn-primary" onClick={handleOpenCreate}>
              创建邀请
            </button>
          </div>
        </div>
      </section>

      <section className="profile-card rise" style={{ "--rise-index": 1 }}>
        <h2 className="profile-card-title">邀请列表</h2>
        {alert ? (
          <p className="form-alert" role="alert">
            {alert}
          </p>
        ) : null}
        <div className="field">
          <label className="field-label" htmlFor="invitation-status-filter">
            状态过滤
            <select
              id="invitation-status-filter"
              className="field-input"
              value={statusFilter}
              onChange={handleFilterChange}
            >
              {STATUS_FILTERS.map(({ value, label }) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
        </div>
        {loading ? (
          <p className="page-sub">加载中…</p>
        ) : items.length === 0 ? (
          <p className="page-sub">暂无邀请记录</p>
        ) : (
          <table className="admin-table">
            <thead>
              <tr>
                <th scope="col">邮箱</th>
                <th scope="col">状态</th>
                <th scope="col">创建时间</th>
                <th scope="col">过期时间</th>
                <th scope="col">使用时间</th>
                <th scope="col">撤销时间</th>
                <th scope="col">操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.id}>
                  <td>{item.email}</td>
                  <td>{statusLabel(item.status)}</td>
                  <td>{item.created_at}</td>
                  <td>{item.expires_at}</td>
                  <td>{item.consumed_at ?? "—"}</td>
                  <td>{item.revoked_at ?? "—"}</td>
                  <td>
                    {item.status === "open" || item.status === "expired" ? (
                      <button
                        type="button"
                        className="btn btn-ghost is-danger btn-sm"
                        onClick={() => setRevokeTarget(item)}
                      >
                        撤销
                      </button>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <AdminReasonPrompt
        open={createOpen}
        title="创建邀请"
        description={`将为 ${form.email.trim()} 创建激活邀请（有效期 ${form.expiresInDays} 天）。明文激活链接仅通过邮件发送给受邀人。`}
        confirmLabel="确认创建"
        onSubmit={handleCreateSubmit}
        onClose={() => setCreateOpen(false)}
      />
      <AdminReasonPrompt
        open={revokeTarget !== null}
        title="撤销邀请"
        description={
          revokeTarget
            ? `将撤销 ${revokeTarget.email} 的邀请（当前状态：${statusLabel(revokeTarget.status)}）。撤销后该邮箱可重新邀请。`
            : ""
        }
        confirmLabel="确认撤销"
        onSubmit={handleRevokeSubmit}
        onClose={() => setRevokeTarget(null)}
      />
    </>
  );
}
