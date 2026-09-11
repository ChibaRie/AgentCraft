import { Navigate, Outlet, Route, Routes, useParams } from "react-router-dom";
import NavBar from "./components/NavBar.jsx";
import PendingVerificationBanner from "./components/PendingVerificationBanner.jsx";
import RequireAuth from "./components/RequireAuth.jsx";
import DeletionCancelPage from "./pages/DeletionCancelPage.jsx";
import EmailVerificationPage from "./pages/EmailVerificationPage.jsx";
import ExpertCenterPage from "./pages/ExpertCenterPage.jsx";
import ExpertDetailPage from "./pages/ExpertDetailPage.jsx";
import ExpertEditPage from "./pages/ExpertEditPage.jsx";
import HomePage from "./pages/HomePage.jsx";
import LoginPage from "./pages/LoginPage.jsx";
import InvitationAcceptPage from "./pages/InvitationAcceptPage.jsx";
import MyExpertsPage from "./pages/MyExpertsPage.jsx";
import PasswordResetConfirmPage from "./pages/PasswordResetConfirmPage.jsx";
import PasswordResetRequestPage from "./pages/PasswordResetRequestPage.jsx";
import ProfilePage from "./pages/ProfilePage.jsx";
import SkillManagePage from "./pages/SkillManagePage.jsx";
// 注意：此 import 用 ./ 形式——../ 形式在当前 rollup 解析器上对（且仅对）此文件失败
import ProviderSettingsPage from "./pages/ProviderSettingsPage.jsx";
import TaskChatPage from "./pages/TaskChatPage.jsx";
import TaskCreatePage from "./pages/TaskCreatePage.jsx";

function ExpertDetailRoute() {
  const { id } = useParams();
  return <ExpertDetailPage expertId={id} />;
}

function ExpertEditRoute() {
  const { id } = useParams();
  return <ExpertEditPage expertId={id} />;
}

function TaskChatRoute() {
  const { id } = useParams();
  return <TaskChatPage key={id} />;
}

/** 应用壳：导航栏 + pending 横幅 + 页面容器。/login 等认证面独立于壳外（P01 全屏构图）。 */
function AppShell() {
  return (
    <>
      <NavBar />
      <PendingVerificationBanner />
      <Outlet />
    </>
  );
}

export default function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      {/* 令牌驱动公开流（E11，FE-T4）：一律不挂守卫——邀请接受会种新 V2 会话
          （存量 V1 用户点邀请链接是合法路径）、pending 用户会话有效（挂 guest 门
          会令验证链接永远弹回）；独立于壳外，同 /login 全屏构图。 */}
      <Route path="/invitations/accept" element={<InvitationAcceptPage />} />
      <Route path="/email-verification" element={<EmailVerificationPage />} />
      {/* 令牌驱动公开流（E11，FE-T5）：一律不挂守卫——重置/撤销用户此时匿名或
          会话已失效（重置 confirm 后全会话失效、注销 deleting 全会话失效），挂门
          必弹回；独立于壳外，同 /login 全屏构图。 */}
      <Route path="/password-reset" element={<PasswordResetRequestPage />} />
      <Route path="/password-reset/confirm" element={<PasswordResetConfirmPage />} />
      <Route path="/account/deletion/cancel" element={<DeletionCancelPage />} />
      <Route element={<AppShell />}>
        <Route
          path="/"
          element={
            <RequireAuth>
              <HomePage />
            </RequireAuth>
          }
        />
        <Route path="/discover" element={<ExpertCenterPage />} />
        <Route path="/discover/:id" element={<ExpertDetailRoute />} />
        <Route
          path="/profile"
          element={
            <RequireAuth>
              <ProfilePage />
            </RequireAuth>
          }
        />
        <Route
          path="/my-experts"
          element={
            <RequireAuth requireExpert>
              <MyExpertsPage />
            </RequireAuth>
          }
        />
        <Route
          path="/my-experts/new"
          element={
            <RequireAuth requireExpert>
              <ExpertEditPage />
            </RequireAuth>
          }
        />
        <Route
          path="/my-experts/:id/edit"
          element={
            <RequireAuth requireExpert>
              <ExpertEditRoute />
            </RequireAuth>
          }
        />
        <Route
          path="/skills"
          element={
            <RequireAuth requireExpert>
              <SkillManagePage />
            </RequireAuth>
          }
        />
        <Route
          path="/settings/providers"
          element={
            <RequireAuth>
              <ProviderSettingsPage />
            </RequireAuth>
          }
        />
        <Route
          path="/tasks"
          element={
            <RequireAuth>
              <TaskChatPage />
            </RequireAuth>
          }
        />
        <Route
          path="/tasks/new"
          element={
            <RequireAuth>
              <TaskCreatePage />
            </RequireAuth>
          }
        />
        <Route
          path="/tasks/:id"
          element={
            <RequireAuth>
              <TaskChatRoute />
            </RequireAuth>
          }
        />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
