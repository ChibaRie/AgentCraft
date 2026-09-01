import { Navigate, Route, Routes, useParams } from "react-router-dom";
import ExpertCenterPage from "./pages/ExpertCenterPage.jsx";
import ExpertDetailPage from "./pages/ExpertDetailPage.jsx";
import ExpertEditPage from "./pages/ExpertEditPage.jsx";
import HomePage from "./pages/HomePage.jsx";
import LoginPage from "./pages/LoginPage.jsx";
import MyExpertsPage from "./pages/MyExpertsPage.jsx";
import ProfilePage from "./pages/ProfilePage.jsx";
import SkillManagePage from "./pages/SkillManagePage.jsx";
import TaskChatPage from "./pages/TaskChatPage.jsx";

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
  return <TaskChatPage taskId={id} />;
}

export default function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/" element={<HomePage />} />
      <Route path="/discover" element={<ExpertCenterPage />} />
      <Route path="/discover/:id" element={<ExpertDetailRoute />} />
      <Route path="/profile" element={<ProfilePage />} />
      <Route path="/my-experts" element={<MyExpertsPage />} />
      <Route path="/my-experts/new" element={<ExpertEditPage />} />
      <Route path="/my-experts/:id/edit" element={<ExpertEditRoute />} />
      <Route path="/skills" element={<SkillManagePage />} />
      <Route path="/tasks/:id" element={<TaskChatRoute />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
