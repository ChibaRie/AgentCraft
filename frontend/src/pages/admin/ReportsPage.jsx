import { useCallback, useEffect, useRef, useState } from "react";
import { X } from "@phosphor-icons/react";
import AdminReasonPrompt from "../../components/AdminReasonPrompt.jsx";
import { useAdminGate } from "../../components/RequireAdmin.jsx";
import {
  getTaskAdmin,
  listReports,
  listTaskFilesAdmin,
  listTaskMessagesAdmin,
  resolveReport,
} from "../../api/v2/admin.js";

const FALLBACK_MESSAGE = "加载失败，请稍后重试";

/** target_type 词表（backend/v2/models/content.py reports check_enum） */
const TARGET_TYPE_LABELS = {
  message: "任务消息",
  expert_revision: "专家版本",
  skill_revision: "技能版本",
};

/** 处置动作三选一（backend/v2/report_service.py _VALID_ACTIONS）；okForMessage
 * 表示该动作可用于 message 目标（ban/takedown 对 message 目标前端预判禁用） */
const ACTION_OPTIONS = [
  { value: "dismiss", label: "驳回举报", hint: "仅关闭举报，不做处置", okForMessage: true },
  {
    value: "takedown_revision",
    label: "下架当前发布版本",
    hint: "将目标实体回退为草稿（D15 指针反向下架）",
    okForMessage: false,
  },
  {
    value: "ban_author",
    label: "封禁作者",
    hint: "停用作者账户并级联回收（级联回执在操作后展示）",
    okForMessage: false,
  },
];

/** cascade 五计数回执展示序（Sup §9.11.8；null=重放基线非告警 R2） */
const CASCADE_FIELDS = [
  ["sessions_revoked", "已撤销会话"],
  ["tokens_invalidated", "已置废令牌"],
  ["flipped_tasks", "翻转任务"],
  ["cancelled_rounds", "取消轮次"],
  ["stopped", "停止执行"],
];

function describeError(error) {
  return error instanceof Error ? error.message : FALLBACK_MESSAGE;
}

/** 消息正文文本渲染（text-only：content 含 HTML 片段时按字面显示不执行） */
function MessageItem({ message }) {
  return (
    <div className="admin-pre">
      <strong>
        #{message.event_sequence} {message.author}
      </strong>
      {"\n"}
      {message.content}
    </div>
  );
}

/**
 * 举报队列页（Phase 8 T12b Step 2）。
 *
 * open 队列 + 处置（action 三选一；message 目标禁用 takedown_revision/ban_author
 * ——前端预判后端 400 词表）。ban_author 成功展示 suspension+cascade（cascade=
 * null 渲染重放基线提示非告警——R2）。「查看上下文」经 D8 读端点（任务快照 +
 * messages/files 内容读）：reason 弹窗（先审计后读提示语）**前置**——未提交
 * reason 前零读取请求（测试清单⑤）。举报条目不携带任务 id（message 举报 target
 * 为消息 id，后端无消息→任务解析端点）：抽屉以管理员录入的任务 id 打开。
 */
export default function ReportsPage() {
  const gate = useAdminGate();
  const gateRef = useRef(gate);
  gateRef.current = gate;

  const [list, setList] = useState(null);
  const [loading, setLoading] = useState(true);
  const [alert, setAlert] = useState("");

  // 处置编排：chooser（三选一弹窗）→ prompt（reason）→ receipt
  const [chooser, setChooser] = useState(null); // {report, action}
  const [prompt, setPrompt] = useState(null); // {report, action}
  const [receipt, setReceipt] = useState(null);

  // D8 上下文编排：{phase: "taskId"|"reason"|"drawer", taskId?, snapshot?}
  const [ctx, setCtx] = useState(null);
  const [ctxDraft, setCtxDraft] = useState("");
  const [ctxAlert, setCtxAlert] = useState("");
  const [ctxMessages, setCtxMessages] = useState([]);
  const [ctxFiles, setCtxFiles] = useState({ input: [], output: [] });

  const load = useCallback(async () => {
    setLoading(true);
    setAlert("");
    try {
      const result = await listReports({});
      setList(result.data ?? { items: [], total: 0, page: 1, size: 20 });
    } catch (error) {
      if (gateRef.current.reportAdminError(error, load)) {
        return;
      }
      setAlert(describeError(error));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  function openChooser(report) {
    setChooser({ report, action: "dismiss" });
  }

  async function handleResolveSubmit({ reason, idempotencyKey }) {
    const { report, action } = prompt;
    const result = await resolveReport({ reportId: report.id, action, reason, idempotencyKey });
    setReceipt({ action, data: result.data ?? null });
    setPrompt(null);
    await load();
  }

  async function loadContext(taskId, reason) {
    try {
      const [snapshot, messages, filesInput, filesOutput] = await Promise.all([
        getTaskAdmin(taskId),
        listTaskMessagesAdmin(taskId, { reason }),
        listTaskFilesAdmin(taskId, { direction: "input", reason }),
        listTaskFilesAdmin(taskId, { direction: "output", reason }),
      ]);
      setCtxMessages(messages.data ?? []);
      setCtxFiles({ input: filesInput.data ?? [], output: filesOutput.data ?? [] });
      setCtx({ phase: "drawer", taskId, snapshot: snapshot.data ?? null });
    } catch (error) {
      if (gateRef.current.reportAdminError(error, () => loadContext(taskId, reason))) {
        // 403 数据面已由 gate 接管（MFA 卡/重探测）；onClose 走 reason 相位清空
        return;
      }
      // 失败反馈外显（修复轮 1）：重抛给 AdminReasonPrompt——弹窗保持打开、
      // reason 输入与幂等键保留（R3），同 reason 可直接重试读取（读审计按次落行）
      throw error;
    }
  }

  function closeDrawer() {
    setCtx(null);
    setCtxMessages([]);
    setCtxFiles({ input: [], output: [] });
    setCtxAlert("");
  }

  const items = list?.items ?? [];
  const isDrawer = ctx?.phase === "drawer";

  return (
    <>
      <section className="profile-card rise" style={{ "--rise-index": 0 }}>
        <h2 className="profile-card-title">举报队列（open）</h2>
        {alert ? (
          <p className="form-alert" role="alert">
            {alert}
          </p>
        ) : null}
        {loading ? <p className="page-sub">加载中…</p> : null}
        {!loading && items.length === 0 ? <p className="page-sub">暂无 open 举报</p> : null}
        {!loading && items.length > 0 ? (
          <table className="admin-table">
            <thead>
              <tr>
                <th scope="col">举报 ID</th>
                <th scope="col">目标类型</th>
                <th scope="col">目标 ID</th>
                <th scope="col">举报理由</th>
                <th scope="col">举报时间</th>
                <th scope="col">操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((report) => (
                <tr key={report.id}>
                  <td>{report.id}</td>
                  <td>{TARGET_TYPE_LABELS[report.target_type] ?? report.target_type}</td>
                  <td>{report.target_id}</td>
                  <td>{report.reason}</td>
                  <td>{report.created_at}</td>
                  <td>
                    <div className="admin-actions">
                      <button type="button" className="btn btn-ghost btn-sm" onClick={() => openChooser(report)}>
                        处置
                      </button>
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm"
                        onClick={() => {
                          setCtxDraft("");
                          setCtx({ phase: "taskId" });
                        }}
                      >
                        查看上下文
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
        {list ? (
          <p className="page-sub">
            共 {list.total} 条 · 第 {list.page} 页
          </p>
        ) : null}

        {receipt ? (
          <section aria-label="最近处置回执" className="admin-notice">
            <h3>最近处置回执</h3>
            {receipt.data?.banned_user_id ? (
              <>
                <p>
                  已封禁作者（banned_user_id）：{receipt.data.banned_user_id} · 账户状态：
                  {receipt.data.suspension?.before_status ?? "—"} →{" "}
                  {receipt.data.suspension?.status ?? "—"}
                </p>
                {receipt.data.cascade ? (
                  <ul>
                    {CASCADE_FIELDS.map(([key, label]) => (
                      <li key={key}>
                        {label}：{receipt.data.cascade[key] ?? 0}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p>级联回执以提交时刻基线为准（重放响应为 null，非异常）。</p>
                )}
              </>
            ) : (
              <p>
                处置完成：
                {receipt.action === "dismiss"
                  ? `举报已驳回（status：${receipt.data?.status ?? "—"}）`
                  : `发布版本已下架${receipt.data?.takedown_revision_id ? `（takedown_revision_id：${receipt.data.takedown_revision_id}）` : ""}`}
                。
              </p>
            )}
          </section>
        ) : null}
      </section>

      {/* 处置三选一弹窗 */}
      {chooser ? (
        <div className="modal-overlay">
          <div className="modal" role="dialog" aria-modal="true" aria-labelledby="admin-resolve-title">
            <h3 id="admin-resolve-title" className="modal-title">
              处置举报
            </h3>
            <p className="modal-body">
              目标：{TARGET_TYPE_LABELS[chooser.report.target_type] ?? chooser.report.target_type}（
              {chooser.report.target_id}）。举报理由：{chooser.report.reason}
            </p>
            <div className="field">
              {ACTION_OPTIONS.map(({ value, label, hint, okForMessage }) => {
                const isMessageTarget = chooser.report.target_type === "message";
                const disabled = isMessageTarget && okForMessage === false;
                return (
                  <label className="admin-option-row" key={value}>
                    <input
                      type="radio"
                      name="admin-resolve-action"
                      value={value}
                      checked={chooser.action === value}
                      disabled={disabled}
                      onChange={() => setChooser({ ...chooser, action: value })}
                    />
                    {label}
                    {disabled ? (
                      <span className="field-note">（message 目标不可用）</span>
                    ) : null}
                    <br />
                    <span className="field-note">{hint}</span>
                  </label>
                );
              })}
            </div>
            <div className="modal-footer">
              <button type="button" className="btn btn-ghost" onClick={() => setChooser(null)}>
                取消
              </button>
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => {
                  setPrompt(chooser);
                  setChooser(null);
                }}
              >
                下一步
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {/* 任务 id 录入弹窗（举报条目不携带任务 id） */}
      {ctx?.phase === "taskId" ? (
        <div className="modal-overlay">
          <div className="modal" role="dialog" aria-modal="true" aria-labelledby="admin-ctx-task-title">
            <h3 id="admin-ctx-task-title" className="modal-title">
              查看任务上下文
            </h3>
            <p className="modal-body">
              举报条目不携带任务 ID（消息类举报的目标为消息 id）。请录入目标任务 id（可从审计查询的
              task.message.read 记录或用户任务列表获取）。
            </p>
            <div className="field">
              <label className="field-label" htmlFor="admin-ctx-task-id">
                任务 ID
                <input
                  id="admin-ctx-task-id"
                  className="field-input"
                  value={ctxDraft}
                  onChange={(event) => setCtxDraft(event.target.value)}
                />
              </label>
            </div>
            <div className="modal-footer">
              <button type="button" className="btn btn-ghost" onClick={() => setCtx(null)}>
                取消
              </button>
              <button
                type="button"
                className="btn btn-primary"
                disabled={ctxDraft.trim() === ""}
                onClick={() => {
                  const taskId = ctxDraft.trim();
                  setCtx({ phase: "reason", taskId });
                  setCtxDraft("");
                }}
              >
                打开任务上下文
              </button>
            </div>
          </div>
        </div>
      ) : null}

      <AdminReasonPrompt
        open={ctx?.phase === "reason"}
        title="查看任务上下文"
        description={`将对任务 ${ctx?.taskId ?? ""} 执行先审计后读（task.message.read / task.file_list.read）：读取请求的 URL 查询串携带操作原因（留痕风险已登记接受）。`}
        onSubmit={({ reason }) => loadContext(ctx.taskId, reason)}
        onClose={() =>
          // 读取成功后 ctx 已推进到 drawer 相位——仅 reason 相位取消时清空
          setCtx((current) => (current?.phase === "reason" ? null : current))
        }
      />
      <AdminReasonPrompt
        open={prompt !== null}
        title="处置举报"
        description={`动作：${ACTION_OPTIONS.find(({ value }) => value === prompt?.action)?.label ?? prompt?.action}。该操作写入审计并要求填写原因。`}
        onSubmit={handleResolveSubmit}
        onClose={() => setPrompt(null)}
      />

      {/* D8 任务上下文抽屉 */}
      {isDrawer ? (
        <>
          <div className="drawer-mask" />
          <aside className="drawer" role="dialog" aria-modal="true" aria-labelledby="admin-ctx-drawer-title">
            <div className="drawer-head">
              <h2 id="admin-ctx-drawer-title">任务上下文（{ctx.taskId}）</h2>
              <button type="button" className="drawer-close" aria-label="关闭任务上下文" onClick={closeDrawer}>
                <X size={16} aria-hidden="true" />
              </button>
            </div>
            <div className="drawer-body">
              {ctxAlert ? (
                <p className="form-alert" role="alert">
                  {ctxAlert}
                </p>
              ) : null}
              {ctx.snapshot ? (
                <section>
                  <h3>任务快照</h3>
                  <p>
                    状态 {ctx.snapshot.status ?? "—"} · 专家 {ctx.snapshot.expert?.name ?? "—"} · 模型{" "}
                    {ctx.snapshot.provider?.display_name ?? "—"} / {ctx.snapshot.provider?.model ?? "—"}
                  </p>
                </section>
              ) : null}
              <section>
                <h3>消息正文（event_sequence 升序）</h3>
                {ctxMessages.length === 0 ? (
                  <p className="page-sub">无消息记录</p>
                ) : (
                  ctxMessages.map((message) => (
                    <MessageItem key={message.id ?? message.event_sequence} message={message} />
                  ))
                )}
              </section>
              <section>
                <h3>文件元数据</h3>
                {["input", "output"].map((direction) => (
                  <div key={direction}>
                    <h4>{direction === "input" ? "任务输入文件" : "任务输出文件"}</h4>
                    {(ctxFiles[direction] ?? []).length === 0 ? (
                      <p className="page-sub">无 {direction} 文件</p>
                    ) : (
                      <ul className="admin-issue-list">
                        {ctxFiles[direction].map((file, index) => (
                          <li key={file.sha256 ?? index}>
                            {file.file_name} · {file.state} · {file.size_bytes} 字节 · sha256 {file.sha256}
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                ))}
                <p className="field-note">
                  文件仅展示元数据（内容字节不出队）；admin files 列表暂无 id 键，内联下载待 T16 契约 pass 后接入。
                </p>
              </section>
            </div>
          </aside>
        </>
      ) : null}
    </>
  );
}
