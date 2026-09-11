import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Devices } from "@phosphor-icons/react";
import { useAuth } from "../auth/AuthContext.jsx";
import { V2ApiError, newIdempotencyKey, requestV2 } from "../api/v2/client.js";
import { V2_AUTH } from "../api/v2/routes.js";
import { formatDateTime } from "../lib/datetime.js";

const FALLBACK_MESSAGE = "操作失败，请稍后重试";
const LOAD_FAILED_MESSAGE = "会话列表加载失败";

/**
 * 设备与登录卡片（FE-T7，契约 = F2 §1.10）。
 *
 * 后端契约（只读参考）：GET /auth/sessions → {data:[{id, device_label,
 * created_at, expires_at, current}], total, page, size}——requestV2 只回 data
 * 数组（信封顶层 total/page/size 被丢弃，勿取）。DELETE /auth/sessions/{id}
 * 幂等义务端点（Idempotency-Key 必带）；rowcount 0 → 统一 404 NOT_FOUND
 * （他人会话/不存在/已撤销一律 404，竞态语义）。
 *
 * 行为：
 * - 挂载拉取列表；行渲染 device_label ?? 「未知设备」+ created_at/expires_at
 *   经 datetime 格式化；current 行挂「本机」badge + 按钮「仅退出本机」，
 *   其余行「退出」。
 * - 退出 → DELETE（幂等键，用户触发提交时生成）→ 200：非本机行静默刷新列表；
 *   本机行 = 本机登出——清 V2 本地会话态（csrf + v2User）后跳 /login
 *   （导航由调用方负责；cookie 已被后端清除）。
 * - 404 → 静默刷新列表（会话已被其他途径撤销的竞态），不展示错误；此时若
 *   撤销的恰是本机会话，刷新请求自然 401 → 事件订阅收口跳登录。
 * - 其余失败 → 内联后端文案（网络层裸错误兜底）。
 */
export default function SessionsCard() {
  const { clearV2Session } = useAuth();
  const navigate = useNavigate();
  const [sessions, setSessions] = useState(null); // null = 加载中
  const [loadError, setLoadError] = useState("");
  const [alert, setAlert] = useState("");
  const [busyId, setBusyId] = useState(null);

  async function loadSessions() {
    try {
      const result = await requestV2(`${V2_AUTH}/sessions`);
      setSessions(Array.isArray(result.data) ? result.data : []);
      setLoadError("");
    } catch (caught) {
      setLoadError(
        caught instanceof V2ApiError ? caught.message : LOAD_FAILED_MESSAGE
      );
    }
  }

  useEffect(() => {
    loadSessions();
  }, []);

  async function handleRevoke(session) {
    setAlert("");
    setBusyId(session.id);
    try {
      await requestV2(`${V2_AUTH}/sessions/${session.id}`, {
        method: "DELETE",
        idempotencyKey: newIdempotencyKey(),
      });
      if (session.current) {
        clearV2Session();
        navigate("/login", { replace: true });
        return;
      }
      await loadSessions();
    } catch (caught) {
      if (caught.status === 404) {
        await loadSessions();
        return;
      }
      setAlert(caught instanceof V2ApiError ? caught.message : FALLBACK_MESSAGE);
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div className="profile-card rise" style={{ "--rise-index": 2 }}>
      <h2 className="profile-card-title">
        <Devices size={16} aria-hidden="true" />
        设备与登录
      </h2>
      <p className="profile-expert-desc">
        登录过此 V2 账户的设备会话。退出某台设备后，该设备上的登录立即失效。
      </p>
      {loadError ? (
        <p className="form-alert" role="alert">
          {loadError}
        </p>
      ) : null}
      {sessions === null && !loadError ? (
        <p className="profile-expert-desc">加载中…</p>
      ) : null}
      {sessions !== null ? (
        <ul className="v2-session-list">
          {sessions.map((session) => (
            <li className="v2-session-row" key={session.id}>
              <div className="v2-session-main">
                <div className="v2-session-device">
                  {session.device_label ?? "未知设备"}
                  {session.current ? (
                    <span className="v2-session-badge">本机</span>
                  ) : null}
                </div>
                <div className="v2-session-times">
                  登录于 {formatDateTime(session.created_at)} · 到期{" "}
                  {formatDateTime(session.expires_at)}
                </div>
              </div>
              <button
                type="button"
                className="btn btn-ghost btn-sm is-danger v2-session-revoke"
                disabled={busyId === session.id}
                onClick={() => handleRevoke(session)}
              >
                {busyId === session.id
                  ? "退出中…"
                  : session.current
                    ? "仅退出本机"
                    : "退出"}
              </button>
            </li>
          ))}
        </ul>
      ) : null}
      {alert ? (
        <p className="form-alert" role="alert">
          {alert}
        </p>
      ) : null}
    </div>
  );
}
