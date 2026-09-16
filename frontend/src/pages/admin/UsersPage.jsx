import { useCallback, useEffect, useRef, useState } from "react";
import { X } from "@phosphor-icons/react";
import AdminReasonPrompt from "../../components/AdminReasonPrompt.jsx";
import { useAdminGate } from "../../components/RequireAdmin.jsx";
import {
  getUserDetail,
  grantEntitlement,
  listUserTasks,
  listUsers,
  revokeEntitlement,
  suspendUser,
  unsuspendUser,
  updateUserQuotas,
} from "../../api/v2/admin.js";

const FALLBACK_MESSAGE = "加载失败，请稍后重试";

/** 用户状态词表（backend/v2/models/identity.py USER_STATUSES；deleted 为终态不入过滤） */
const STATUS_FILTERS = [
  { value: "", label: "全部" },
  { value: "pending", label: "待验证" },
  { value: "active", label: "正常" },
  { value: "suspended", label: "已停用" },
  { value: "deleting", label: "注销中" },
  { value: "deleted", label: "已注销" },
];

const STATUS_LABELS = Object.fromEntries(
  STATUS_FILTERS.map(({ value, label }) => [value, label])
);

const ENTITLEMENT_KIND = "expert_author";

const QUOTA_FIELDS = [
  { key: "max_daily_tasks", label: "每日任务上限", detailKey: "maxDailyTasks" },
  { key: "max_active_tasks", label: "活跃任务上限", detailKey: "maxActiveTasks" },
  { key: "max_running_tasks", label: "并发任务上限", detailKey: "maxRunningTasks" },
  { key: "max_retained_storage_bytes", label: "保留存储上限（字节）", detailKey: "maxRetainedStorageBytes" },
];

/** 级联五计数回执展示序（Sup §9.11.8 cascade 形状；null=重放基线非告警 R2） */
const CASCADE_FIELDS = [
  ["sessions_revoked", "已撤销会话"],
  ["tokens_invalidated", "已置废令牌"],
  ["flipped_tasks", "翻转任务"],
  ["cancelled_rounds", "取消轮次"],
  ["stopped", "停止执行"],
];

function statusLabel(status) {
  return STATUS_LABELS[status] ?? status;
}

/** strict int 表单值解析：空串=不变（undefined）；其余须为非负整数 */
function parseQuotaValue(raw) {
  if (raw === "" || raw === null || raw === undefined) {
    return { value: undefined };
  }
  if (!/^\d+$/.test(String(raw).trim())) {
    return { error: "配额必须为非负整数（空值 = 保持不变）" };
  }
  return { value: Number(raw.trim()) };
}

/**
 * 用户管理页 + 详情抽屉（Phase 8 T12a Step 5）。
 *
 * 列表：email_prefix/status 过滤（元数据读）。详情抽屉四区块：
 * ①概览（user/usage/tasks 计数）；②配额 PUT（strict int，空值=不变，
 * 仅提交填写维度——后端 exclude_unset）；③entitlements 授/撤（D4a：
 * DELETE 带 JSON body）；④suspend/unsuspend（级联五计数回执；cascade=null
 * 渲染「以提交时刻基线为准」而非告警——R2 重放基线语义）+ D8 用户任务
 * 列表（元数据读）。域 409 字面码文案由 AdminReasonPrompt 统一分流（⑧）。
 */
export default function UsersPage() {
  const gate = useAdminGate();
  const gateRef = useRef(gate);
  gateRef.current = gate;

  const [filters, setFilters] = useState({ emailPrefix: "", status: "" });
  const [list, setList] = useState(null);
  const [loading, setLoading] = useState(true);
  const [alert, setAlert] = useState("");

  const [detail, setDetail] = useState(null);
  // 抽屉首开反馈：目标行独立于 detail 保存——请求未落定前即渲染抽屉骨架，
  // 否则点击「查看详情」在首帧之后才有任何可见反馈（Phase 9 T7）
  const [detailTarget, setDetailTarget] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailAlert, setDetailAlert] = useState("");
  const [quotaDraft, setQuotaDraft] = useState({});
  const [quotaAlert, setQuotaAlert] = useState("");
  const [receipt, setReceipt] = useState(null); // suspend/unsuspend 最近回执
  const [userTasks, setUserTasks] = useState(null);
  const [tasksLoading, setTasksLoading] = useState(false);

  // 统一 reason 弹窗编排：{kind: "suspend"|"unsuspend"|"quotas"|"grant"|"revoke"} | null
  const [prompt, setPrompt] = useState(null);

  // latest-call 守卫（Phase 9 T7）：过滤条件逐键变化会并发多个列表请求，
  // 乱序响应可能以旧数据覆盖新筛选结果；只接受序号最新的一次落状态。
  const loadSeqRef = useRef(0);
  const detailSeqRef = useRef(0);

  const load = useCallback(async () => {
    const seq = ++loadSeqRef.current;
    setLoading(true);
    setAlert("");
    try {
      const result = await listUsers({
        emailPrefix: filters.emailPrefix || undefined,
        status: filters.status || undefined,
      });
      if (seq !== loadSeqRef.current) {
        return;
      }
      setList(result.data ?? { items: [], total: 0, page: 1, size: 20 });
    } catch (error) {
      if (seq !== loadSeqRef.current) {
        return;
      }
      if (gateRef.current.reportAdminError(error, load)) {
        return;
      }
      setAlert(error instanceof Error ? error.message : FALLBACK_MESSAGE);
    } finally {
      if (seq === loadSeqRef.current) {
        setLoading(false);
      }
    }
  }, [filters]);

  const loadDetail = useCallback(async (userId, { reset = true } = {}) => {
    const seq = ++detailSeqRef.current;
    setDetailLoading(true);
    setDetailAlert("");
    try {
      const result = await getUserDetail(userId);
      if (seq !== detailSeqRef.current) {
        return;
      }
      const next = result.data ?? null;
      setDetail(next);
      // 配额表单以当前值为初值（缺值留空）；提交时仅上送相对当前的改动维度
      setQuotaDraft(
        Object.fromEntries(
          QUOTA_FIELDS.map(({ key, detailKey }) => [
            detailKey,
            next?.quotas?.[key] !== undefined && next?.quotas?.[key] !== null
              ? String(next.quotas[key])
              : "",
          ])
        )
      );
      // reset（换目标用户）：级联回执与已加载任务列表一并清空；写操作成功后的
      // 刷新（reset=false）必须保留回执展示（⑤）
      if (reset) {
        setReceipt(null);
        setUserTasks(null);
      }
    } catch (error) {
      if (seq !== detailSeqRef.current) {
        return;
      }
      if (gateRef.current.reportAdminError(error, () => loadDetail(userId, { reset }))) {
        return;
      }
      setDetailAlert(error instanceof Error ? error.message : FALLBACK_MESSAGE);
    } finally {
      if (seq === detailSeqRef.current) {
        setDetailLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  function openDetail(user) {
    setDetailTarget(user);
    setDetail(null);
    loadDetail(user.id);
  }

  function closeDetail() {
    setDetailTarget(null);
    setDetail(null);
  }

  function handleSearch() {
    load();
  }

  function handleOpenQuotas() {
    // 表单以当前值预填：仅提交相对当前值的改动维度（空/未改 = 保持不变）
    const changed = {};
    for (const { key, detailKey } of QUOTA_FIELDS) {
      const parsed = parseQuotaValue(quotaDraft[detailKey]);
      if (parsed.error) {
        setQuotaAlert(parsed.error);
        return;
      }
      if (parsed.value === undefined) {
        continue;
      }
      if (detail?.quotas?.[key] === parsed.value) {
        continue;
      }
      changed[detailKey] = parsed.value;
    }
    if (Object.keys(changed).length === 0) {
      setQuotaAlert("至少修改一个配额维度（与当前值相同 = 保持不变）");
      return;
    }
    setQuotaAlert("");
    setPrompt({ kind: "quotas", quotas: changed });
  }

  async function handlePromptSubmit({ reason, idempotencyKey }) {
    const userId = detail.user.id;
    if (prompt.kind === "suspend") {
      const result = await suspendUser({ userId, reason, idempotencyKey });
      setReceipt(result.data ?? null);
    } else if (prompt.kind === "unsuspend") {
      const result = await unsuspendUser({ userId, reason, idempotencyKey });
      setReceipt(result.data ?? null);
    } else if (prompt.kind === "quotas") {
      const camel = {};
      if (prompt.quotas.maxDailyTasks !== undefined) camel.maxDailyTasks = prompt.quotas.maxDailyTasks;
      if (prompt.quotas.maxActiveTasks !== undefined) camel.maxActiveTasks = prompt.quotas.maxActiveTasks;
      if (prompt.quotas.maxRunningTasks !== undefined) camel.maxRunningTasks = prompt.quotas.maxRunningTasks;
      if (prompt.quotas.maxRetainedStorageBytes !== undefined) {
        camel.maxRetainedStorageBytes = prompt.quotas.maxRetainedStorageBytes;
      }
      await updateUserQuotas({ userId, quotas: camel, reason, idempotencyKey });
      setReceipt(null);
    } else if (prompt.kind === "grant") {
      await grantEntitlement({ userId, kind: ENTITLEMENT_KIND, reason, idempotencyKey });
      setReceipt(null);
    } else if (prompt.kind === "revoke") {
      await revokeEntitlement({ userId, kind: ENTITLEMENT_KIND, reason, idempotencyKey });
      setReceipt(null);
    }
    setPrompt(null);
    // 写操作成功后刷新详情与列表（reset=false 保留级联回执展示——⑤/R2）
    await loadDetail(userId, { reset: false });
    await load();
  }

  async function handleLoadTasks() {
    setTasksLoading(true);
    try {
      const result = await listUserTasks(detail.user.id);
      setUserTasks(result.data ?? { items: [], total: 0, page: 1, size: 20 });
    } catch (error) {
      if (gateRef.current.reportAdminError(error, handleLoadTasks)) {
        return;
      }
      setUserTasks({ items: [], total: 0, page: 1, size: 20, error: error.message });
    } finally {
      setTasksLoading(false);
    }
  }

  const items = list?.items ?? [];
  // 目标行（未落定时取列表行）——抽屉首开即渲染，落定后以详情覆盖
  const detailUser = detail?.user ?? detailTarget;
  const isSuspended = detailUser?.status === "suspended";

  return (
    <>
      <section className="profile-card rise" style={{ "--rise-index": 0 }}>
        <h2 className="profile-card-title">用户检索</h2>
        {alert ? (
          <p className="form-alert" role="alert">
            {alert}
          </p>
        ) : null}
        <div className="provider-form">
          <div className="field">
            <label className="field-label" htmlFor="user-email-prefix">
              邮箱前缀
              <input
                id="user-email-prefix"
                className="field-input"
                value={filters.emailPrefix}
                onChange={(event) => setFilters({ ...filters, emailPrefix: event.target.value })}
                placeholder="按邮箱前缀检索"
              />
            </label>
          </div>
          <div className="field">
            <label className="field-label" htmlFor="user-status-filter">
              状态过滤
              <select
                id="user-status-filter"
                className="field-input"
                value={filters.status}
                onChange={(event) => setFilters({ ...filters, status: event.target.value })}
              >
                {STATUS_FILTERS.map(({ value, label }) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <div className="profile-security-actions">
            <button type="button" className="btn btn-primary" onClick={handleSearch}>
              查询
            </button>
          </div>
        </div>
        {loading ? (
          <p className="page-sub">加载中…</p>
        ) : items.length === 0 ? (
          <p className="page-sub">暂无匹配用户</p>
        ) : (
          <table className="admin-table">
            <thead>
              <tr>
                <th scope="col">邮箱</th>
                <th scope="col">角色</th>
                <th scope="col">状态</th>
                <th scope="col">创建时间</th>
                <th scope="col">操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.id}>
                  <td>{item.email}</td>
                  <td>{item.role}</td>
                  <td>{statusLabel(item.status)}</td>
                  <td>{item.created_at}</td>
                  <td>
                    <button
                      type="button"
                      className="btn btn-ghost btn-sm"
                      onClick={() => openDetail(item)}
                    >
                      查看详情
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {list ? (
          <p className="page-sub">
            共 {list.total} 条 · 第 {list.page} 页
          </p>
        ) : null}
      </section>

      {detailUser ? (
        <div className="drawer-mask" />
      ) : null}
      {detailUser ? (
        <aside className="drawer" role="dialog" aria-modal="true" aria-labelledby="admin-user-drawer-title">
          <div className="drawer-head">
            <h2 id="admin-user-drawer-title">用户详情</h2>
            <button type="button" className="drawer-close" aria-label="关闭详情" onClick={closeDetail}>
              <X size={16} aria-hidden="true" />
            </button>
          </div>
          <div className="drawer-body">
            {detailAlert ? (
              <p className="form-alert" role="alert">
                {detailAlert}
              </p>
            ) : null}
            {detailLoading && !detail ? <p className="page-sub">加载中…</p> : null}

            <section>
              <h3>概览</h3>
              <p>
                {detailUser.email} · {detailUser.role} · {statusLabel(detailUser.status)} · 创建于{" "}
                {detailUser.created_at}
              </p>
              <p>任务总数：{detail?.tasks?.total ?? 0}</p>
              <p>
                用量：每日 {detail?.usage?.max_daily_tasks ?? 0} / 活跃 {detail?.usage?.max_active_tasks ?? 0} /
                并发 {detail?.usage?.max_running_tasks ?? 0}
              </p>
            </section>

            {/* 数据依赖区块：详情未落定前不渲染——规避 detail 为空时的操作入口 */}
            {detail ? (
              <>
            <section>
              <h3>配额调整</h3>
              {quotaAlert ? (
                <p className="form-alert" role="alert">
                  {quotaAlert}
                </p>
              ) : null}
              {QUOTA_FIELDS.map(({ key, detailKey, label }) => (
                <div className="field" key={detailKey}>
                  <label className="field-label" htmlFor={`quota-${detailKey}`}>
                    {label}
                    <input
                      id={`quota-${detailKey}`}
                      className="field-input"
                      inputMode="numeric"
                      value={quotaDraft[detailKey] ?? ""}
                      placeholder={String(detail?.quotas?.[key] ?? "—")}
                      onChange={(event) =>
                        setQuotaDraft({ ...quotaDraft, [detailKey]: event.target.value })
                      }
                    />
                  </label>
                </div>
              ))}
              <div className="profile-security-actions">
                <button type="button" className="btn btn-primary" onClick={handleOpenQuotas}>
                  保存配额
                </button>
              </div>
            </section>

            <section>
              <h3>作者权限（{ENTITLEMENT_KIND}）</h3>
              <div className="profile-security-actions">
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={() => setPrompt({ kind: "grant" })}
                >
                  授予作者权限
                </button>
                <button
                  type="button"
                  className="btn btn-ghost is-danger"
                  onClick={() => setPrompt({ kind: "revoke" })}
                >
                  撤销作者权限
                </button>
              </div>
            </section>

            <section>
              <h3>停用 / 恢复</h3>
              <div className="profile-security-actions">
                {!isSuspended ? (
                  <button
                    type="button"
                    className="btn btn-ghost is-danger"
                    onClick={() => setPrompt({ kind: "suspend" })}
                  >
                    停用账户
                  </button>
                ) : (
                  <button
                    type="button"
                    className="btn btn-primary"
                    onClick={() => setPrompt({ kind: "unsuspend" })}
                  >
                    恢复账户
                  </button>
                )}
              </div>
              {receipt ? (
                <section aria-label="最近操作回执">
                  <h4>最近操作回执</h4>
                  <p>
                    {receipt.before_status} → {receipt.status}
                  </p>
                  {receipt.cascade ? (
                    <ul>
                      {CASCADE_FIELDS.map(([key, label]) => (
                        <li key={key}>
                          {label}：{receipt.cascade[key] ?? 0}
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p>级联回执以提交时刻基线为准（重放响应为 null，非异常）。</p>
                  )}
                </section>
              ) : null}
            </section>

            <section>
              <h3>任务列表</h3>
              <div className="profile-security-actions">
                <button type="button" className="btn btn-ghost" onClick={handleLoadTasks}>
                  查看任务列表
                </button>
              </div>
              {tasksLoading ? <p className="page-sub">加载中…</p> : null}
              {userTasks ? (
                userTasks.items.length === 0 ? (
                  <p className="page-sub">该用户暂无任务</p>
                ) : (
                  <table className="admin-table">
                    <thead>
                      <tr>
                        <th scope="col">任务 ID</th>
                        <th scope="col">状态</th>
                        <th scope="col">创建时间</th>
                        <th scope="col">中止原因</th>
                      </tr>
                    </thead>
                    <tbody>
                      {userTasks.items.map((task) => (
                        <tr key={task.id}>
                          <td>{task.id}</td>
                          <td>{task.status}</td>
                          <td>{task.created_at}</td>
                          <td>{task.abort_reason ?? "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )
              ) : null}
            </section>
              </>
            ) : null}
          </div>
        </aside>
      ) : null}

      <AdminReasonPrompt
        open={prompt !== null}
        title={
          prompt?.kind === "suspend"
            ? "停用账户"
            : prompt?.kind === "unsuspend"
              ? "恢复账户"
              : prompt?.kind === "quotas"
                ? "调整配额"
                : prompt?.kind === "grant"
                  ? "授予作者权限"
                  : "撤销作者权限"
        }
        description={
          detailUser
            ? `目标用户：${detailUser.email}。该操作写入审计并要求填写原因。${
                prompt?.kind === "suspend"
                  ? "停用将撤销全部会话并回收任务（级联回执在操作后展示）。"
                  : ""
              }`
            : ""
        }
        onSubmit={handlePromptSubmit}
        onClose={() => setPrompt(null)}
      />
    </>
  );
}
