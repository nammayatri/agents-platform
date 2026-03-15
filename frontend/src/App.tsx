import { useEffect } from 'react'
import { Routes, Route, Navigate, Outlet } from 'react-router-dom'
import { useAuthStore } from './stores/authStore'
import LoginPage from './pages/LoginPage'
import DashboardPage from './pages/DashboardPage'
import TodoBoardPage from './pages/TodoBoardPage'
import TodoDetailPage from './pages/TodoDetailPage'
import SettingsPage from './pages/SettingsPage'
import ProjectSettingsPage from './pages/ProjectSettingsPage'
import ProjectChatPage from './pages/ProjectChatPage'
import AgentsPage from './pages/AgentsPage'
import AppShell from './components/layout/AppShell'

function ProtectedLayout() {
  const { isAuthenticated } = useAuthStore()
  if (!isAuthenticated) return <Navigate to="/login" replace />
  return (
    <AppShell>
      <Outlet />
    </AppShell>
  )
}

export default function App() {
  const { isAuthenticated, loadUser } = useAuthStore()

  useEffect(() => {
    if (isAuthenticated) loadUser()
  }, [isAuthenticated, loadUser])

  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route element={<ProtectedLayout />}>
        <Route index element={<DashboardPage />} />
        <Route path="projects/new" element={<ProjectSettingsPage />} />
        <Route path="projects/:projectId/settings" element={<ProjectSettingsPage />} />
        <Route path="projects/:projectId" element={<TodoBoardPage />} />
        <Route path="projects/:projectId/chat" element={<ProjectChatPage />} />
        <Route path="todos/:todoId" element={<TodoDetailPage />} />
        <Route path="agents" element={<AgentsPage />} />
        <Route path="settings" element={<SettingsPage />} />
      </Route>
    </Routes>
  )
}
