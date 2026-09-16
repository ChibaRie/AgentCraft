import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import AdminReasonPrompt from "../../components/AdminReasonPrompt.jsx";
import { useAdminGate } from "../../components/RequireAdmin.jsx";
import { buildArtifactDownloadUrl, listAuditLogs } from "../../api/v2/admin.js";

const FALLBACK_MESSAGE = "加载失败，请稍后重试";

/** 分页常量（列表查询 1≤size≤100；本页固定首页 20 条） */
const AUDIT_PAGE = 1;
const AUDIT_SIZE = 20;

const EMPTY_FILTERS = {
  actorId: "",
  action: "",
  targetType: "",
  targetId: "",
  since: "",
  until: "",
};

function describeError(error) {
  return error instanceof Error ? error.message : FALLBACK_MESSAGE;
}

/** detail JSON 树节点：键值全部为字面文本（text-only 纪律） */
function JsonNode({ name, value }) {
  const label = name !== null ? <span className="admin-tree-key">{name}：</span> : null;
  if (value === null || value === undefined) {
    return (
      <div>
        {label}
        <span className="admin-tree-leaf">null</span>
      </div>
    );
  }
  if (Array.isArray(value)) {
    return (
      <div className="admin-tree-node">
        <div>
          {label}[{value.length} 项]
        </div>
        <div className="admin-tree-children">
          {value.map((item, index) => (
            <JsonNode key={index} name={String(index)} value={item} />
          ))}
        </div>
      </div>
    );
  }
  if (typeof value === "object") {
    const entries = Object.entries(value);
    return (
      <div className="admin-tree-node">
        <div>
          {label}（{entries.length} 键）
        </div>
        <div className="admin-tree-children">
          {entries.map(([key, item]) => (
            <JsonNode key={key} name={key} value={item} />
          ))}
        </div>
      </div>
    );
  }
  return (
    <div>
      {label}
      <span className="admin-tree-leaf">{String(value)}</span>
    </div>
  );
}

/** 审计详情 JSON 树（detail 原样 JSONB，零内容改写零 HTML 解析） */
function DetailTree({ detail }) {
  return (
    <div className="admin-pre" aria-label="审计详情">
      {detail === null || detail === undefined ? (
        "—"
      ) : (
        <JsonNode name={null} value={detail} />
      )}
    </div>
  );
}

/**
 * 审计查询页（Phase 8 T12b Step 4）。
 *
 * 六维过滤（actor_id/action/target_type/target_id/since/until 全可选，naive
 * 时间戳后端视为 UTC 双端闭区间）+ created_at DESC 稳定序列表 + detail JSON 树
 * （文本渲染）。产物下载（D7h）：reason 弹窗（R7 知悉注记——直链 URL 查询串
 * 携带操作原因）→ buildArtifactDownloadUrl 直链 <a> 点击下载，仅 detail 携带
 * task_id+file_id 的行（task.artifact.download 审计形态）出示按钮。
 */
export default function AuditPage() {
  const gate = useAdminGate();
  const gateRef = useRef(gate);
  gateRef.current = gate;

  const [filters, setFilters] = useState(EMPTY_FILTERS);

  // latest-call 守卫（Phase 9 T7）：筛选逐键变化会并发多次查询，乱序响应可能
  // 以旧结果覆盖新筛选；只接受序号最新的一次落状态。
  const loadSeqRef = useRef(0);
  const [list, setList] = useState(null);
  const [loading, setLoading] = useState(true);
  const [alert, setAlert] = useState("");
  const [expandedId, setExpandedId] = useState(null);
  const [downloadTarget, setDownloadTarget] = useState(null);

  const load = useCallback(async () => {
    const seq = ++loadSeqRef.current;
    setLoading(true);
    setAlert("");
    try {
      const result = await listAuditLogs({
        actorId: filters.actorId || undefined,
        action: filters.action || undefined,
        targetType: filters.targetType || undefined,
        targetId: filters.targetId || undefined,
        since: filters.since || undefined,
        until: filters.until || undefined,
        page: AUDIT_PAGE,
        size: AUDIT_SIZE,
      });
      if (seq !== loadSeqRef.current) {
        return;
      }
      setList(result.data ?? { items: [], total: 0, page: AUDIT_PAGE, size: AUDIT_SIZE });
    } catch (error) {
      if (seq !== loadSeqRef.current) {
        return;
      }
      if (gateRef.current.reportAdminError(error, load)) {
        return;
      }
      setAlert(describeError(error));
    } finally {
      if (seq === loadSeqRef.current) {
        setLoading(false);
      }
    }
  }, [filters]);

  useEffect(() => {
    load();
  }, [load]);

  function handleFilterChange(key, value) {
    setFilters({ ...filters, [key]: value });
  }

  function handleSearch() {
    load();
  }

  /** admin 产物下载直链（GET FileResponse；点击即下载，不留存） */
  function triggerDownload(url) {
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  }

  function handleDownloadSubmit({ reason }) {
    const row = downloadTarget;
    if (!row) {
      return;
    }
    const url = buildArtifactDownloadUrl({
      taskId: row.detail.task_id,
      fileId: row.detail.file_id,
      reason,
    });
    triggerDownload(url);
  }

  const items = list?.items ?? [];

  return (
    <>
      <section className="profile-card rise" style={{ "--rise-index": 0 }}>
        <h2 className="profile-card-title">审计查询</h2>
        {alert ? (
          <p className="form-alert" role="alert">
            {alert}
          </p>
        ) : null}
        <div className="provider-form">
          <div className="field">
            <label className="field-label" htmlFor="audit-actor-id">
              操作者 ID
              <input
                id="audit-actor-id"
                className="field-input"
                value={filters.actorId}
                onChange={(event) => handleFilterChange("actorId", event.target.value)}
                placeholder="UUID"
              />
            </label>
          </div>
          <div className="field">
            <label className="field-label" htmlFor="audit-action">
              动作
              <input
                id="audit-action"
                className="field-input"
                value={filters.action}
                onChange={(event) => handleFilterChange("action", event.target.value)}
                placeholder="如 user.suspend"
              />
            </label>
          </div>
          <div className="field">
            <label className="field-label" htmlFor="audit-target-type">
              目标类型
              <input
                id="audit-target-type"
                className="field-input"
                value={filters.targetType}
                onChange={(event) => handleFilterChange("targetType", event.target.value)}
                placeholder="如 user / task"
              />
            </label>
          </div>
          <div className="field">
            <label className="field-label" htmlFor="audit-target-id">
              目标 ID
              <input
                id="audit-target-id"
                className="field-input"
                value={filters.targetId}
                onChange={(event) => handleFilterChange("targetId", event.target.value)}
                placeholder="UUID"
              />
            </label>
          </div>
          <div className="field">
            <label className="field-label" htmlFor="audit-since">
              起始时间
              <input
                id="audit-since"
                className="field-input"
                type="datetime-local"
                value={filters.since}
                onChange={(event) => handleFilterChange("since", event.target.value)}
              />
            </label>
          </div>
          <div className="field">
            <label className="field-label" htmlFor="audit-until">
              截止时间
              <input
                id="audit-until"
                className="field-input"
                type="datetime-local"
                value={filters.until}
                onChange={(event) => handleFilterChange("until", event.target.value)}
              />
            </label>
          </div>
          <div className="profile-security-actions">
            <button type="button" className="btn btn-primary" onClick={handleSearch}>
              查询
            </button>
          </div>
        </div>

        {loading ? <p className="page-sub">加载中…</p> : null}
        {!loading && items.length === 0 ? <p className="page-sub">无匹配审计记录</p> : null}
        {!loading && items.length > 0 ? (
          <table className="admin-table">
            <thead>
              <tr>
                <th scope="col">记录 ID</th>
                <th scope="col">时间</th>
                <th scope="col">动作</th>
                <th scope="col">操作者</th>
                <th scope="col">目标</th>
                <th scope="col">原因</th>
                <th scope="col">详情</th>
                <th scope="col">操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((row) => (
                <Fragment key={row.id}>
                  <tr>
                    <td>{row.id}</td>
                    <td>{row.created_at}</td>
                    <td>{row.action}</td>
                    <td>{row.actor_id ?? "—"}</td>
                    <td>
                      {row.target_type}
                      {row.target_id ? ` · ${row.target_id}` : ""}
                    </td>
                    <td>{row.reason}</td>
                    <td>
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm"
                        onClick={() => setExpandedId(expandedId === row.id ? null : row.id)}
                      >
                        详情
                      </button>
                    </td>
                    <td>
                      {row.detail && row.detail.task_id && row.detail.file_id ? (
                        <button
                          type="button"
                          className="btn btn-ghost btn-sm"
                          onClick={() => setDownloadTarget(row)}
                        >
                          下载产物
                        </button>
                      ) : null}
                    </td>
                  </tr>
                  {expandedId === row.id ? (
                    <tr>
                      <td colSpan={8}>
                        <DetailTree detail={row.detail} />
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
              ))}
            </tbody>
          </table>
        ) : null}
        {list ? (
          <p className="page-sub">
            共 {list.total} 条 · 第 {list.page} 页
          </p>
        ) : null}
      </section>

      <AdminReasonPrompt
        open={downloadTarget !== null}
        title="下载任务产物"
        description={
          downloadTarget
            ? `将打开任务 ${downloadTarget.detail.task_id} 产物 ${downloadTarget.detail.file_id} 的下载直链。注意：直链 URL 查询串将携带操作原因（留痕风险已登记接受）；文件仅本次点击下载，前端不留存。`
            : ""
        }
        onSubmit={handleDownloadSubmit}
        onClose={() => setDownloadTarget(null)}
      />
    </>
  );
}
