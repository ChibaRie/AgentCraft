import { useEffect, useState } from "react";
import { Wrench, X } from "@phosphor-icons/react";
import { formatDateTime } from "../lib/datetime.js";

/**
 * 右侧上下文面板（P09 增强）：Skill 快照 / MCP 工具 / 调用记录 三 tab。
 *
 * 数据全部来自任务创建时冻结的快照（GET /api/tasks/{id} 的 skills/mcp_tools）
 * 与消息历史中的 role=tool 行——「任务创建后改专家/Skill 不影响本任务」
 * 的快照语义在 UI 上可见。
 */
export default function TaskContextPanel({ skills = [], mcpTools = [], toolCalls = [] }) {
  const [activeTab, setActiveTab] = useState("skill");
  const [drawerSkill, setDrawerSkill] = useState(null);

  // Esc 关闭抽屉
  useEffect(() => {
    if (!drawerSkill) {
      return undefined;
    }
    const onKeyDown = (event) => {
      if (event.key === "Escape") {
        setDrawerSkill(null);
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [drawerSkill]);

  const tabs = [
    { key: "skill", label: "Skill", count: skills.length },
    { key: "mcp", label: "MCP 工具", count: mcpTools.length },
    { key: "events", label: "调用记录", count: toolCalls.length },
  ];

  return (
    <aside className="context-panel rise" style={{ "--rise-index": 2 }} aria-label="专家上下文">
      <div className="context-tabs" role="tablist" aria-label="上下文面板">
        {tabs.map((tab) => (
          <button
            key={tab.key}
            type="button"
            role="tab"
            aria-selected={activeTab === tab.key}
            className={`context-tab${activeTab === tab.key ? " is-active" : ""}`}
            onClick={() => setActiveTab(tab.key)}
          >
            {tab.label}
            <span className="context-tab-count">{tab.count}</span>
          </button>
        ))}
      </div>

      {activeTab === "skill" && (
        <div className="context-body" role="tabpanel">
          <p className="context-note">
            任务创建时已冻结 <code>skill_snapshot</code>，后续修改 Skill 不影响本任务。
          </p>
          {skills.length === 0 ? (
            <p className="context-empty">该专家未启用任何 Skill</p>
          ) : (
            skills.map((skill) => (
              <button
                key={skill.name}
                type="button"
                className="context-card"
                onClick={() => setDrawerSkill(skill)}
              >
                <span className="context-card-name">{skill.name}</span>
                <span className="context-card-desc">
                  {skill.content.split("\n")[0]?.replace(/^角色：/, "") || "查看完整定义"}
                </span>
              </button>
            ))
          )}
        </div>
      )}

      {activeTab === "mcp" && (
        <div className="context-body" role="tabpanel">
          <p className="context-note">
            工具集在任务创建时冻结（能力上限快照）；调用经控制面实时校验，
            Server 下架 / 工具禁用 / 授权撤销会立即阻断。
          </p>
          {mcpTools.length === 0 ? (
            <p className="context-empty">本任务未绑定 MCP 工具</p>
          ) : (
            mcpTools.map((tool) => (
              <div className="context-tool-card" key={`${tool.serverId}-${tool.name}`}>
                <div className="context-tool-head">
                  <Wrench size={12} aria-hidden="true" />
                  <span className="context-tool-name">{tool.name}</span>
                  {tool.sensitive && (
                    <span className="skill-status is-offline" title="敏感工具：启用已记录授权">
                      敏感
                    </span>
                  )}
                </div>
                <p className="context-tool-desc">{tool.description}</p>
              </div>
            ))
          )}
        </div>
      )}

      {activeTab === "events" && (
        <div className="context-body" role="tabpanel">
          <p className="context-note">工具调用经 SSE 实时推送，结果回注 Agent 上下文继续推理。</p>
          {toolCalls.length === 0 ? (
            <p className="context-empty">本任务暂无工具调用记录</p>
          ) : (
            <ul className="event-list">
              {toolCalls.map((call, index) => (
                <li className="event-item" key={`event-${index}`}>
                  <div className="event-top">
                    <span className="event-name">{call.name}</span>
                    <span className={`event-state${call.status === "error" ? " is-error" : ""}`}>
                      {call.status === "running"
                        ? "执行中…"
                        : call.status === "error"
                          ? "调用失败"
                          : "已完成"}
                    </span>
                    {call.time && <span className="event-time">{formatDateTime(call.time)}</span>}
                  </div>
                  {call.result && <pre className="event-result">{call.result}</pre>}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {drawerSkill && (
        <div
          className="drawer-mask"
          role="presentation"
          onClick={(event) => {
            if (event.target === event.currentTarget) {
              setDrawerSkill(null);
            }
          }}
        >
          <div className="drawer" role="dialog" aria-modal="true" aria-labelledby="drawer-title">
            <div className="drawer-head">
              <h2 id="drawer-title">{drawerSkill.name}</h2>
              <button
                type="button"
                className="drawer-close"
                aria-label="关闭详情"
                onClick={() => setDrawerSkill(null)}
              >
                <X size={16} aria-hidden="true" />
              </button>
            </div>
            <div className="drawer-body">
              <pre className="drawer-content">{drawerSkill.content}</pre>
              <p className="context-note">以上为任务创建时冻结的快照内容（只读）。</p>
            </div>
          </div>
        </div>
      )}
    </aside>
  );
}
