import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { CheckCircle, UserCircle } from "@phosphor-icons/react";
import { requestV2 } from "../api/v2/client.js";
import { V2_DISCOVER } from "../api/v2/routes.js";
import { CATEGORY_LABELS } from "../lib/categories.js";

export default function ExpertDetailPage({ expertId }) {
  const [expert, setExpert] = useState(null);
  const [isMissing, setIsMissing] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const navigate = useNavigate();

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setIsLoading(true);
      setLoadError("");
      try {
        // V2 discover 详情（Sup §10.2）：published_revision_id 直作召唤专家的
        // expert_revision_id，不再二次解析
        const result = await requestV2(`${V2_DISCOVER}/experts/${expertId}`);
        if (!cancelled) {
          setExpert(result.data);
        }
      } catch (error) {
        if (!cancelled) {
          // 404 是"专家不存在或未公开"；其余（网络/5xx）单独提示，避免误导用户
          if (error.status === 404) {
            setIsMissing(true);
          } else {
            setLoadError(error.message || "加载失败，请稍后重试");
          }
        }
      } finally {
        if (!cancelled) {
          setIsLoading(false);
        }
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [expertId]);

  if (isLoading) {
    return (
      <main className="app-main">
        <div className="detail-shell">
          <div className="profile-card is-skeleton" aria-hidden="true">
            <div className="skeleton-line is-title" />
            <div className="skeleton-line" />
            <div className="skeleton-line is-short" />
          </div>
        </div>
      </main>
    );
  }

  if (isMissing) {
    return (
      <main className="app-main">
        <div className="empty-state rise">
          <strong>专家不存在或未公开</strong>
          该专家可能已下架。
          <div style={{ marginTop: 14 }}>
            <Link to="/discover" className="btn btn-ghost">
              返回专家中心
            </Link>
          </div>
        </div>
      </main>
    );
  }

  if (loadError || !expert) {
    return (
      <main className="app-main">
        <div className="empty-state rise">
          <strong>加载失败</strong>
          {loadError || "请稍后重试。"}
          <div style={{ marginTop: 14 }}>
            <Link to="/discover" className="btn btn-ghost">
              返回专家中心
            </Link>
          </div>
        </div>
      </main>
    );
  }

  return (
    <main className="app-main">
      <div className="detail-shell">
        <header className="profile-card rise">
          <div className="profile-identity">
            <span className="profile-avatar" aria-hidden="true">
              {expert.name.slice(0, 1)}
            </span>
            <div>
              <div className="profile-name-row">
                <span className="profile-name">{expert.name}</span>
                <span className="role-badge">{CATEGORY_LABELS[expert.category] || expert.category}</span>
              </div>
              <div className="profile-email">{expert.description}</div>
            </div>
            <button
              type="button"
              className="btn btn-primary detail-summon"
              title="创建任务并开始对话"
              onClick={() => {
                // Sup §10.2：published_revision_id 作 rid 查询参数（TaskCreatePage
                // 可选消费，直用为 POST /tasks 的 expert_revision_id）；缺失
                // （防御，published 实体恒非空）时不带 rid——创建页回退选择流
                const params = new URLSearchParams({ expert: expert.id });
                if (expert.published_revision_id) {
                  params.set("rid", expert.published_revision_id);
                }
                navigate(`/tasks/new?${params.toString()}`);
              }}
            >
              召唤专家
            </button>
          </div>
        </header>

        <div className="detail-columns">
          <section className="profile-card rise" style={{ "--rise-index": 1 }} aria-label="人设与方法论">
            <h2 className="profile-card-title">
              <UserCircle size={16} aria-hidden="true" />
              人设
            </h2>
            <p className="detail-prose">{expert.persona}</p>
            <h2 className="profile-card-title" style={{ marginTop: 22 }}>
              方法论
            </h2>
            <p className="detail-prose">{expert.methodology}</p>
            {expert.task_examples && expert.task_examples.length > 0 && (
              <>
                <h2 className="profile-card-title" style={{ marginTop: 22 }}>
                  擅长的任务
                </h2>
                <ul className="detail-examples">
                  {expert.task_examples.map((example, index) => (
                    <li key={index}>
                      <CheckCircle size={13} aria-hidden="true" />
                      {example}
                    </li>
                  ))}
                </ul>
              </>
            )}
          </section>

          <section className="profile-card rise" style={{ "--rise-index": 2 }} aria-label="已启用 Skill">
            <h2 className="profile-card-title">已启用的 Skill</h2>
            {expert.skills.length === 0 ? (
              <p className="detail-prose">这位专家暂未启用 Skill，将直接以人设与方法论完成任务。</p>
            ) : (
              <ul className="skill-mini-list">
                {/* V2 详情 skill 项形状 {skill_id, name, revision_no}——无 description，
                    有则渲染（cutover 后仅 V2 形状） */}
                {expert.skills.map((skill) => (
                  <li className="skill-mini" key={skill.skill_id ?? skill.id ?? skill.name}>
                    <strong>{skill.name}</strong>
                    {skill.description ? <span>{skill.description}</span> : null}
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>
      </div>
    </main>
  );
}
