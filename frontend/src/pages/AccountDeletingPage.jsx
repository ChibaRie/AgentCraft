import { useLocation } from "react-router-dom";
import { ShieldWarning } from "@phosphor-icons/react";

// 后端契约缺省兜底：deletion/request 200 恒带 days_remaining（14 天宽限期），
// 直达/刷新丢失 location.state 时按契约默认值展示
const DEFAULT_DAYS_REMAINING = 14;

/**
 * 账户注销中冻结页（FE-T7 终审修复：独立路由 /account/deleting）。
 *
 * 为什么独立于 /profile 且不挂守卫：注销受理成功路径 clearV2Session()
 * （v2User→null）与路由跳转同批提交——冻结页若仍挂在 RequireAuth 之后，
 * V2-only 用户（无 V1 会话兜底）会在同一渲染批次被守卫 Navigate /login
 * 弹走，「账户注销中 N 天」展示态永远不可达。故本页不挂任何门；
 * days_remaining 经 DangerZone 的 navigate state 传入，缺失时显示 14。
 */
export default function AccountDeletingPage() {
  const location = useLocation();
  const daysRemaining = location.state?.daysRemaining ?? DEFAULT_DAYS_REMAINING;

  return (
    <main className="app-main">
      <section className="v2-deleting-page rise" role="status">
        <ShieldWarning size={28} weight="fill" aria-hidden="true" />
        <h1 className="v2-deleting-title">账户注销中</h1>
        <p className="v2-deleting-lead">
          账户注销申请已受理，<strong>{daysRemaining} 天后生效</strong>。
        </p>
        <p className="v2-deleting-note">
          恢复链接已发送至邮箱，宽限期内可凭邮件中的恢复链接撤销注销、
          恢复账户的正常使用。
        </p>
        <p className="v2-deleting-domain">
          本次注销仅针对新账户体系（V2 账户域）；旧版工作区账户的登录不受影响。
        </p>
      </section>
    </main>
  );
}
