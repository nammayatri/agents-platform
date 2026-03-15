import { useEffect, useState, useRef } from 'react'
import { useParams, Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useTodoStore } from '../stores/todoStore'
import type { TodoItem, TodoState, ProviderConfig } from '../types'

const COLUMNS: { state: TodoState; label: string; accent: string }[] = [
  { state: 'scheduled', label: 'Scheduled', accent: 'border-l-indigo-500' },
  { state: 'intake', label: 'Intake', accent: 'border-l-violet-500' },
  { state: 'planning', label: 'Planning', accent: 'border-l-blue-500' },
  { state: 'plan_ready', label: 'Plan Review', accent: 'border-l-cyan-500' },
  { state: 'in_progress', label: 'In Progress', accent: 'border-l-amber-500' },
  { state: 'review', label: 'Review', accent: 'border-l-orange-500' },
  { state: 'completed', label: 'Done', accent: 'border-l-emerald-500' },
]

const ACTIVE_STATES: TodoState[] = ['scheduled', 'intake', 'planning', 'plan_ready', 'in_progress', 'review']

const PRIORITY_CONFIG: Record<string, { color: string; dot: string; label: string }> = {
  critical: { color: 'text-red-400', dot: 'bg-red-400', label: 'Critical' },
  high: { color: 'text-orange-400', dot: 'bg-orange-400', label: 'High' },
  medium: { color: 'text-gray-400', dot: 'bg-gray-500', label: 'Medium' },
  low: { color: 'text-gray-600', dot: 'bg-gray-600', label: 'Low' },
}

const STATE_DOT_COLORS: Record<string, string> = {
  scheduled: 'bg-indigo-500',
  intake: 'bg-violet-500',
  planning: 'bg-blue-500',
  plan_ready: 'bg-cyan-500',
  in_progress: 'bg-amber-500',
  review: 'bg-orange-500',
  completed: 'bg-emerald-500',
  failed: 'bg-red-500',
  cancelled: 'bg-gray-500',
}

type ViewFilter = 'active' | 'all' | 'completed'
type DateFilter = 'all' | 'today' | 'week' | 'month'

export default function TodoBoardPage() {
  const { projectId } = useParams<{ projectId: string }>()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const { todos, fetchTodos, createTodo, retryTodo, isLoading, isCreating, createError, clearCreateError, providers, fetchProviders } = useTodoStore()
  const [showCreate, setShowCreate] = useState(false)
  const [newTitle, setNewTitle] = useState('')
  const [newDesc, setNewDesc] = useState('')
  const [newPriority, setNewPriority] = useState('medium')
  const [newType, setNewType] = useState('code')
  const [newProviderId, setNewProviderId] = useState('')
  const [scheduleEnabled, setScheduleEnabled] = useState(false)
  const [scheduledAt, setScheduledAt] = useState('')
  const [showTrouble, setShowTrouble] = useState(true)
  const [showCancelled, setShowCancelled] = useState(false)
  const titleRef = useRef<HTMLInputElement>(null)

  // Filter state from URL
  const viewFilter = (searchParams.get('view') as ViewFilter) || 'active'
  const dateFilter = (searchParams.get('date') as DateFilter) || 'all'

  const setViewFilter = (v: ViewFilter) => {
    const params = new URLSearchParams(searchParams)
    params.set('view', v)
    setSearchParams(params)
  }
  const setDateFilter = (d: DateFilter) => {
    const params = new URLSearchParams(searchParams)
    params.set('date', d)
    setSearchParams(params)
  }

  useEffect(() => {
    if (projectId) fetchTodos(projectId)
    fetchProviders()
  }, [projectId, fetchTodos, fetchProviders])

  const activeProviders = providers.filter((p: ProviderConfig) => p.is_active)

  useEffect(() => {
    if (showCreate && titleRef.current) titleRef.current.focus()
  }, [showCreate])

  useEffect(() => {
    if (!projectId) return
    const interval = setInterval(() => fetchTodos(projectId), 10000)
    return () => clearInterval(interval)
  }, [projectId, fetchTodos])

  const projectTodos = Object.values(todos).filter((t) => t.project_id === projectId)

  // Date filtering
  const getDateCutoff = (): number | null => {
    const now = Date.now()
    switch (dateFilter) {
      case 'today': return now - 24 * 60 * 60 * 1000
      case 'week': return now - 7 * 24 * 60 * 60 * 1000
      case 'month': return now - 30 * 24 * 60 * 60 * 1000
      default: return null
    }
  }
  const dateCutoff = getDateCutoff()
  const dateFilteredTodos = dateCutoff
    ? projectTodos.filter((t) => new Date(t.created_at).getTime() >= dateCutoff)
    : projectTodos

  // Separate failed, cancelled from main view
  const failedTodos = dateFilteredTodos.filter((t) => t.state === 'failed')
  const cancelledTodos = dateFilteredTodos.filter((t) => t.state === 'cancelled')
  const mainTodos = dateFilteredTodos.filter((t) => t.state !== 'failed' && t.state !== 'cancelled')

  // Determine visible columns based on filter
  const visibleColumns = viewFilter === 'active'
    ? COLUMNS.filter((c) => ACTIVE_STATES.includes(c.state))
    : viewFilter === 'completed'
      ? COLUMNS.filter((c) => c.state === 'completed')
      : COLUMNS

  const handleCreate = async () => {
    if (!projectId || !newTitle.trim()) return
    try {
      await createTodo(projectId, {
        title: newTitle.trim(),
        description: newDesc.trim() || undefined,
        priority: newPriority,
        task_type: newType,
        ai_provider_id: newProviderId || undefined,
        scheduled_at: scheduleEnabled && scheduledAt ? new Date(scheduledAt).toISOString() : undefined,
      })
      setNewTitle('')
      setNewDesc('')
      setNewPriority('medium')
      setNewType('code')
      setNewProviderId('')
      setScheduleEnabled(false)
      setScheduledAt('')
      setShowCreate(false)
    } catch {
      // Error captured in store
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) handleCreate()
    if (e.key === 'Escape') { setShowCreate(false); clearCreateError() }
  }

  return (
    <div className="p-6 h-full flex flex-col">
      {/* Header */}
      <div className="flex items-center justify-between mb-4">
        <div>
          <h1 className="text-lg font-semibold text-white">Tasks</h1>
          <p className="text-xs text-gray-600 mt-0.5">
            {projectTodos.length} task{projectTodos.length !== 1 ? 's' : ''}
            {failedTodos.length > 0 && (
              <span className="text-red-400/80 ml-2">{failedTodos.length} failed</span>
            )}
          </p>
        </div>
        <div className="flex items-center gap-3">
          {isLoading && (
            <div className="flex items-center gap-1.5 text-gray-600 text-xs">
              <Spinner size="sm" />
              <span>Syncing</span>
            </div>
          )}
          <button
            onClick={() => navigate(`/projects/${projectId}/chat`)}
            className="px-3 py-1.5 bg-gray-800 hover:bg-gray-700 rounded-lg text-sm text-gray-300 transition-colors flex items-center gap-1.5"
          >
            <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M7.5 8.25h9m-9 3H12m-9.75 1.51c0 1.6 1.123 2.994 2.707 3.227 1.129.166 2.27.293 3.423.379.35.026.67.21.865.501L12 21l2.755-4.133a1.14 1.14 0 01.865-.501 48.172 48.172 0 003.423-.379c1.584-.233 2.707-1.626 2.707-3.228V6.741c0-1.602-1.123-2.995-2.707-3.228A48.394 48.394 0 0012 3c-2.392 0-4.744.175-7.043.513C3.373 3.746 2.25 5.14 2.25 6.741v6.018z" />
            </svg>
            Chat
          </button>
          <button
            onClick={() => { setShowCreate(true); clearCreateError() }}
            className="px-4 py-1.5 bg-indigo-600 hover:bg-indigo-500 rounded-lg text-sm font-medium text-white transition-colors"
          >
            New Task
          </button>
        </div>
      </div>

      {/* Filters */}
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-1">
          {(['active', 'all', 'completed'] as const).map((v) => (
            <button
              key={v}
              onClick={() => setViewFilter(v)}
              className={`px-3 py-1 rounded-lg text-xs font-medium transition-colors ${
                viewFilter === v
                  ? 'bg-indigo-600/20 text-indigo-400'
                  : 'text-gray-600 hover:text-gray-400 hover:bg-gray-900'
              }`}
            >
              {v === 'active' ? 'Active' : v === 'all' ? 'All' : 'Completed'}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-1">
          {(['all', 'today', 'week', 'month'] as const).map((d) => (
            <button
              key={d}
              onClick={() => setDateFilter(d)}
              className={`px-2.5 py-1 rounded-lg text-[11px] transition-colors ${
                dateFilter === d
                  ? 'bg-gray-800 text-gray-300'
                  : 'text-gray-700 hover:text-gray-500'
              }`}
            >
              {d === 'all' ? 'All time' : d === 'today' ? 'Today' : d === 'week' ? 'This week' : 'This month'}
            </button>
          ))}
        </div>
      </div>

      {/* Create Task Panel */}
      {showCreate && (
        <div className="mb-5 p-5 bg-gray-900 rounded-xl border border-gray-800" onKeyDown={handleKeyDown}>
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-sm font-medium text-gray-300">New Task</h2>
            <span className="text-[11px] text-gray-600">
              {navigator.platform.includes('Mac') ? 'Cmd' : 'Ctrl'}+Enter to submit
            </span>
          </div>
          <input
            ref={titleRef}
            className="w-full px-3 py-2 bg-gray-950 border border-gray-800 rounded-lg text-white text-sm placeholder-gray-600 focus:outline-none focus:border-indigo-500 transition-colors mb-3"
            placeholder="What needs to be done?"
            value={newTitle}
            onChange={(e) => setNewTitle(e.target.value)}
          />
          <textarea
            className="w-full px-3 py-2 bg-gray-950 border border-gray-800 rounded-lg text-white text-sm placeholder-gray-600 focus:outline-none focus:border-indigo-500 transition-colors mb-3 resize-none"
            placeholder="Add details (optional -- the AI will ask clarifying questions)"
            value={newDesc}
            onChange={(e) => setNewDesc(e.target.value)}
            rows={2}
          />
          <div className="flex items-center gap-3 mb-3">
            <div className="flex-1">
              <label className="block text-[11px] text-gray-600 mb-1">Priority</label>
              <select
                value={newPriority}
                onChange={(e) => setNewPriority(e.target.value)}
                className="w-full px-3 py-1.5 bg-gray-950 border border-gray-800 rounded-lg text-sm text-white focus:outline-none focus:border-indigo-500 transition-colors"
              >
                <option value="critical">Critical</option>
                <option value="high">High</option>
                <option value="medium">Medium</option>
                <option value="low">Low</option>
              </select>
            </div>
            <div className="flex-1">
              <label className="block text-[11px] text-gray-600 mb-1">Type</label>
              <select
                value={newType}
                onChange={(e) => setNewType(e.target.value)}
                className="w-full px-3 py-1.5 bg-gray-950 border border-gray-800 rounded-lg text-sm text-white focus:outline-none focus:border-indigo-500 transition-colors"
              >
                <option value="code">Code</option>
                <option value="research">Research</option>
                <option value="document">Document</option>
                <option value="general">General</option>
              </select>
            </div>
          </div>

          <div className="mb-4">
            <label className="block text-[11px] text-gray-600 mb-1">AI Model</label>
            <select
              value={newProviderId}
              onChange={(e) => setNewProviderId(e.target.value)}
              className="w-full px-3 py-1.5 bg-gray-950 border border-gray-800 rounded-lg text-sm text-white focus:outline-none focus:border-indigo-500 transition-colors"
            >
              <option value="">Auto (default provider)</option>
              {activeProviders.map((p: ProviderConfig) => (
                <option key={p.id} value={p.id}>{p.display_name} -- {p.default_model}</option>
              ))}
            </select>
          </div>

          <div className="mb-4">
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={scheduleEnabled}
                onChange={(e) => setScheduleEnabled(e.target.checked)}
                className="rounded border-gray-700 bg-gray-950 text-indigo-500 focus:ring-indigo-500"
              />
              <span className="text-[11px] text-gray-500">Schedule for later</span>
            </label>
            {scheduleEnabled && (
              <input
                type="datetime-local"
                value={scheduledAt}
                onChange={(e) => setScheduledAt(e.target.value)}
                className="mt-2 w-full px-3 py-1.5 bg-gray-950 border border-gray-800 rounded-lg text-sm text-white focus:outline-none focus:border-indigo-500 transition-colors"
              />
            )}
          </div>

          {createError && (
            <div className="mb-3 px-3 py-2 bg-red-500/10 border border-red-500/20 rounded-lg text-red-400 text-sm">
              {createError}
            </div>
          )}

          <div className="flex items-center gap-2">
            <button
              onClick={handleCreate}
              disabled={isCreating || !newTitle.trim()}
              className="px-4 py-1.5 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 disabled:cursor-not-allowed rounded-lg text-sm font-medium text-white transition-colors flex items-center gap-2"
            >
              {isCreating ? <><Spinner size="sm" /> Creating...</> : 'Create'}
            </button>
            <button
              onClick={() => { setShowCreate(false); clearCreateError() }}
              className="px-4 py-1.5 bg-gray-800 hover:bg-gray-700 rounded-lg text-sm text-gray-400 transition-colors"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {/* Failed Banner (only failed, not cancelled) */}
      {failedTodos.length > 0 && (
        <div className="mb-4">
          <button
            onClick={() => setShowTrouble(!showTrouble)}
            className="w-full flex items-center justify-between px-4 py-2.5 bg-red-500/5 hover:bg-red-500/8 border border-red-500/10 rounded-lg transition-colors"
          >
            <div className="flex items-center gap-2.5">
              <span className="w-1.5 h-1.5 rounded-full bg-red-400" />
              <span className="text-sm text-red-300/80">
                {failedTodos.length} failed
              </span>
            </div>
            <svg className={`w-3.5 h-3.5 text-gray-600 transition-transform ${showTrouble ? 'rotate-180' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
            </svg>
          </button>

          {showTrouble && (
            <div className="mt-2 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
              {failedTodos.map((todo) => (
                <TroubleCard key={todo.id} todo={todo} onRetry={async () => {
                  await retryTodo(todo.id)
                  if (projectId) fetchTodos(projectId)
                }} />
              ))}
            </div>
          )}
        </div>
      )}

      {/* Kanban Columns */}
      <div className="flex-1 flex gap-3 overflow-x-auto pb-4">
        {visibleColumns.map((col) => {
          const items = mainTodos.filter((t) => t.state === col.state)
          const isSlim = col.state === 'scheduled'
          return (
            <div key={col.state} className={`flex flex-col ${isSlim ? 'w-[160px] shrink-0' : 'flex-1 min-w-[220px]'}`}>
              <div className={`mb-3 pb-2 border-l-2 ${col.accent} pl-3 flex items-center justify-between`}>
                <span className="text-xs font-medium text-gray-400 uppercase tracking-wider">{col.label}</span>
                <span className="text-[11px] text-gray-600 tabular-nums">{items.length}</span>
              </div>

              <div className="flex-1 space-y-2 overflow-y-auto">
                {items.length === 0 && (
                  <div className="py-8 text-center text-xs text-gray-700">No tasks</div>
                )}
                {items.map((todo) => (
                  <TodoCard key={todo.id} todo={todo} slim={isSlim} />
                ))}
              </div>
            </div>
          )
        })}
      </div>

      {/* Cancelled — subtle section at bottom */}
      {cancelledTodos.length > 0 && (
        <div className="pt-2 border-t border-gray-900">
          <button
            onClick={() => setShowCancelled(!showCancelled)}
            className="flex items-center gap-1.5 text-[11px] text-gray-700 hover:text-gray-500 transition-colors"
          >
            <span className="w-1 h-1 rounded-full bg-gray-700" />
            {cancelledTodos.length} cancelled
            <svg className={`w-3 h-3 transition-transform ${showCancelled ? 'rotate-180' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
            </svg>
          </button>
          {showCancelled && (
            <div className="mt-1.5 space-y-1">
              {cancelledTodos.map((t) => (
                <Link
                  key={t.id}
                  to={`/todos/${t.id}`}
                  className="block px-3 py-1.5 text-[11px] text-gray-700 hover:text-gray-500 transition-colors truncate"
                >
                  {t.title}
                </Link>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function TodoCard({ todo, slim }: { todo: TodoItem; slim?: boolean }) {
  const priority = PRIORITY_CONFIG[todo.priority] || PRIORITY_CONFIG.medium
  const completedCount = todo.sub_tasks?.filter((s) => s.status === 'completed').length ?? 0
  const totalCount = todo.sub_tasks?.length ?? 0
  const progressPct = totalCount > 0 ? (completedCount / totalCount) * 100 : 0
  const timeAgo = getRelativeTime(todo.created_at)

  if (slim) {
    return (
      <Link
        to={`/todos/${todo.id}`}
        className="group block px-2.5 py-2 bg-gray-900 rounded-lg border border-gray-800/50 hover:border-gray-700 transition-all"
      >
        <div className="text-xs text-gray-300 leading-snug group-hover:text-white transition-colors truncate">
          {todo.title}
        </div>
        <div className="mt-1.5 flex items-center gap-1.5">
          <span className={`w-1.5 h-1.5 rounded-full ${priority.dot}`} />
          {todo.scheduled_at && (
            <span className="text-[10px] text-indigo-400/70 truncate">
              {new Date(todo.scheduled_at).toLocaleDateString()}
            </span>
          )}
          {!todo.scheduled_at && (
            <span className="text-[10px] text-gray-700">{timeAgo}</span>
          )}
        </div>
      </Link>
    )
  }

  return (
    <Link
      to={`/todos/${todo.id}`}
      className="group block p-3 bg-gray-900 rounded-lg border border-gray-800/50 hover:border-gray-700 transition-all"
    >
      <div className="text-sm text-gray-200 mb-2 leading-snug group-hover:text-white transition-colors">
        {todo.title}
      </div>

      <div className="flex items-center gap-2 flex-wrap">
        <span className="inline-flex items-center gap-1 text-[11px]">
          <span className={`w-1.5 h-1.5 rounded-full ${priority.dot}`} />
          <span className={priority.color}>{priority.label}</span>
        </span>
        <span className="text-[11px] text-gray-700">|</span>
        <span className="text-[11px] text-gray-600">{todo.task_type}</span>
        {todo.sub_state && (
          <>
            <span className="text-[11px] text-gray-700">|</span>
            <span className="text-[11px] text-indigo-400/70 font-mono">{todo.sub_state}</span>
          </>
        )}
      </div>

      {totalCount > 0 && (
        <div className="mt-2.5">
          <div className="w-full bg-gray-800 rounded-full h-1 overflow-hidden">
            <div
              className="h-full rounded-full transition-all duration-500"
              style={{
                width: `${progressPct}%`,
                backgroundColor: progressPct === 100 ? '#10b981' : '#6366f1',
              }}
            />
          </div>
          <div className="text-[11px] text-gray-600 mt-1">{completedCount}/{totalCount} sub-tasks</div>
        </div>
      )}

      {todo.state === 'scheduled' && todo.scheduled_at && (
        <div className="mt-2 flex items-center gap-1 text-[11px] text-indigo-400/70">
          <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 6v6h4.5m4.5 0a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
          <span>{new Date(todo.scheduled_at).toLocaleString()}</span>
        </div>
      )}

      <div className="mt-2 flex items-center gap-1.5 text-[11px] text-gray-700">
        {todo.provider_model && (
          <>
            <span className="text-gray-600 truncate max-w-[100px] font-mono" title={todo.provider_model}>
              {todo.provider_model}
            </span>
            <span>·</span>
          </>
        )}
        <span>{timeAgo}</span>
      </div>
    </Link>
  )
}

function TroubleCard({ todo, onRetry }: { todo: TodoItem; onRetry: () => void }) {
  const dotColor = STATE_DOT_COLORS[todo.state] || 'bg-gray-500'

  return (
    <div className="p-3 rounded-lg border transition-colors bg-gray-900 border-red-500/10">
      <div className="flex items-start justify-between gap-2 mb-1.5">
        <Link to={`/todos/${todo.id}`} className="text-sm text-gray-300 hover:text-white transition-colors leading-snug flex-1">
          {todo.title}
        </Link>
        <span className="flex items-center gap-1.5 shrink-0">
          <span className={`w-1.5 h-1.5 rounded-full ${dotColor}`} />
          <span className="text-[11px] text-gray-500">Failed</span>
        </span>
      </div>

      {todo.error_message && (
        <div className="mb-2 px-2.5 py-1.5 bg-gray-950 rounded text-[11px] text-red-300/60 font-mono leading-relaxed break-words">
          {todo.error_message}
        </div>
      )}

      <div className="flex items-center justify-between">
        <span className="text-[11px] text-gray-700">{getRelativeTime(todo.created_at)}</span>
        <button
          onClick={(e) => { e.preventDefault(); onRetry() }}
          className="text-[11px] px-2.5 py-1 rounded text-indigo-400 hover:text-indigo-300 hover:bg-indigo-500/10 transition-colors"
        >
          Retry
        </button>
      </div>
    </div>
  )
}

function Spinner({ size = 'md' }: { size?: 'sm' | 'md' }) {
  const sizeClass = size === 'sm' ? 'w-3 h-3' : 'w-4 h-4'
  return (
    <svg className={`${sizeClass} animate-spin`} viewBox="0 0 24 24" fill="none">
      <circle className="opacity-20" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" />
      <path className="opacity-80" d="M4 12a8 8 0 018-8" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
    </svg>
  )
}

function getRelativeTime(dateStr: string): string {
  const now = Date.now()
  const then = new Date(dateStr).getTime()
  const diffMs = now - then
  const diffSec = Math.floor(diffMs / 1000)
  const diffMin = Math.floor(diffSec / 60)
  const diffHr = Math.floor(diffMin / 60)
  const diffDay = Math.floor(diffHr / 24)

  if (diffSec < 10) return 'just now'
  if (diffSec < 60) return `${diffSec}s ago`
  if (diffMin < 60) return `${diffMin}m ago`
  if (diffHr < 24) return `${diffHr}h ago`
  if (diffDay < 30) return `${diffDay}d ago`
  return new Date(dateStr).toLocaleDateString()
}
