import { AuthProvider } from "./auth/AuthContext.jsx";
import AppRoutes from "./router.jsx";

export default function App() {
  return (
    <AuthProvider>
      <AppRoutes />
    </AuthProvider>
  );
}
