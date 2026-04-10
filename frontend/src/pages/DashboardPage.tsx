import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useAuthStore } from '../stores/authStore'
import { admin as adminApi } from '../services/api'
import { ListTodo, Play, CheckCircle2, AlertCircle, Clock, FolderOpen } from 'lucide-react'
import { EmptyState } from '../components/ui/EmptyState'
import { STATE_STYLES } from '../styles/classes'
import type { TodoItem } from '../types'

export default function DashboardPage() {
  const { user } = useAuthStore()
  const navigate = useNavigate()
  const [stats, setStats] = useState<Record<string, number>>({})
  const [recentTodos, setRecentTodos] = useState<TodoItem[]>([])

  useEffect(() => {
    if (user?.role === 'admin') {
      adminApi.stats().then(setStats).catch(() => {})
      adminApi.todos().then((t) => setRecentTodos((t as TodoItem[]).slice(0, 20))).catch(() => {})
    }
  }, [user?.role])

  const statCards = [
    { label: 'Total Tasks', value: stats.total_todos, accent: 'border-l-gray-500', iconBg: 'bg-gray-500/10', iconColor: 'text-gray-400', Icon: ListTodo },
    { label: 'Active', value: stats.active_todos, accent: 'border-l-amber-500', iconBg: 'bg-amber-500/10', iconColor: 'text-amber-400', Icon: Play },
    { label: 'Completed', value: stats.completed_todos, accent: 'border-l-emerald-500', iconBg: 'bg-emerald-500/10', iconColor: 'text-emerald-400', Icon: CheckCircle2 },
    { label: 'Failed', value: stats.failed_todos, accent: 'border-l-red-500', iconBg: 'bg-red-500/10', iconColor: 'text-red-400', Icon: AlertCircle },
  ]

  return (
    <div className="p-4 md:p-6 max-w-5xl mx-auto">
      {user?.role === 'admin' && (
        <>
          <div className="mb-8 animate-fade-in">
            <h1 className="text-xl font-semibold text-white">
              Welcome back{user.display_name ? `, ${user.display_name}` : ''}
            </h1>
            <p className="text-sm text-gray-500 mt-1">
              Here&apos;s what&apos;s happening across your workspace
            </p>
          </div>

          <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-8">
            {statCards.map((s, i) => (
              <div
                key={s.label}
                className={`p-4 bg-gray-900 rounded-lg border-l-2 ${s.accent} border border-gray-800/50 animate-fade-in`}
                style={{ animationDelay: `${i * 75}ms`, animationFillMode: 'both' }}
              >
                <div className="flex items-center gap-3">
                  <div className={`w-10 h-10 rounded-lg ${s.iconBg} flex items-center justify-center`}>
                    <s.Icon className={`w-5 h-5 ${s.iconColor}`} />
                  </div>
                  <div>
                    <div className="text-2xl font-semibold text-white tabular-nums">{s.value ?? '-'}</div>
                    <div className="text-xs text-gray-500">{s.label}</div>
                  </div>
                </div>
              </div>
            ))}
          </div>

          <div className="flex items-center gap-2 mb-3">
            <Clock className="w-4 h-4 text-gray-600" />
            <h2 className="text-sm font-medium text-gray-300">Recent Tasks</h2>
          </div>
          <div className="space-y-1 animate-fade-in">
            {recentTodos.length === 0 && (
              <EmptyState
                icon={<ListTodo />}
                message="No tasks created yet"
                size="sm"
              />
            )}
            {recentTodos.map((todo) => {
              const stateStyle = STATE_STYLES[todo.state]
              return (
                <Link
                  key={todo.id}
                  to={`/todos/${todo.id}`}
                  className="flex items-center gap-3 px-4 py-2.5 bg-gray-900 rounded-lg border border-gray-800/30 hover:border-gray-700 transition-colors group"
                >
                  <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${stateStyle?.dot || 'bg-gray-500'}`} />
                  <span className="text-sm text-gray-300 group-hover:text-white transition-colors flex-1 truncate">
                    {todo.title}
                  </span>
                  <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${stateStyle?.bg || 'bg-gray-800'} ${stateStyle?.text || 'text-gray-500'}`}>
                    {todo.state.replace('_', ' ')}
                  </span>
                  <span className="text-[11px] text-gray-700">
                    {new Date(todo.created_at).toLocaleDateString()}
                  </span>
                </Link>
              )
            })}
          </div>
        </>
      )}

      {user?.role !== 'admin' && (
        <div className="flex items-center justify-center min-h-[60vh]">
          <EmptyState
            icon={<FolderOpen />}
            title="Welcome to Agent Platform"
            message="Select a project from the sidebar to view tasks, or create a new project to get started."
            action={{ label: 'Create Project', onClick: () => navigate('/projects/new') }}
            size="lg"
          />
        </div>
      )}
    </div>
  )
}
