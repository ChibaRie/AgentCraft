import { Check, Globe, UploadSimple } from "@phosphor-icons/react";

const BRAND_POINTS = [
  { icon: Globe, label: "发布专家，沉淀可复用的人设与方法论" },
  { icon: UploadSimple, label: "召唤专家，挂载项目目录与任务文件" },
  { icon: Check, label: "Skill 注入上下文，任务能力随建随用" },
];

/**
 * 认证面品牌侧（FE-T3 引入于 LoginPage，FE-T4 提为共享）：
 * V1/V2/提示态/邀请与验证落地页共用，纯展示。
 */
export default function AuthBrandPanel() {
  return (
    <section className="auth-brand rise" aria-label="AgentCraft 产品介绍">
      <div className="auth-brand-eyebrow rise" style={{ "--rise-index": 0 }}>
        <span className="navbar-mark" aria-hidden="true" />
        AgentCraft
      </div>
      <div className="auth-brand-body rise" style={{ "--rise-index": 1 }}>
        <h1 className="auth-brand-title">
          把领域的经验，
          <br />
          交给一个可靠的专家。
        </h1>
        <p className="auth-brand-sub">
          AgentCraft 是运行在你本机的 AI 专家工作台：专家由你定义，Skill 与工具由你装配，
          任务在你授权的项目目录里完成。
        </p>
      </div>
      <div className="auth-brand-points rise" style={{ "--rise-index": 2 }}>
        {BRAND_POINTS.map((point) => (
          <div className="auth-brand-point" key={point.label}>
            <point.icon size={15} aria-hidden="true" />
            <span>
              <strong>{point.label.split("，")[0]}</strong>，{point.label.split("，")[1]}
            </span>
          </div>
        ))}
      </div>
    </section>
  );
}
