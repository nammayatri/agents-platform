import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useAuthStore } from '../stores/authStore'
import { admin as adminApi } from '../services/api'
import type { TodoItem } from '../types'

const STATE_DOT_COLORS: Record<string, string> = {
  intake: 'bg-violet-500',
  planning: 'bg-blue-500',
  plan_ready: 'bg-cyan-500',
  in_progress: 'bg-amber-500',
  review: 'bg-orange-500',
  completed: 'bg-emerald-500',
  failed: 'bg-red-500',
  cancelled: 'bg-gray-500',
}

export default function DashboardPage() {
  const { user } = useAuthStore()
  const [stats, setStats] = useState<Record<string, number>>({})
  const [recentTodos, setRecentTodos] = useState<TodoItem[]>([])

  useEffect(() => {
    if (user?.role === 'admin') {
      adminApi.stats().then(setStats)
      adminApi.todos().then((t) => setRecentTodos((t as TodoItem[]).slice(0, 20)))
    }
  }, [user])

  const statCards = [
    { label: 'Total Tasks', value: stats.total_todos, accent: 'border-l-gray-500' },
    { label: 'Active', value: stats.active_todos, accent: 'border-l-amber-500' },
    { label: 'Completed', value: stats.completed_todos, accent: 'border-l-emerald-500' },
    { label: 'Failed', value: stats.failed_todos, accent: 'border-l-red-500' },
  ]

  return (
    <div className="p-6 max-w-5xl mx-auto">
      <div className="mb-8">
        <h1 className="text-lg font-semibold text-white">Dashboard</h1>
        <p className="text-xs text-gray-600 mt-0.5">
          {user?.role === 'admin' ? 'System overview across all users' : 'Select a project from the sidebar to get started'}
        </p>
      </div>

      {user?.role === 'admin' && (
        <>
          <div className="grid grid-cols-4 gap-3 mb-8">
            {statCards.map((s) => (
              <div key={s.label} className={`p-4 bg-gray-900 rounded-lg border-l-2 ${s.accent} border border-gray-800/50`}>
                <div className="text-2xl font-semibold text-white tabular-nums">{s.value ?? '-'}</div>
                <div className="text-xs text-gray-500 mt-1">{s.label}</div>
              </div>
            ))}
          </div>

          <div className="mb-4">
            <h2 className="text-sm font-medium text-gray-300 mb-3 uppercase tracking-wider">Recent Tasks</h2>
          </div>
          <div className="space-y-1">
            {recentTodos.map((todo) => (
              <Link
                key={todo.id}
                to={`/todos/${todo.id}`}
                className="flex items-center gap-3 px-4 py-2.5 bg-gray-900 rounded-lg border border-gray-800/30 hover:border-gray-700 transition-colors group"
              >
                <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${STATE_DOT_COLORS[todo.state] || 'bg-gray-500'}`} />
                <span className="text-sm text-gray-300 group-hover:text-white transition-colors flex-1 truncate">
                  {todo.title}
                </span>
                <span className="text-[11px] text-gray-600 font-mono">{todo.state.replace('_', ' ')}</span>
                <span className="text-[11px] text-gray-700">
                  {new Date(todo.created_at).toLocaleDateString()}
                </span>
              </Link>
            ))}
          </div>
        </>
      )}

      {user?.role !== 'admin' && (
        <div className="py-16 text-center">
          <p className="text-sm text-gray-600 mb-2">No projects selected</p>
          <p className="text-xs text-gray-700">
            Create a new project or select one from the sidebar to view and manage tasks.
          </p>
        </div>
      )}
    </div>
  )
}
