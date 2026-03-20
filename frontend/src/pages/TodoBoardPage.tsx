import { useEffect, useState, useRef, useMemo } from 'react'
import { useParams, Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useTodoStore } from '../stores/todoStore'
import { providers as providersApi } from '../services/api'
import { Plus, ListTodo, Clock } from 'lucide-react'
import { EmptyState } from '../components/ui/EmptyState'
import type { TodoItem, TodoState, ProviderConfig, ModelInfo } from '../types'

/* ── Group ordering & styling ─────────────────────────────────── */

const GROUPS: { state: TodoState; label: string; accent: string; dotColor: string }[] = [
  { state: 'scheduled',   label: 'Scheduled',    accent: 'border-l-indigo-500', dotColor: 'bg-indigo-500' },
  { state: 'intake',      label: 'Intake',       accent: 'border-l-violet-500', dotColor: 'bg-violet-500' },
  { state: 'planning',    label: 'Planning',     accent: 'border-l-blue-500',   dotColor: 'bg-blue-500' },
  { state: 'plan_ready',  label: 'Plan Review',  accent: 'border-l-cyan-500',   dotColor: 'bg-cyan-500' },
  { state: 'in_progress', label: 'In Progress',  accent: 'border-l-amber-500',  dotColor: 'bg-amber-500' },
  { state: 'testing',     label: 'Testing',      accent: 'border-l-teal-500',   dotColor: 'bg-teal-500' },
  { state: 'review',      label: 'Review',       accent: 'border-l-orange-500', dotColor: 'bg-orange-500' },
  { state: 'completed',   label: 'Completed',    accent: 'border-l-emerald-500', dotColor: 'bg-emerald-500' },
  { state: 'failed',      label: 'Failed',       accent: 'border-l-red-500',    dotColor: 'bg-red-500' },
  { state: 'cancelled',   label: 'Cancelled',    accent: 'border-l-gray-600',   dotColor: 'bg-gray-600' },
]


const PRIORITY_CONFIG: Record<string, { color: string; dot: string; label: string; weight: number }> = {
  critical: { color: 'text-red-400', dot: 'bg-red-400', label: 'Critical', weight: 0 },
  high:     { color: 'text-orange-400', dot: 'bg-orange-400', label: 'High', weight: 1 },
  medium:   { color: 'text-gray-400', dot: 'bg-gray-500', label: 'Medium', weight: 2 },
  low:      { color: 'text-gray-600', dot: 'bg-gray-600', label: 'Low', weight: 3 },
}

type DateFilter = 'all' | 'today' | 'week' | 'month'
type SortMode = 'priority' | 'newest' | 'oldest'

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
  const [newModel, setNewModel] = useState('')
  const [availableModels, setAvailableModels] = useState<ModelInfo[]>([])
  const [loadingModels, setLoadingModels] = useState(false)
  const [scheduleEnabled, setScheduleEnabled] = useState(false)
  const [scheduledAt, setScheduledAt] = useState('')
  const [searchQuery, setSearchQuery] = useState('')
  const [sortMode, setSortMode] = useState<SortMode>('priority')
  const titleRef = useRef<HTMLInputElement>(null)

  // Filter state from URL
  const dateFilter = (searchParams.get('date') as DateFilter) || 'all'

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
    if (!newProviderId) {
      setAvailableModels([])
      setNewModel('')
      return
    }
    setLoadingModels(true)
    providersApi.listModels(newProviderId)
      .then(res => {
        setAvailableModels(res.models)
        const defaultModel = res.models.find(m => m.is_default)
        setNewModel(defaultModel?.id || '')
      })
      .catch(() => {
        setAvailableModels([])
        setNewModel('')
      })
      .finally(() => setLoadingModels(false))
  }, [newProviderId])

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

  // Search filter
  const searchFiltered = useMemo(() => {
    if (!searchQuery.trim()) return dateFilteredTodos
    const q = searchQuery.toLowerCase()
    return dateFilteredTodos.filter(
      (t) => t.title.toLowerCase().includes(q) || (t.description && t.description.toLowerCase().includes(q))
    )
  }, [dateFilteredTodos, searchQuery])

  // Sort
  const sortedTodos = useMemo(() => {
    const list = [...searchFiltered]
    switch (sortMode) {
      case 'priority':
        list.sort((a, b) => {
          const wa = PRIORITY_CONFIG[a.priority]?.weight ?? 2
          const wb = PRIORITY_CONFIG[b.priority]?.weight ?? 2
          if (wa !== wb) return wa - wb
          return new Date(b.updated_at || b.created_at).getTime() - new Date(a.updated_at || a.created_at).getTime()
        })
        break
      case 'newest':
        list.sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())
        break
      case 'oldest':
        list.sort((a, b) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime())
        break
    }
    return list
  }, [searchFiltered, sortMode])

  // Always show all state columns so the full pipeline is visible
  const groupedTodos = useMemo(() => {
    return GROUPS.map((group) => ({
      ...group,
      items: sortedTodos.filter((t) => t.state === group.state),
    }))
  }, [sortedTodos])

  const failedCount = projectTodos.filter((t) => t.state === 'failed').length

  const handleCreate = async () => {
    if (!projectId || !newTitle.trim()) return
    try {
      await createTodo(projectId, {
        title: newTitle.trim(),
        description: newDesc.trim() || undefined,
        priority: newPriority,
        task_type: newType,
        ai_provider_id: newProviderId || undefined,
        ai_model: newModel || undefined,
        scheduled_at: scheduleEnabled && scheduledAt ? new Date(scheduledAt).toISOString() : undefined,
      })
      setNewTitle('')
      setNewDesc('')
      setNewPriority('medium')
      setNewType('code')
      setNewProviderId('')
      setNewModel('')
      setAvailableModels([])
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

  const handleRetry = async (todoId: string) => {
    await retryTodo(todoId)
    if (projectId) fetchTodos(projectId)
  }

  const hasFilters = searchQuery.trim() !== '' || dateFilter !== 'all'
  const noResults = hasFilters && sortedTodos.length === 0

  return (
    <div className="p-4 md:p-6 h-full flex flex-col">
      {/* Header */}
      <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between mb-4">
        <div className="flex items-center gap-4">
          <div>
            <h1 className="text-lg font-semibold text-white">Tasks</h1>
            <p className="text-xs text-gray-600 mt-0.5">
              {projectTodos.length} task{projectTodos.length !== 1 ? 's' : ''}
              {failedCount > 0 && (
                <span className="text-red-400/60 ml-2">{failedCount} needs attention</span>
              )}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {isLoading && (
            <div className="flex items-center gap-1.5 text-gray-600 text-xs">
              <Spinner size="sm" />
              <span>Syncing</span>
            </div>
          )}
          <div className="relative">
            <svg className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-gray-600 pointer-events-none" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z" />
            </svg>
            <input
              type="text"
              placeholder="Search..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="w-40 focus:w-56 pl-8 pr-3 py-1.5 bg-gray-900 border border-gray-800 rounded-lg text-xs text-white placeholder-gray-600 focus:outline-none focus:border-indigo-500 transition-all"
            />
          </div>
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
            className="px-4 py-1.5 bg-indigo-600 hover:bg-indigo-500 rounded-lg text-sm font-medium text-white transition-colors flex items-center gap-1.5"
          >
            <Plus className="w-4 h-4" />
            New Task
          </button>
        </div>
      </div>

      {/* Filters */}
      <div className="flex items-center gap-2 mb-4">
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
        <select
          value={sortMode}
          onChange={(e) => setSortMode(e.target.value as SortMode)}
          className="px-2.5 py-1 bg-gray-900 border border-gray-800 rounded-lg text-[11px] text-gray-400 focus:outline-none focus:border-indigo-500 transition-colors"
        >
          <option value="priority">Sort: Priority</option>
          <option value="newest">Sort: Newest</option>
          <option value="oldest">Sort: Oldest</option>
        </select>
      </div>

      {/* Create Task Panel */}
      {showCreate && (
        <div className="mb-5 p-4 md:p-5 bg-gray-900 rounded-xl border border-gray-800 animate-fade-in-up" onKeyDown={handleKeyDown}>
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
            <label className="block text-[11px] text-gray-600 mb-1">AI Provider</label>
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
            {newProviderId && (
              <div className="mt-2">
                <label className="block text-[11px] text-gray-600 mb-1">Model</label>
                <select
                  value={newModel}
                  onChange={(e) => setNewModel(e.target.value)}
                  disabled={loadingModels}
                  className="w-full px-3 py-1.5 bg-gray-950 border border-gray-800 rounded-lg text-sm text-white focus:outline-none focus:border-indigo-500 transition-colors disabled:opacity-40"
                >
                  {loadingModels ? (
                    <option>Loading models...</option>
                  ) : (
                    availableModels.map(m => (
                      <option key={m.id} value={m.id}>
                        {m.name}{m.is_default ? ' (default)' : ''}
                      </option>
                    ))
                  )}
                </select>
              </div>
            )}
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

      {/* Kanban Board */}
      <div className="flex-1 overflow-hidden">
        {projectTodos.length === 0 && !isLoading && (
          <EmptyState
            icon={<ListTodo />}
            title="No tasks yet"
            message="Create a task or use Chat to get started"
            action={{ label: 'New Task', onClick: () => { setShowCreate(true); clearCreateError() } }}
            size="lg"
          />
        )}

        {projectTodos.length > 0 && noResults && (
          <div className="flex flex-col items-center justify-center py-16">
            <p className="text-sm text-gray-500 mb-1.5">No tasks match the current filters</p>
            <button
              onClick={() => { setSearchQuery(''); setDateFilter('all') }}
              className="text-xs text-indigo-400 hover:text-indigo-300 transition-colors"
            >
              Clear filters
            </button>
          </div>
        )}

        {projectTodos.length > 0 && (
          <div className="flex gap-3 h-full overflow-x-auto pb-4 pr-4">
            {groupedTodos.map((group) => (
              <div key={group.state} className="flex flex-col w-72 shrink-0">
                <ColumnHeader label={group.label} count={group.items.length} dotColor={group.dotColor} />
                <div className="flex-1 overflow-y-auto space-y-2 pr-1">
                  {group.items.length === 0 && (
                    <div className="py-6 text-center text-[11px] text-gray-700 border border-dashed border-gray-800/50 rounded-lg">
                      No tasks
                    </div>
                  )}
                  {group.items.map((todo) => (
                    <TaskCard key={todo.id} todo={todo} onRetry={() => handleRetry(todo.id)} />
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

/* ── Column Header ────────────────────────────────────────────── */

function ColumnHeader({ label, count, dotColor }: { label: string; count: number; dotColor: string }) {
  return (
    <div className="flex items-center gap-2 mb-3 px-1">
      <span className={`w-2 h-2 rounded-full shrink-0 ${dotColor}`} />
      <span className="text-xs font-medium text-gray-400 uppercase tracking-wider">{label}</span>
      <span className="text-[10px] text-gray-600 bg-gray-800 px-1.5 py-0.5 rounded-full">{count}</span>
    </div>
  )
}

/* ── Task Card ────────────────────────────────────────────────── */

function TaskCard({ todo, onRetry }: { todo: TodoItem; onRetry: () => void }) {
  const priority = PRIORITY_CONFIG[todo.priority] || PRIORITY_CONFIG.medium
  const completedCount = todo.sub_tasks?.filter((s) => s.status === 'completed').length ?? 0
  const totalCount = todo.sub_tasks?.length ?? 0
  const progressPct = totalCount > 0 ? (completedCount / totalCount) * 100 : 0
  const timeAgo = getRelativeTime(todo.updated_at || todo.created_at)
  const isFailed = todo.state === 'failed'
  const isCancelled = todo.state === 'cancelled'
  const isActive = todo.state === 'in_progress' || todo.state === 'testing'

  return (
    <Link
      to={`/todos/${todo.id}`}
      className={`group block px-3 py-2.5 rounded-lg border transition-all hover:shadow-card ${
        isFailed
          ? 'bg-gray-900 border-red-500/10 hover:border-red-500/20'
          : 'bg-gray-900 border-gray-800/50 hover:border-gray-700'
      }`}
    >
      {/* Priority + Title */}
      <div className="flex items-start gap-2 min-w-0 mb-1.5">
        <span className={`w-1.5 h-1.5 rounded-full shrink-0 mt-1.5 ${priority.dot}`} title={priority.label} />
        <span className={`flex-1 text-sm leading-snug transition-colors ${
          isCancelled
            ? 'text-gray-600 line-through'
            : 'text-gray-300 group-hover:text-white'
        }`}>
          {todo.title}
        </span>
        {isActive && (
          <span className={`w-2 h-2 rounded-full shrink-0 mt-1 ${
            GROUPS.find(g => g.state === todo.state)?.dotColor || 'bg-gray-500'
          } animate-pulse`} />
        )}
      </div>

      {/* Metadata row */}
      <div className="flex items-center gap-1.5 flex-wrap">
        <span className="text-[10px] text-gray-600">{todo.task_type}</span>
        {todo.sub_state && (
          <span className="px-1.5 py-0.5 bg-gray-800 rounded text-[10px] text-indigo-400/70 font-mono">
            {todo.sub_state}
          </span>
        )}
        {todo.provider_model && (
          <span className="text-[10px] text-gray-700 font-mono truncate max-w-[100px]" title={todo.provider_model}>
            {todo.provider_model}
          </span>
        )}
        {todo.state === 'scheduled' && todo.scheduled_at && (
          <span className="text-[10px] text-indigo-400/70" title={new Date(todo.scheduled_at).toLocaleString()}>
            <Clock className="w-3 h-3 inline" />
          </span>
        )}
        <span className="ml-auto text-[10px] text-gray-700 tabular-nums shrink-0">{timeAgo}</span>
      </div>

      {/* Progress bar */}
      {totalCount > 0 && (
        <div className="flex items-center gap-2 mt-2">
          <div className="flex-1 bg-gray-800 rounded-full h-1 overflow-hidden">
            <div
              className="h-full rounded-full transition-all duration-500"
              style={{
                width: `${progressPct}%`,
                backgroundColor: progressPct === 100 ? '#10b981' : '#6366f1',
              }}
            />
          </div>
          <span className="text-[10px] text-gray-600 tabular-nums">{completedCount}/{totalCount}</span>
        </div>
      )}

      {/* Error snippet + retry */}
      {isFailed && (
        <div className="mt-2 flex items-center gap-2">
          {todo.error_message && (
            <span className="flex-1 text-[10px] text-red-400/60 font-mono truncate">
              {todo.error_message}
            </span>
          )}
          <button
            onClick={(e) => { e.preventDefault(); e.stopPropagation(); onRetry() }}
            className="text-[10px] px-2 py-0.5 rounded text-indigo-400 hover:text-indigo-300 hover:bg-indigo-500/10 transition-colors shrink-0"
          >
            Retry
          </button>
        </div>
      )}
    </Link>
  )
}

/* ── Helpers ───────────────────────────────────────────────────── */

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
