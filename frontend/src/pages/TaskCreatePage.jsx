import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { ArrowLeft, ArrowRight, Warning } from "@phosphor-icons/react";
import { request } from "../api/client.js";
import WorkdirSelector from "../components/WorkdirSelector.jsx";

const CATEGORY_LABELS = {
  tech: "技术",
  design: "设计",
  writing: "写作",
  data_analysis: "数据分析",
  office: "办公",
  other: "其他",
};

function ExpertCard({ expert }) {
  return (
    <div className="task-create-expert">
      <span className="task-create-expert-avatar" aria-hidden="true">
        {expert.name.slice(0, 1)}
      </span>
      <div>
        <div className="task-create-expert-name">{expert.name}</div>
        <div className="task-create-expert-desc">
          {CATEGORY_LABELS[expert.category] || expert.category} · {expert.description}
        </div>
      </div>
    </div>
  );
}

function ExpertPicker({ onSelect }) {
  const [experts, setExperts] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    request("/api/discover/experts?page=1&size=50")
      .then((payload) => {
        if (!cancelled) {
          setExperts(payload.data);
        }
      })
      .catch((cause) => {
        if (!cancelled) {
          setError(cause.message);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (error) {
    return <p className="form-alert">{error}</p>;
  }
  if (!experts) {
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
            onClick={() => onSelect(expert.id)}
          >
            <span className="task-create-expert-avatar" aria-hidden="true">
              {expert.name.slice(0, 1)}
            </span>
            <div>
              <div className="task-create-expert-name">{expert.name}</div>
              <div className="task-create-expert-desc">{expert.description}</div>
            </div>
            <ArrowRight size={15} aria-hidden="true" />
          </button>
        </li>
      ))}
    </ul>
  );
}

/** P09 任务创建页：召唤专家 → 描述任务 → 选工作目录 → 进入对话。 */
export default function TaskCreatePage() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const expertParam = searchParams.get("expert");

  const [expert, setExpert] = useState(null);
  const [expertError, setExpertError] = useState(null);
  const [description, setDescription] = useState("");
  const [workdir, setWorkdir] = useState("");
  const [notice, setNotice] = useState(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  useEffect(() => {
    if (!expertParam) {
      setExpert(null);
      return undefined;
    }
    let cancelled = false;
    setExpertError(null);
    request(`/api/discover/experts/${expertParam}`)
      .then((payload) => {
        if (!cancelled) {
          setExpert(payload.data);
        }
      })
      .catch((cause) => {
        if (!cancelled) {
          setExpertError(cause.message || "专家不存在或未发布");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [expertParam]);

  async function handleSubmit(event) {
    event.preventDefault();
    if (!expert || isSubmitting) {
      return;
    }
    setIsSubmitting(true);
    setNotice(null);
    try {
      const payload = await request("/api/tasks", {
        method: "POST",
        body: JSON.stringify({
          expert_id: expert.id,
          description,
          workdir: workdir || undefined,
        }),
      });
      navigate(`/tasks/${payload.data.task_id}`);
    } catch (cause) {
      setNotice(cause.message);
      setIsSubmitting(false);
    }
  }

  return (
    <main className="page">
      <div className="task-create">
        <header className="page-header">
          <h1 className="page-title">召唤专家</h1>
          <p className="page-sub">创建一个任务，与专家开始协作。</p>
        </header>

        {!expertParam && <ExpertPicker onSelect={(id) => navigate(`/tasks/new?expert=${id}`)} />}

        {expertParam && (
          <>
            {expertError && (
              <div className="form-alert" role="alert">
                <Warning size={15} aria-hidden="true" /> {expertError}
                <button
                  type="button"
                  className="btn btn-ghost btn-sm task-create-back"
                  onClick={() => navigate("/tasks/new")}
                >
                  <ArrowLeft size={14} aria-hidden="true" /> 重新选择
                </button>
              </div>
            )}
            {expert && (
              <form className="task-create-form" onSubmit={handleSubmit}>
                <ExpertCard expert={expert} />

                <div className="field">
                  <label className="field-label" htmlFor="task-description">
                    任务描述
                  </label>
                  <textarea
                    id="task-description"
                    className="field-input task-create-textarea"
                    value={description}
                    maxLength={2000}
                    onChange={(event) => setDescription(event.target.value)}
                    placeholder="例如：帮我整理本周的技术周报，覆盖架构组与平台组的进展…"
                    required
                  />
                </div>

                <WorkdirSelector value={workdir} onChange={setWorkdir} />

                {notice && (
                  <div className="form-alert" role="alert">
                    {notice}
                  </div>
                )}

                <div className="task-create-actions">
                  <button
                    type="button"
                    className="btn btn-ghost"
                    onClick={() => navigate(expertParam ? "/tasks/new" : "/discover")}
                  >
                    取消
                  </button>
                  <button
                    type="submit"
                    className="btn btn-primary"
                    disabled={isSubmitting || !description.trim()}
                  >
                    {isSubmitting ? "创建中…" : "创建任务"}
                  </button>
                </div>
              </form>
            )}
          </>
        )}
      </div>
    </main>
  );
}
