import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { MagnifyingGlass, Plus, UserCircle } from "@phosphor-icons/react";
import { useAuth } from "../auth/AuthContext.jsx";
import { requestV2 } from "../api/v2/client.js";
import { V2_DISCOVER } from "../api/v2/routes.js";
import { CATEGORY_LABELS, CATEGORY_OPTIONS } from "../lib/categories.js";

function ExpertCard({ expert, index }) {
  return (
    <Link
      to={`/discover/${expert.id}`}
      className="discover-card rise"
      style={{ "--rise-index": Math.min(index, 6) }}
    >
      <div className="discover-card-head">
        <span className="discover-avatar" aria-hidden="true">
          {expert.name.slice(0, 1)}
        </span>
        <div>
          <h3 className="discover-name">{expert.name}</h3>
          <span className="discover-category">{CATEGORY_LABELS[expert.category] || expert.category}</span>
        </div>
      </div>
      <p className="discover-desc">{expert.description}</p>
      <p className="discover-meta">{expert.skill_count} 个已启用 Skill</p>
    </Link>
  );
}

export default function ExpertCenterPage() {
  // isExpert 单判据（V2 entitlement）蕴含有效会话——isAuthenticated 门并轨删除
  const { isExpert } = useAuth();
  const [searchParams, setSearchParams] = useSearchParams();
  const search = searchParams.get("search") || "";
  const category = searchParams.get("category") || "";
  // 非法 page 参数（如 ?page=abc）回退到第 1 页
  const page = Math.max(1, Number.parseInt(searchParams.get("page") || "1", 10) || 1);

  const [cards, setCards] = useState([]);
  const [total, setTotal] = useState(0);
  const [searchText, setSearchText] = useState(search);
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const pageSize = 9;

  // URL 中的 search 参数变化（前进/后退/分享链接）时同步输入框
  useEffect(() => {
    setSearchText(search);
  }, [search]);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setIsLoading(true);
      setLoadError("");
      try {
        // V2 discover（匿名可达；Sup §10.2 信封 {items,total,page,page_size}，
        // 分页参数 page_size 语义对齐 V2——V1 为 size）
        const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
        if (search) params.set("search", search);
        if (category) params.set("category", category);
        const result = await requestV2(`${V2_DISCOVER}/experts?${params.toString()}`);
        if (!cancelled) {
          setCards(result.data?.items ?? []);
          setTotal(result.data?.total ?? 0);
        }
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
  }, [search, category, page]);

  function updateParams(changes) {
    const next = new URLSearchParams(searchParams);
    for (const [key, value] of Object.entries(changes)) {
      if (value) {
        next.set(key, value);
      } else {
        next.delete(key);
      }
    }
    if (!("page" in changes)) {
      next.delete("page");
    }
    setSearchParams(next);
  }

  const totalPages = Math.max(1, Math.ceil(total / pageSize));

  return (
    <main className="app-main">
      <header className="page-header rise">
        <div className="page-header-row">
          <div>
            <h1 className="page-title">专家中心</h1>
            <p className="page-sub">浏览社区公开的专家，找到匹配你任务的那一位。</p>
          </div>
          {isExpert && (
            <div className="manage-toolbar-actions">
              <Link to="/my-experts/new" className="btn btn-primary">
                <Plus size={14} aria-hidden="true" />
                新建专家
              </Link>
              <Link to="/my-experts" className="btn btn-ghost">
                <UserCircle size={14} aria-hidden="true" />
                我的专家
              </Link>
            </div>
          )}
        </div>
      </header>

      <div className="discover-toolbar rise" style={{ "--rise-index": 1 }}>
        <form
          className="discover-search"
          onSubmit={(event) => {
            event.preventDefault();
            updateParams({ search: searchText.trim() });
          }}
        >
          <MagnifyingGlass size={15} aria-hidden="true" />
          <input
            className="discover-search-input"
            placeholder="搜索专家名称或简介"
            value={searchText}
            onChange={(event) => setSearchText(event.target.value)}
            aria-label="搜索专家"
          />
        </form>
        <div className="discover-filters" role="group" aria-label="分类筛选">
          <button
            type="button"
            className={`filter-chip ${category === "" ? "is-active" : ""}`}
            onClick={() => updateParams({ category: "" })}
          >
            全部
          </button>
          {CATEGORY_OPTIONS.map((option) => (
            <button
              type="button"
              key={option.value}
              className={`filter-chip ${category === option.value ? "is-active" : ""}`}
              onClick={() => updateParams({ category: option.value })}
            >
              {option.label}
            </button>
          ))}
        </div>
      </div>

      <div className="form-alert" role="alert" hidden={!loadError} style={{ marginBottom: 16 }}>
        {loadError}
      </div>

      {loadError ? null : isLoading ? (
        <div className="discover-grid">
          {[0, 1, 2].map((index) => (
            <div className="discover-card is-skeleton" key={index} aria-hidden="true">
              <div className="skeleton-line is-title" />
              <div className="skeleton-line" />
              <div className="skeleton-line is-short" />
            </div>
          ))}
        </div>
      ) : cards.length === 0 ? (
        <div className="empty-state rise">
          <strong>没有找到匹配的专家</strong>
          换个关键词或清除分类筛选试试；公开专家会出现在这里。
        </div>
      ) : (
        <div className="discover-grid">
          {cards.map((expert, index) => (
            <ExpertCard key={expert.id} expert={expert} index={index} />
          ))}
        </div>
      )}

      {loadError ? null : totalPages > 1 && (
        <div className="discover-pager">
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            disabled={page <= 1}
            onClick={() => updateParams({ page: String(page - 1) })}
          >
            上一页
          </button>
          <span className="discover-pager-info">
            第 {page} / {totalPages} 页 · 共 {total} 位
          </span>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            disabled={page >= totalPages}
            onClick={() => updateParams({ page: String(page + 1) })}
          >
            下一页
          </button>
        </div>
      )}
    </main>
  );
}
