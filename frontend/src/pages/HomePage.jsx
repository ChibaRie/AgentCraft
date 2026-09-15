import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowRight, ChatCenteredDots, Plus, Wrench } from "@phosphor-icons/react";
import { useAuth } from "../auth/AuthContext.jsx";
import { displayName } from "../auth/displayName.js";
import { request } from "../api/client.js";
import { requestV2 } from "../api/v2/client.js";
import { V2_DISCOVER, V2_TASKS } from "../api/v2/routes.js";
import { CATEGORY_LABELS, CATEGORY_OPTIONS } from "../lib/categories.js";
import { TASK_STATUS_LABELS, toDisplayTask } from "../lib/taskDisplay.js";

function greeting() {
  const hour = new Date().getHours();
  if (hour < 6) return "夜深了";
  if (hour < 12) return "早上好";
  if (hour < 18) return "下午好";
  return "晚上好";
}

/** V2 discover 列表（Sup §10.2 信封 {items,total,page,page_size}；分页参数
 *  page_size 语义对齐 V2——V1 为 size）。 */
async function fetchFeaturedExperts(category) {
  const params = new URLSearchParams({ page: "1", page_size: "6" });
  if (category) {
    params.set("category", category);
  }
  const result = await requestV2(`${V2_DISCOVER}/experts?${params.toString()}`);
  return { items: result.data?.items ?? [], total: result.data?.total ?? 0 };
}

function HeroInkPanel({ stats }) {
  return (
    <div className="home-ink" aria-hidden="true">
      <p className="home-ink-title">工作台此刻</p>
      <ul className="home-ink-stats">
        <li>
          <strong>{stats.runningTasks}</strong>
          <span>进行中任务</span>
        </li>
        <li>
          <strong>{stats.publishedExperts}</strong>
          <span>专家中心在列</span>
        </li>
        <li>
          <strong>{stats.mySkills}</strong>
          <span>我发布的 Skill</span>
        </li>
      </ul>
      <p className="home-ink-foot">专家由你定义 · 执行在本机沙箱</p>
    </div>
  );
}

function TaskDigest({ tasks }) {
  return (
    <ul className="home-task-list">
      {tasks.map((task) => (
        <li key={task.id}>
          <Link to={`/tasks/${task.id}`} className="home-task-row">
            <span className="home-task-title">{task.title || `任务 #${task.id}`}</span>
            <span className="home-task-meta">
              <span className={`status-chip is-${task.status}`}>
                {TASK_STATUS_LABELS[task.status] || task.status}
              </span>
              <span className="context-tool-desc">{task.expertLabel}</span>
            </span>
          </Link>
        </li>
      ))}
    </ul>
  );
}

function ExpertCard({ expert, index }) {
  return (
    <Link
      to={`/discover/${expert.id}`}
      className="discover-card rise"
      style={{ "--rise-index": Math.min(index, 5) }}
    >
      <div className="discover-card-head">
        <span className="discover-avatar" aria-hidden="true">
          {expert.name.slice(0, 1)}
        </span>
        <div>
          <h3 className="discover-name">{expert.name}</h3>
          <span className="discover-category">
            {CATEGORY_LABELS[expert.category] || expert.category}
          </span>
        </div>
      </div>
      <p className="discover-desc">{expert.description}</p>
      <p className="discover-meta">{expert.skill_count} 个已启用 Skill</p>
    </Link>
  );
}

/** P02 首页（仿智能体中心的信息架构）：问候 + 工作台速览 + 最近任务 +
 *  精选专家（分类筛选）。精选/discover 走 V2（匿名可达）；最近任务按双轨
 *  会话门控（cutover 前并存，T14 收敛）。 */
export default function HomePage() {
  const { user, v2User, isExpert } = useAuth();
  // 身份源统一（终审修复）：V2 权威会话优先（NavBar 同向），V1-only 用户回退
  const displayUser = v2User ?? user;
  // 双轨任务列表门控（T11 ②）：V1 会话在 → V1 优先；否则 V2 会话 → V2_TASKS。
  // V2-only 用户此前直调 V1 /api/tasks 必 401——全局错误横幅与「最近的任务」
  // 永停加载态的根源。
  const hasV1Session = Boolean(user);
  const [recentTasks, setRecentTasks] = useState(null);
  const [stats, setStats] = useState({ runningTasks: "—", publishedExperts: "—", mySkills: "—" });
  const [experts, setExperts] = useState([]);
  const [expertTotal, setExpertTotal] = useState(0);
  const [category, setCategory] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState("");

  useEffect(() => {
    let cancelled = false;
    async function loadRecentTasks() {
      if (hasV1Session) {
        const payload = await request("/api/tasks?page=1&size=3");
        return payload.data.map(toDisplayTask);
      }
      if (v2User) {
        const result = await requestV2(`${V2_TASKS}?page=1&size=3`);
        return (result.data?.items ?? []).map(toDisplayTask);
      }
      return [];
    }
    async function load() {
      try {
        const skillReq =
          hasV1Session && isExpert ? request("/api/skills?page=1&size=1") : null;
        const [tasks, featured, skillPayload] = await Promise.all([
          loadRecentTasks(),
          fetchFeaturedExperts(""),
          skillReq?.catch(() => null) ?? Promise.resolve(null),
        ]);
        if (cancelled) {
          return;
        }
        setRecentTasks(tasks);
        setExperts(featured.items);
        setExpertTotal(featured.total);
        setStats({
          runningTasks: tasks.filter((task) => task.status === "running").length,
          publishedExperts: featured.total,
          mySkills: hasV1Session && isExpert && skillPayload ? skillPayload.total : "—",
        });
      } catch (error) {
        if (!cancelled) {
          setLoadError(error.message || "加载失败，请稍后重试");
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
  }, [hasV1Session, v2User, isExpert]);

  // 分类筛选切换：重新拉取精选（首页每类只展示前 6 个）
  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const featured = await fetchFeaturedExperts(category);
        if (!cancelled) {
          setExperts(featured.items);
          setExpertTotal(featured.total);
        }
      } catch {
        // 精选区失败不打断首屏（顶部已有全局错误提示）
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [category]);

  return (
    <main className="app-main home">
      <section className="home-hero rise">
        <div className="home-hero-copy">
          <p className="home-hero-eyebrow">AgentCraft 工作台</p>
          <h1 className="home-hero-title">
            {greeting()}，{displayName(displayUser)}。
          </h1>
          <p className="home-hero-sub">
            把领域的经验交给一位可靠的专家——人设与 Skill 由你装配，
            任务在你授权的项目目录里由本机沙箱完成。
          </p>
          <div className="home-hero-actions">
            <Link to="/discover" className="btn btn-primary">
              <ChatCenteredDots size={15} aria-hidden="true" />
              召唤专家
            </Link>
            <Link to="/tasks/new" className="btn btn-ghost">
              <Plus size={15} aria-hidden="true" />
              直接建任务
            </Link>
            <Link to="/skills" className="btn btn-ghost">
              <Wrench size={15} aria-hidden="true" />
              管理 Skill
            </Link>
          </div>
        </div>
        <HeroInkPanel stats={stats} />
      </section>

      <section className="home-section rise" style={{ "--rise-index": 1 }} aria-label="最近任务">
        <header className="home-section-head">
          <h2 className="home-section-title">最近的任务</h2>
          <Link to="/tasks" className="home-section-more">
            全部任务
            <ArrowRight size={12} aria-hidden="true" />
          </Link>
        </header>
        {recentTasks === null ? (
          <p className="home-loading">加载中…</p>
        ) : recentTasks.length === 0 ? (
          <p className="home-empty">还没有任务——从专家中心召唤一位，开始第一轮对话。</p>
        ) : (
          <TaskDigest tasks={recentTasks} />
        )}
      </section>

      <section className="home-section rise" style={{ "--rise-index": 2 }} aria-label="精选专家">
        <header className="home-section-head">
          <h2 className="home-section-title">精选专家</h2>
          <Link to="/discover" className="home-section-more">
            进入专家中心
            <ArrowRight size={12} aria-hidden="true" />
          </Link>
        </header>
        <div className="discover-filters" role="group" aria-label="精选分类">
          <button
            type="button"
            className={`filter-chip ${category === "" ? "is-active" : ""}`}
            onClick={() => setCategory("")}
          >
            全部
          </button>
          {CATEGORY_OPTIONS.map((option) => (
            <button
              type="button"
              key={option.value}
              className={`filter-chip ${category === option.value ? "is-active" : ""}`}
              onClick={() => setCategory(option.value)}
            >
              {option.label}
            </button>
          ))}
        </div>
        {loadError ? (
          <div className="form-alert" role="alert">
            {loadError}
          </div>
        ) : isLoading ? (
          <div className="discover-grid">
            {[0, 1, 2].map((index) => (
              <div className="discover-card is-skeleton" key={index} aria-hidden="true">
                <div className="skeleton-line is-title" />
                <div className="skeleton-line" />
                <div className="skeleton-line is-short" />
              </div>
            ))}
          </div>
        ) : experts.length === 0 ? (
          <p className="home-empty">该分类下暂无公开专家。</p>
        ) : (
          <div className="discover-grid">
            {experts.map((expert, index) => (
              <ExpertCard key={expert.id} expert={expert} index={index} />
            ))}
          </div>
        )}
        {!isLoading && experts.length > 0 && (
          <p className="home-expert-total">专家中心共有 {expertTotal} 位公开专家。</p>
        )}
      </section>
    </main>
  );
}
