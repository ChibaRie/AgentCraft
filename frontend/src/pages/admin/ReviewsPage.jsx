import { useCallback, useEffect, useRef, useState } from "react";
import { X } from "@phosphor-icons/react";
import AdminReasonPrompt from "../../components/AdminReasonPrompt.jsx";
import { useAdminGate } from "../../components/RequireAdmin.jsx";
import { approveRevision, listReviews, rejectRevision } from "../../api/v2/admin.js";

const FALLBACK_MESSAGE = "加载失败，请稍后重试";

/** target_type 词表（backend/v2/review_service.py _REVIEW_DOMAINS） */
const TARGET_TYPE_FILTERS = [
  { value: "", label: "全部" },
  { value: "expert_revision", label: "专家版本" },
  { value: "skill_revision", label: "技能版本" },
];

const TARGET_TYPE_LABELS = Object.fromEntries(
  TARGET_TYPE_FILTERS.map(({ value, label }) => [value, label])
);

/** auto_check 徽标（读取时后端重算，{valid, issues:[{field,rule,level,message}]}） */
function AutoCheckBadge({ autoCheck }) {
  if (!autoCheck) {
    return <span className="admin-chip">自动检查：—</span>;
  }
  return autoCheck.valid ? (
    <span className="admin-chip is-ok">自动检查通过</span>
  ) : (
    <span className="admin-chip is-bad">自动检查未通过 · {(autoCheck.issues ?? []).length} 项</span>
  );
}

function AutoCheckIssues({ issues }) {
  if (!issues || issues.length === 0) {
    return null;
  }
  return (
    <ul className="admin-issue-list">
      {issues.map((issue, index) => (
        <li key={index}>
          [{issue.level}] {issue.field} · {issue.rule}：{issue.message}
        </li>
      ))}
    </ul>
  );
}

/**
 * 审核队列页（Phase 8 T12b Step 1）。
 *
 * pending_review 双域汇流队列（元数据+内容读，§6:128 豁免读审计）。content_json
 * **text-only 渲染硬约束**（R6/安全 Minor 1）：JSON 序列化后以文本节点呈现，
 * 全页禁用 HTML 注入属性（ReviewsPage.test.jsx 静态+行为化逃逸双断言）。
 * approve/reject 经 AdminReasonPrompt（target_type 随 body）；approve 成功回执
 * 展示 published_revision_id（Phase 7 reviews.py 实际返回形状——契约 Minor 7）。
 */
export default function ReviewsPage() {
  const gate = useAdminGate();
  const gateRef = useRef(gate);
  gateRef.current = gate;

  const [filters, setFilters] = useState({ targetType: "" });
  const [list, setList] = useState(null);
  const [loading, setLoading] = useState(true);
  const [alert, setAlert] = useState("");

  const [openRevision, setOpenRevision] = useState(null);
  const [receipt, setReceipt] = useState(null);
  // 统一 reason 弹窗编排：{kind: "approve"|"reject"} | null
  const [prompt, setPrompt] = useState(null);

  // latest-call 守卫（Phase 9 T7）：筛选切换会并发多次列表请求，乱序响应可能
  // 以旧结果覆盖新筛选；只接受序号最新的一次落状态。
  const loadSeqRef = useRef(0);

  const load = useCallback(async () => {
    const seq = ++loadSeqRef.current;
    setLoading(true);
    setAlert("");
    try {
      const result = await listReviews({ targetType: filters.targetType || undefined });
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

  useEffect(() => {
    load();
  }, [load]);

  function handleSearch() {
    load();
  }

  function openContent(revision) {
    setOpenRevision(revision);
    setReceipt(null);
  }

  function closeDrawer() {
    setOpenRevision(null);
    setReceipt(null);
  }

  async function handlePromptSubmit({ reason, idempotencyKey }) {
    const revision = openRevision;
    const action = { revisionId: revision.id, targetType: revision.target_type, reason, idempotencyKey };
    if (prompt.kind === "approve") {
      const result = await approveRevision(action);
      setReceipt({ kind: "approve", data: result.data ?? null });
    } else {
      const result = await rejectRevision(action);
      setReceipt({ kind: "reject", data: result.data ?? null });
    }
    setPrompt(null);
    // 处置后该 revision 离开队列：后台刷新列表；抽屉保持打开以展示回执
    await load();
  }

  const items = list?.items ?? [];
  const targetTypeLabel = openRevision ? TARGET_TYPE_LABELS[openRevision.target_type] ?? openRevision.target_type : "";

  return (
    <>
      <section className="profile-card rise" style={{ "--rise-index": 0 }}>
        <h2 className="profile-card-title">审核队列（待审版本）</h2>
        {alert ? (
          <p className="form-alert" role="alert">
            {alert}
          </p>
        ) : null}
        <div className="provider-form">
          <div className="field">
            <label className="field-label" htmlFor="review-target-type">
              目标类型
              <select
                id="review-target-type"
                className="field-input"
                value={filters.targetType}
                onChange={(event) => setFilters({ targetType: event.target.value })}
              >
                {TARGET_TYPE_FILTERS.map(({ value, label }) => (
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
          <p className="page-sub">暂无待审版本</p>
        ) : (
          <table className="admin-table">
            <thead>
              <tr>
                <th scope="col">版本 ID</th>
                <th scope="col">目标类型</th>
                <th scope="col">版本号</th>
                <th scope="col">自动检查</th>
                <th scope="col">提交时间</th>
                <th scope="col">操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.id}>
                  <td>{item.id}</td>
                  <td>{TARGET_TYPE_LABELS[item.target_type] ?? item.target_type}</td>
                  <td>第 {item.revision_no} 版</td>
                  <td>
                    <AutoCheckBadge autoCheck={item.auto_check} />
                  </td>
                  <td>{item.created_at}</td>
                  <td>
                    <button
                      type="button"
                      className="btn btn-ghost btn-sm"
                      onClick={() => openContent(item)}
                    >
                      查看内容
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

      {openRevision ? (
        <div className="drawer-mask" />
      ) : null}
      {openRevision ? (
        <aside className="drawer" role="dialog" aria-modal="true" aria-labelledby="admin-review-drawer-title">
          <div className="drawer-head">
            <h2 id="admin-review-drawer-title">
              审核内容（{targetTypeLabel} · 第 {openRevision.revision_no} 版）
            </h2>
            <button type="button" className="drawer-close" aria-label="关闭审核内容" onClick={closeDrawer}>
              <X size={16} aria-hidden="true" />
            </button>
          </div>
          <div className="drawer-body">
            <section>
              <h3>版本信息</h3>
              <p>
                版本 ID：{openRevision.id} · 作者：{openRevision.owner_id} · 提交于{" "}
                {openRevision.created_at}
              </p>
              <p>
                <AutoCheckBadge autoCheck={openRevision.auto_check} />
              </p>
              <AutoCheckIssues issues={openRevision.auto_check?.issues} />
            </section>

            <section>
              <h3>内容全文（text-only）</h3>
              <p className="field-note">
                内容按字面文本渲染，不做任何 HTML 解析（防注入）。
              </p>
              <pre className="admin-pre">{JSON.stringify(openRevision.content_json, null, 2)}</pre>
              <p className="field-note">内容指纹：{openRevision.content_sha256}</p>
            </section>

            <section>
              <h3>处置</h3>
              <div className="profile-security-actions">
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={() => setPrompt({ kind: "approve" })}
                >
                  通过发布
                </button>
                <button
                  type="button"
                  className="btn btn-ghost is-danger"
                  onClick={() => setPrompt({ kind: "reject" })}
                >
                  驳回
                </button>
              </div>
              {receipt?.kind === "approve" && receipt.data ? (
                <div className="admin-notice" role="status">
                  <p>已发布。published_revision_id：{receipt.data.published_revision_id}</p>
                  <p>
                    实体状态：{receipt.data.entity_status} · 前一发布版本：
                    {receipt.data.previous_published_revision_id ?? "—"}
                  </p>
                </div>
              ) : null}
              {receipt?.kind === "reject" && receipt.data ? (
                <div className="admin-notice" role="status">
                  <p>已驳回（revision status：{receipt.data.status}）。实体与发布指针未被触碰。</p>
                </div>
              ) : null}
            </section>
          </div>
        </aside>
      ) : null}

      <AdminReasonPrompt
        open={prompt !== null}
        title={prompt?.kind === "approve" ? "通过并发布" : "驳回版本"}
        description={
          openRevision
            ? `目标：${TARGET_TYPE_LABELS[openRevision.target_type] ?? openRevision.target_type} 第 ${openRevision.revision_no} 版（${openRevision.id}）。target_type 随载荷提交；该操作写入审计并要求填写原因。`
            : ""
        }
        onSubmit={handlePromptSubmit}
        onClose={() => setPrompt(null)}
      />
    </>
  );
}
