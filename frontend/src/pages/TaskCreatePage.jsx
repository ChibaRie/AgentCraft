import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { ArrowLeft, ArrowRight } from "@phosphor-icons/react";
import {
  V2ApiError,
  getCsrfToken,
  newIdempotencyKey,
  requestV2,
} from "../api/v2/client.js";
import { V2_DISCOVER, V2_PROVIDERS, V2_TASKS } from "../api/v2/routes.js";

/**
 * P09 任务创建页（Phase 8 T8 按 detached 模型全量重建；契约 = Sup §3/§4/§10.2）。
 *
 * 四步流：
 * ① 专家选择——GET ${V2_DISCOVER}/experts 卡片段（含 published_revision_id），
 *    选中项无 rid（异常态）禁用提交；支持 ?expert=&rid= 深链（T11 对接）。
 * ② Provider 选择——GET ${V2_PROVIDERS} 列表 + 「默认」缺省项；缺省提交不携
 *    provider_id 键（服务端走默认位解析）；PROVIDER_NOT_CONFIGURED /
 *    CATALOG_ITEM_DISABLED 错误面文案。
 * ③ 首条消息 1..65536 + 可选文件区（≤10 个、单个 ≤5MiB——客户端预检拦截，
 *    服务端 FILE_LIMIT_EXCEEDED 同样落错误面）。
 * ④ 提交编排——POST ${V2_TASKS}（幂等键现场生成）→ 201 → 逐文件 multipart
 *    POST ${V2_TASKS}/${id}/files（幂等键逐文件生成；requestV2 是 JSON-only，
 *    上传走 raw fetch 同信封语义）→ POST ${V2_TASKS}/${id}/input/commit
 *    （幂等键；空 manifest 合法——零文件也必须 commit）→ navigate /tasks/${id}。
 *    uploading 态页内展示 quota（GET ${V2_TASKS}/${id}/quota，仅展示位，失败静默）。
 */

const BASE_URL = import.meta.env.VITE_API_BASE_URL || "";

const MAX_FILES = 10;
const MAX_SINGLE_FILE_BYTES = 5 * 1024 * 1024;
const MAX_MESSAGE_CHARS = 65536;
const DISCOVER_PAGE_SIZE = 50;

const FALLBACK_MESSAGE = "操作失败，请稍后重试";

// 建任务错误码 → 引导文案（其余码直接透出后端 message）
const SUBMIT_ERROR_HINTS = {
  PROVIDER_NOT_CONFIGURED:
    "尚未配置可用的 Provider：请在「设置 → Provider」添加并启用模型服务，或更换选择后再试。",
  CATALOG_ITEM_DISABLED:
    "所选 Provider 的目录条目已被停用：请更换 Provider 或刷新目录后重试。",
  FILE_LIMIT_EXCEEDED:
    "文件超出限制（至多 10 个、单个 ≤5MiB、输入+产物总量 ≤10MiB），请调整后重试。",
};

const CATEGORY_LABELS = {
  tech: "技术",
  design: "设计",
  writing: "写作",
  data_analysis: "数据分析",
  office: "办公",
  other: "其他",
};

function describeError(cause) {
  if (cause instanceof V2ApiError) {
    return SUBMIT_ERROR_HINTS[cause.code] ?? cause.message;
  }
  return FALLBACK_MESSAGE;
}

/**
 * multipart 输入文件上传（raw fetch）：requestV2 仅支持 JSON body，FormData
 * 必须直连。信封/错误语义镜像 requestV2 finalize（V2ApiError 统一错误面）。
 */
async function uploadInputFile(taskId, file, idempotencyKey) {
  const form = new FormData();
  form.append("files", file, file.name);
  const headers = { "Idempotency-Key": idempotencyKey };
  const csrf = getCsrfToken();
  if (csrf) {
    headers["X-CSRF-Token"] = csrf;
  }
  const response = await fetch(
    `${BASE_URL}${V2_TASKS}/${encodeURIComponent(taskId)}/files`,
    {
      method: "POST",
      headers,
      body: form,
      credentials: "same-origin",
    }
  );
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    throw new V2ApiError(
      payload?.error?.code ?? `HTTP_${response.status}`,
      payload?.error?.message ?? `上传失败（HTTP ${response.status}）`,
      response.status
    );
  }
  if (payload === null || typeof payload !== "object") {
    throw new V2ApiError("INVALID_RESPONSE", "响应格式错误，请稍后重试", response.status);
  }
  return payload.data;
}

function ExpertAvatar({ expert }) {
  // 与 V1 视觉对齐：首字母块（discover 卡 avatar_url 暂不做图片渲染）
  return (
    <span className="task-create-expert-avatar" aria-hidden="true">
      {String(expert.name ?? "?").slice(0, 1)}
    </span>
  );
}

function expertLine(expert) {
  const category = CATEGORY_LABELS[expert.category] ?? expert.category;
  const skills = expert.skill_count ? ` · ${expert.skill_count} 个技能` : "";
  return `${category} · ${expert.description ?? ""}${skills}`;
}

function ExpertCard({ expert, onReset }) {
  return (
    <div className="task-create-expert">
      <ExpertAvatar expert={expert} />
      <div>
        <div className="task-create-expert-name">{expert.name}</div>
        <div className="task-create-expert-desc">{expertLine(expert)}</div>
      </div>
      <button
        type="button"
        className="btn btn-ghost btn-sm task-create-back"
        onClick={onReset}
      >
        <ArrowLeft size={14} aria-hidden="true" /> 重新选择
      </button>
    </div>
  );
}

function ExpertPicker({ experts, onSelect }) {
  if (experts === null) {
    return <p className="task-create-note">正在读取专家中心…</p>;
  }
  if (experts.length === 0) {
    return <p className="task-create-note">专家中心还没有可召唤的专家。</p>;
  }
  return (
    <ul className="task-create-picker">
      {experts.map((expert) => (
        <li key={expert.id}>
          <button
            type="button"
            className="task-create-option"
            onClick={() => onSelect(expert)}
          >
            <ExpertAvatar expert={expert} />
            <div>
              <div className="task-create-expert-name">{expert.name}</div>
              <div className="task-create-expert-desc">{expertLine(expert)}</div>
            </div>
            <ArrowRight size={15} aria-hidden="true" />
          </button>
        </li>
      ))}
    </ul>
  );
}

/** 卡片段（discover 列表项或详情）→ 选中态；rid 深链优先，缺失（异常态）记 null */
function toSelection(card, ridOverride) {
  return {
    id: card.id,
    rid: ridOverride || card.published_revision_id || null,
    name: card.name ?? "未命名专家",
    description: card.description ?? "",
    category: card.category ?? "other",
    skill_count: card.skill_count ?? 0,
  };
}

/** 文件预检（客户端拦截，不发请求）；返回错误文案或 null */
function validateFiles(files) {
  if (files.length > MAX_FILES) {
    return `最多附加 ${MAX_FILES} 个文件`;
  }
  const oversized = files.filter((file) => file.size > MAX_SINGLE_FILE_BYTES);
  if (oversized.length > 0) {
    return `单个文件不能超过 5MiB：${oversized.map((file) => file.name).join("、")}`;
  }
  return null;
}

const SUBMIT_LABELS = {
  creating: "创建中…",
  uploading: "上传文件中…",
  committing: "冻结输入中…",
};

export default function TaskCreatePage() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const expertParam = searchParams.get("expert");
  const ridParam = searchParams.get("rid");

  const [experts, setExperts] = useState(null); // null = 装载中
  const [expertLoadError, setExpertLoadError] = useState(null);
  const [selected, setSelected] = useState(null); // {id, rid, name, description, ...}
  const [providers, setProviders] = useState([]);
  const [providerId, setProviderId] = useState(""); // "" = 默认（用户默认或系统）
  const [message, setMessage] = useState("");
  const [files, setFiles] = useState([]);
  const [submitState, setSubmitState] = useState("idle");
  const [notice, setNotice] = useState(null);
  const [quota, setQuota] = useState(null);

  // ① 专家列表（V2 discover 卡片段含 published_revision_id，§10.2）
  useEffect(() => {
    let cancelled = false;
    requestV2(
      `${V2_DISCOVER}/experts?page=1&page_size=${DISCOVER_PAGE_SIZE}`
    )
      .then((result) => {
        if (!cancelled) {
          setExperts(Array.isArray(result.data?.items) ? result.data.items : []);
        }
      })
      .catch((cause) => {
        if (!cancelled) {
          setExpertLoadError(describeError(cause));
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // ② Provider 列表（可选项，失败不阻塞建任务——服务端仍可走默认位解析）
  useEffect(() => {
    let cancelled = false;
    requestV2(V2_PROVIDERS)
      .then((result) => {
        if (!cancelled) {
          setProviders(Array.isArray(result.data) ? result.data : []);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setProviders([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // 深链预选：?expert={id} 命中列表即选；未命中（分页外/草稿）→ discover 详情兜底
  useEffect(() => {
    if (experts === null || selected || !expertParam) {
      return undefined;
    }
    const card = experts.find((item) => item.id === expertParam);
    if (card) {
      setSelected(toSelection(card, ridParam));
      return undefined;
    }
    let cancelled = false;
    requestV2(`${V2_DISCOVER}/experts/${encodeURIComponent(expertParam)}`)
      .then((result) => {
        if (!cancelled && result.data) {
          setSelected(toSelection(result.data, ridParam));
        }
      })
      .catch(() => {
        // 深链失效静默回退选择流
      });
    return () => {
      cancelled = true;
    };
  }, [experts, selected, expertParam, ridParam]);

  // ④ quota 展示位（uploading 态拉取一次；失败静默——权威判定在服务端写事务）
  function loadQuota(taskId) {
    requestV2(`${V2_TASKS}/${encodeURIComponent(taskId)}/quota`)
      .then((result) => setQuota(result.data ?? null))
      .catch(() => setQuota(null));
  }

  function createTask() {
    const body = { expert_revision_id: selected.rid, initial_message: message.trim() };
    if (providerId) {
      body.provider_id = providerId; // 缺省项（""）→ 键整体缺席
    }
    return requestV2(V2_TASKS, {
      method: "POST",
      body,
      idempotencyKey: newIdempotencyKey(), // 每次提交现场生成（ProviderSettingsPage 范式）
    }).then((result) => {
      const taskId = result.data?.task?.id;
      if (!taskId) {
        throw new V2ApiError("INVALID_RESPONSE", "创建响应缺少任务标识，请稍后重试", result.status);
      }
      return taskId;
    });
  }

  async function uploadAllFiles(taskId) {
    const manifest = [];
    for (const file of files) {
      const result = await uploadInputFile(taskId, file, newIdempotencyKey()); // 逐文件各新键
      const uploaded = result?.files?.[0];
      if (!uploaded?.sha256) {
        throw new V2ApiError("INVALID_RESPONSE", "上传响应缺少文件校验信息", 200);
      }
      manifest.push({
        file_name: uploaded.file_name,
        sha256: uploaded.sha256,
        size: uploaded.size_bytes,
      });
    }
    return manifest;
  }

  function commitInput(taskId, manifest) {
    // 空 manifest 合法：零文件也必须显式 commit（Sup §4）
    return requestV2(`${V2_TASKS}/${encodeURIComponent(taskId)}/input/commit`, {
      method: "POST",
      body: { manifest },
      idempotencyKey: newIdempotencyKey(),
    });
  }

  async function handleSubmit(event) {
    event.preventDefault();
    if (submitState !== "idle" || !selected?.rid || !message.trim()) {
      return;
    }
    const fileProblem = validateFiles(files);
    if (fileProblem) {
      setNotice(fileProblem); // ③ 客户端预检：不发任何请求
      return;
    }
    setNotice(null);
    setSubmitState("creating");
    let taskId = null;
    try {
      taskId = await createTask();
      setSubmitState("uploading");
      loadQuota(taskId);
      const manifest = await uploadAllFiles(taskId);
      setSubmitState("committing");
      await commitInput(taskId, manifest);
    } catch (cause) {
      if (
        taskId !== null &&
        cause instanceof V2ApiError &&
        cause.status === 409 &&
        cause.code === "INPUT_COMMITTED"
      ) {
        // ⑥ 重试防护：输入已冻结（本流刚创建的任务，唯一成因）——不再重试
        // commit，恢复导航到任务页
        navigate(`/tasks/${taskId}`);
        return;
      }
      setNotice(describeError(cause));
      setSubmitState("idle");
      return;
    }
    navigate(`/tasks/${taskId}`);
  }

  const isSubmitting = submitState !== "idle";
  const userDefaultName = providers.find((provider) => provider.is_default)?.catalog_display_name;
  const quotaText = quota
    ? `任务用量：输入 ${quota.usage?.inputs_count ?? 0}/${quota.limits?.max_files_per_task ?? MAX_FILES} 个 · 累计 ${quota.usage?.total_bytes ?? 0}/${quota.limits?.max_task_bytes ?? "-"} 字节`
    : "正在读取任务用量…";

  return (
    <main className="page">
      <div className="task-create">
        <header className="page-header">
          <h1 className="page-title">召唤专家</h1>
          <p className="page-sub">选择专家、描述任务，可选附加输入文件。</p>
        </header>

        {expertLoadError && (
          <div className="form-alert" role="alert">
            {expertLoadError}
          </div>
        )}

        {!selected && (
          <ExpertPicker
            experts={experts}
            onSelect={(card) => setSelected(toSelection(card, ridParam))}
          />
        )}

        {selected && (
          <form className="task-create-form" onSubmit={handleSubmit}>
            <ExpertCard
              expert={selected}
              onReset={() => {
                setSelected(null);
                setNotice(null);
              }}
            />

            <div className="field">
              <label className="field-label" htmlFor="task-message">
                首条消息
              </label>
              <textarea
                id="task-message"
                className="field-input task-create-textarea"
                value={message}
                maxLength={MAX_MESSAGE_CHARS}
                onChange={(event) => setMessage(event.target.value)}
                placeholder="例如：帮我整理本周的技术周报，覆盖架构组与平台组的进展…"
                required
              />
            </div>

            <div className="field">
              <label className="field-label" htmlFor="task-files">
                附加输入文件（可选）
              </label>
              <input
                id="task-files"
                className="field-input"
                type="file"
                multiple
                onChange={(event) => setFiles(Array.from(event.target.files ?? []))}
              />
              <p className="field-note">
                至多 {MAX_FILES} 个、单个 ≤5MiB；提交后输入即冻结。
              </p>
              {files.length > 0 && (
                <p className="task-create-note">已选 {files.length} 个文件。</p>
              )}
            </div>

            <div className="field">
              <label className="field-label" htmlFor="task-provider">
                模型服务（Provider）
              </label>
              <select
                id="task-provider"
                className="field-input"
                value={providerId}
                onChange={(event) => setProviderId(event.target.value)}
              >
                <option value="">
                  默认{userDefaultName ? `（${userDefaultName}）` : "（系统级配置）"}
                </option>
                {providers.map((provider) => (
                  <option key={provider.id} value={provider.id}>
                    {provider.catalog_display_name} · {provider.model_id}
                  </option>
                ))}
              </select>
              <p className="field-note">
                缺省使用你的默认 Provider；所选 Provider 在创建时冻结为任务快照，
                之后修改配置不影响本任务。
              </p>
            </div>

            {(submitState === "uploading" || submitState === "committing") && (
              <p className="task-create-note" data-testid="task-quota">
                {quotaText}
              </p>
            )}

            {notice && (
              <div className="form-alert" role="alert">
                {notice}
              </div>
            )}

            <div className="task-create-actions">
              <button
                type="button"
                className="btn btn-ghost"
                disabled={isSubmitting}
                onClick={() => navigate("/discover")}
              >
                取消
              </button>
              <button
                type="submit"
                className="btn btn-primary"
                disabled={isSubmitting || !selected.rid || !message.trim()}
              >
                {SUBMIT_LABELS[submitState] ?? "创建任务"}
              </button>
            </div>
          </form>
        )}
      </div>
    </main>
  );
}
