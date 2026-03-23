import { useEffect, useState, useRef } from 'react'
import { useParams } from 'react-router-dom'
import { Code2, Loader2, CheckCircle2, AlertCircle, XCircle, FileText, CirclePause, CircleDashed } from 'lucide-react'
import { useTodoStore } from '../stores/todoStore'
import { useTaskWebSocket } from '../hooks/useTaskWebSocket'
import { todos as todosApi } from '../services/api'
import DiffViewer from '../components/DiffViewer'
import WorkspaceView from '../components/workspace/WorkspaceView'
import ExecutionLog from '../components/workspace/ExecutionLog'
import ReviewFeedbackCard from '../components/chat/ReviewFeedbackCard'
import type { SubTask, ChatMessage, Deliverable, AgentRun, PlanSubTask, IterationLogEntry, ProgressLogEntry } from '../types'

const STATE_COLORS: Record<string, string> = {
  intake: 'bg-violet-600',
  planning: 'bg-blue-600',
  plan_ready: 'bg-cyan-600',
  in_progress: 'bg-amber-600',
  testing: 'bg-teal-600',
  review: 'bg-orange-600',
  completed: 'bg-emerald-600',
  failed: 'bg-red-600',
  cancelled: 'bg-gray-600',
}

const SUBTASK_COLORS: Record<string, string> = {
  pending: 'bg-gray-600',
  assigned: 'bg-blue-600',
  running: 'bg-amber-600',
  completed: 'bg-emerald-600',
  failed: 'bg-red-600',
  cancelled: 'bg-gray-600',
}

const ROLE_LABELS: Record<string, string> = {
  coder: 'Code',
  debugger: 'Debug',
  tester: 'Test',
  reviewer: 'Review',
  pr_creator: 'PR',
  report_writer: 'Report',
  merge_agent: 'Merge',
  merge_observer: 'Watch',
  release_build_watcher: 'Build',
  release_deployer: 'Deploy',
}

const VERDICT_COLORS: Record<string, string> = {
  approved: 'bg-emerald-500/10 text-emerald-400/80 border-emerald-500/20',
  needs_changes: 'bg-amber-500/10 text-amber-400/80 border-amber-500/20',
}

/** Safely render a value that might be an object (LLM output) as a string. */
function safeStr(v: unknown): string {
  if (v == null) return ''
  if (typeof v === 'string') return v
  if (typeof v === 'number' || typeof v === 'boolean') return String(v)
  if (typeof v === 'object' && 'name' in (v as Record<string, unknown>)) return String((v as Record<string, unknown>).name)
  return JSON.stringify(v)
}

const STATE_ICON_MAP: Record<string, React.ReactNode> = {
  intake: <CircleDashed className="w-4 h-4 text-violet-400" />,
  planning: <Loader2 className="w-4 h-4 text-blue-400 animate-spin" />,
  plan_ready: <FileText className="w-4 h-4 text-cyan-400" />,
  in_progress: <Loader2 className="w-4 h-4 text-amber-400 animate-spin" />,
  testing: <Loader2 className="w-4 h-4 text-teal-400 animate-spin" />,
  review: <CirclePause className="w-4 h-4 text-orange-400" />,
  completed: <CheckCircle2 className="w-4 h-4 text-emerald-400" />,
  failed: <AlertCircle className="w-4 h-4 text-red-400" />,
  cancelled: <XCircle className="w-4 h-4 text-gray-500" />,
}

const STATE_BANNER_BG: Record<string, string> = {
  intake: 'bg-violet-500/5',
  planning: 'bg-blue-500/5',
  plan_ready: 'bg-cyan-500/5',
  in_progress: 'bg-amber-500/5',
  testing: 'bg-teal-500/5',
  review: 'bg-orange-500/5',
  completed: 'bg-emerald-500/5',
  failed: 'bg-red-500/5',
  cancelled: 'bg-gray-500/5',
}

function CollapsibleSection({ title, count, defaultOpen = true, children }: {
  title: string; count?: number; defaultOpen?: boolean; children: React.ReactNode
}) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div className="mb-6">
      <button onClick={() => setOpen(!open)} className="flex items-center gap-2 w-full text-left mb-3">
        <span className="text-gray-600 text-[11px]">{open ? '\u25BC' : '\u25B6'}</span>
        <h2 className="text-sm font-medium text-gray-300 uppercase tracking-wider">{title}</h2>
        {count !== undefined && <span className="text-[10px] text-gray-600 font-mono">{count}</span>}
      </button>
      {open && children}
    </div>
  )
}

export default function TodoDetailPage() {
  const { todoId } = useParams<{ todoId: string }>()
  const todos = useTodoStore((s) => s.todos)
  const chatMessages = useTodoStore((s) => s.chatMessages)
  const deliverablesByTodo = useTodoStore((s) => s.deliverablesByTodo)
  const activityLogs = useTodoStore((s) => s.activityLogs)
  const fetchTodo = useTodoStore((s) => s.fetchTodo)
  const fetchChat = useTodoStore((s) => s.fetchChat)
  const fetchDeliverables = useTodoStore((s) => s.fetchDeliverables)
  const sendChat = useTodoStore((s) => s.sendChat)
  const cancelTodo = useTodoStore((s) => s.cancelTodo)
  const retryTodo = useTodoStore((s) => s.retryTodo)
  const triggerSubTask = useTodoStore((s) => s.triggerSubTask)
  const acceptDeliverables = useTodoStore((s) => s.acceptDeliverables)
  const requestChanges = useTodoStore((s) => s.requestChanges)
  const approvePlan = useTodoStore((s) => s.approvePlan)
  const rejectPlan = useTodoStore((s) => s.rejectPlan)
  const approveMerge = useTodoStore((s) => s.approveMerge)
  const rejectMerge = useTodoStore((s) => s.rejectMerge)
  const appendActivity = useTodoStore((s) => s.appendActivity)
  const resumeTodo = useTodoStore((s) => s.resumeTodo)
  const allExecutionEvents = useTodoStore((s) => s.executionEvents)
  const agentRunsByTodo = useTodoStore((s) => s.agentRunsByTodo)
  const fetchAgentRuns = useTodoStore((s) => s.fetchAgentRuns)

  useTaskWebSocket(todoId || null)

  const [chatInput, setChatInput] = useState('')
  const [changesFeedback, setChangesFeedback] = useState('')
  const [rejectFeedback, setRejectFeedback] = useState('')
  const [showRejectForm, setShowRejectForm] = useState(false)
  const [rejectMergeFeedback, setRejectMergeFeedback] = useState('')
  const [showRejectMergeForm, setShowRejectMergeForm] = useState(false)
  const [rejectReleaseFeedback, setRejectReleaseFeedback] = useState('')
  const [showRejectReleaseForm, setShowRejectReleaseForm] = useState(false)
  const [expandedSubTasks, setExpandedSubTasks] = useState<Set<string>>(new Set())
  const [expandedActivity, setExpandedActivity] = useState<Set<string>>(new Set())
  const [expandedDetails, setExpandedDetails] = useState<Set<string>>(new Set())
  const [showMobileChat, setShowMobileChat] = useState(false)
  const [showWorkspace, setShowWorkspace] = useState(false)
  const activityEndRefs = useRef<Record<string, HTMLDivElement | null>>({})
  const chatEndRef = useRef<HTMLDivElement>(null)

  const toggleSubTask = (id: string) => {
    setExpandedSubTasks((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id); else next.add(id)
      return next
    })
    setExpandedDetails((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id); else next.add(id)
      return next
    })
  }

  useEffect(() => {
    if (todoId) {
      fetchTodo(todoId)
      fetchChat(todoId)
      fetchDeliverables(todoId)
      fetchAgentRuns(todoId)
    }
  }, [todoId, fetchTodo, fetchChat, fetchDeliverables, fetchAgentRuns])

  const todo = todoId ? todos[todoId] : undefined
  const messages = todoId ? chatMessages[todoId] || [] : []
  const taskDeliverables = todoId ? deliverablesByTodo[todoId] || []  : []
  const agentRuns = todoId ? agentRunsByTodo[todoId] || [] : []
  const executionEvents = todoId ? allExecutionEvents[todoId] || [] : []

  // Previous-run detection: when a todo is retried, items from before the
  // retry are considered "previous run" and greyed out. We use retried_at
  // (set only on explicit retries) rather than state_changed_at (which
  // updates on every state transition and causes false positives).
  const todoActive = todo ? !['completed', 'failed', 'cancelled'].includes(todo.state) : false
  const retriedAt = todo?.retried_at ? new Date(todo.retried_at).getTime() : 0
  const hasPreviousRun = todoActive && retriedAt > 0

  const isSubTaskPreviousRun = (st: SubTask) =>
    hasPreviousRun &&
    new Date(st.created_at).getTime() < retriedAt &&
    (st.status === 'completed' || st.status === 'failed')

  const isTimestampPreviousRun = (createdAt: string) =>
    hasPreviousRun && new Date(createdAt).getTime() < retriedAt

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages.length])

  // Seed activity log from persisted progress_message on load/refresh
  useEffect(() => {
    if (!todo?.sub_tasks) return
    for (const st of todo.sub_tasks) {
      if (st.status === 'running' && st.progress_message && (!activityLogs[st.id] || activityLogs[st.id].length === 0)) {
        appendActivity(st.id, st.progress_message)
      }
    }
  }, [todo?.sub_tasks]) // eslint-disable-line react-hooks/exhaustive-deps

  // Auto-scroll activity logs when new entries arrive (debounced)
  const activityScrollTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const prevActivityCounts = useRef<Record<string, number>>({})
  useEffect(() => {
    if (activityScrollTimer.current) clearTimeout(activityScrollTimer.current)
    activityScrollTimer.current = setTimeout(() => {
      for (const [stId, entries] of Object.entries(activityLogs)) {
        const prevLen = prevActivityCounts.current[stId] || 0
        if (entries.length > prevLen && expandedActivity.has(stId)) {
          activityEndRefs.current[stId]?.scrollIntoView({ behavior: 'smooth', block: 'end' })
        }
        prevActivityCounts.current[stId] = entries.length
      }
    }, 300)
    return () => {
      if (activityScrollTimer.current) clearTimeout(activityScrollTimer.current)
    }
  }, [activityLogs, expandedActivity])

  const handleSendChat = async () => {
    if (!todoId || !chatInput.trim()) return
    await sendChat(todoId, chatInput.trim())
    setChatInput('')
  }

  if (!todo) {
    return (
      <div className="p-4 md:p-6 space-y-4 animate-fade-in">
        <div className="flex items-center gap-2.5 mb-2">
          <div className="skeleton h-5 w-20 rounded" />
          <div className="skeleton h-5 w-16 rounded" />
        </div>
        <div className="skeleton h-7 w-80 rounded" />
        <div className="skeleton h-4 w-full max-w-lg rounded" />
        <div className="skeleton h-4 w-48 rounded" />
        <div className="mt-6 space-y-2">
          {[1, 2, 3].map(i => (
            <div key={i} className="skeleton h-16 w-full rounded-lg" />
          ))}
        </div>
      </div>
    )
  }

  return (
    <div className="flex flex-col md:flex-row h-full">
      {/* Main content */}
      <div className="flex-1 p-4 md:p-6 overflow-y-auto">
        {/* Header */}
        <div className={`mb-6 -mx-4 md:-mx-6 -mt-4 md:-mt-6 px-4 md:px-6 pt-4 md:pt-6 pb-4 ${STATE_BANNER_BG[todo.state] || ''} animate-fade-in`}>
          <div className="flex items-center gap-2.5 mb-2">
            {STATE_ICON_MAP[todo.state]}
            <span className={`px-2 py-0.5 rounded text-[11px] font-medium text-white ${STATE_COLORS[todo.state]}`}>
              {todo.state.replace('_', ' ')}
            </span>
            {todo.sub_state && (
              <span className="px-2 py-0.5 bg-gray-900 border border-gray-800 rounded text-[11px] text-gray-400 font-mono">
                {todo.sub_state}
              </span>
            )}
            <span className="text-[11px] text-gray-600">{todo.task_type}</span>
          </div>
          <h1 className="text-xl font-semibold text-white leading-snug">{todo.title}</h1>
          {todo.description && (
            <p className="mt-2 text-gray-500 text-sm leading-relaxed">{todo.description}</p>
          )}
          <div className="mt-2 flex items-center gap-2 flex-wrap">
            {todo.provider_name && (
              <div className="inline-flex items-center gap-1.5 px-2.5 py-1 bg-gray-900 border border-gray-800 rounded text-[11px] text-gray-500">
                <span className="w-1.5 h-1.5 rounded-full bg-emerald-500" />
                <span>{todo.provider_name}</span>
                {todo.provider_model && (
                  <span className="text-gray-600 font-mono">· {todo.provider_model}</span>
                )}
              </div>
            )}
            {todo.actual_tokens > 0 && (
              <span className="text-[11px] text-gray-600 font-mono">{todo.actual_tokens.toLocaleString()} tok</span>
            )}
            {todo.cost_usd > 0 && (
              <span className="text-[11px] text-gray-600 font-mono">${todo.cost_usd.toFixed(4)}</span>
            )}
            {todo.retry_count > 0 && (
              <span className="text-[11px] text-gray-600 font-mono">{todo.retry_count} retries</span>
            )}
            {todo.completed_at && todo.created_at && (() => {
              const dur = new Date(todo.completed_at).getTime() - new Date(todo.created_at).getTime()
              const secs = Math.round(dur / 1000)
              const durStr = secs >= 3600
                ? `${Math.floor(secs / 3600)}h ${Math.floor((secs % 3600) / 60)}m`
                : secs >= 60 ? `${Math.floor(secs / 60)}m ${secs % 60}s` : `${secs}s`
              return <span className="text-[11px] text-gray-600 font-mono">{durStr}</span>
            })()}
            <button
              onClick={() => setShowWorkspace(!showWorkspace)}
              className={`ml-auto px-3 py-1.5 rounded-lg text-xs transition-colors flex items-center gap-1.5 ${
                showWorkspace
                  ? 'bg-indigo-600 hover:bg-indigo-500 text-white'
                  : 'bg-gray-800 hover:bg-gray-700 text-gray-400 hover:text-white'
              }`}
            >
              <Code2 className="w-3.5 h-3.5" />
              {showWorkspace ? 'Task Details' : 'Workspace'}
            </button>
          </div>
        </div>
        <div className="mb-2" />

        {/* Workspace Mode */}
        {showWorkspace ? (
          <div className="flex-1 -mx-4 md:-mx-6 -mb-4 md:-mb-6" style={{ height: 'calc(100vh - 180px)' }}>
            <WorkspaceView todoId={todoId!} />
          </div>
        ) : (
        <>
        {/* Error Banner */}
        {todo.error_message && (
          <div className="mb-5 px-4 py-3 bg-red-500/5 border border-red-500/10 rounded-lg">
            <div className="flex items-center gap-2 mb-1">
              <span className="w-1.5 h-1.5 rounded-full bg-red-400" />
              <span className="text-sm font-medium text-red-300/80">Task Failed</span>
            </div>
            <pre className="text-sm text-red-300/60 font-mono break-words whitespace-pre-wrap max-h-40 overflow-y-auto">{todo.error_message}</pre>
            {/* Show failed agent runs with detailed errors */}
            {(() => {
              const failedRuns = agentRuns.filter((r: AgentRun) => r.status === 'failed' && r.error_detail)
              if (failedRuns.length === 0) return null
              return (
                <details className="mt-3 border-t border-red-500/10 pt-2">
                  <summary className="text-[11px] text-red-400/70 cursor-pointer hover:text-red-400">
                    Agent run details ({failedRuns.length} failed)
                  </summary>
                  <div className="mt-2 space-y-2">
                    {failedRuns.map((run: AgentRun) => (
                      <div key={run.id} className="bg-red-950/30 rounded px-3 py-2">
                        <div className="flex items-center gap-2 text-[11px] mb-1">
                          <span className="text-red-400 font-medium">{run.agent_role}</span>
                          {run.error_type && run.error_type !== 'transient' && (
                            <span className="px-1.5 py-0.5 bg-red-500/10 rounded text-[10px] text-red-300">{run.error_type}</span>
                          )}
                          <span className="ml-auto text-gray-600 font-mono">{run.duration_ms ? `${(run.duration_ms / 1000).toFixed(1)}s` : ''}</span>
                        </div>
                        <pre className="text-[11px] text-red-300/50 font-mono whitespace-pre-wrap max-h-48 overflow-y-auto leading-relaxed">{run.error_detail}</pre>
                      </div>
                    ))}
                  </div>
                </details>
              )
            })()}
          </div>
        )}

        {/* Merge Approval Panel */}
        {todo.sub_state === 'awaiting_merge_approval' && (() => {
          const prDeliverable = taskDeliverables.find((d: Deliverable) => d.type === 'pull_request' && d.pr_url)
          return (
            <div className="mb-5 bg-gray-900 border border-indigo-500/20 rounded-lg overflow-hidden">
              <div className="px-4 py-3 border-b border-gray-800 flex items-center gap-2">
                <span className="w-2 h-2 rounded-full bg-indigo-400 animate-pulse" />
                <h2 className="text-sm font-medium text-white">Merge Approval Required</h2>
              </div>
              <div className="px-4 py-3">
                <p className="text-sm text-gray-400 mb-2">
                  CI has passed. This PR is ready to merge.
                </p>
                {prDeliverable && prDeliverable.pr_url && (
                  <a href={prDeliverable.pr_url} target="_blank" rel="noreferrer"
                    className="text-sm text-indigo-400 hover:underline">
                    PR #{prDeliverable.pr_number}: {prDeliverable.title}
                  </a>
                )}
              </div>
              <div className="px-4 py-3 border-t border-gray-800 flex items-center gap-2">
                <button
                  onClick={() => todoId && approveMerge(todoId)}
                  className="px-4 py-1.5 bg-emerald-600 hover:bg-emerald-500 rounded-lg text-sm font-medium text-white transition-colors"
                >
                  Approve & Merge
                </button>
                {showRejectMergeForm ? (
                  <div className="flex-1 flex items-center gap-2">
                    <input
                      className="flex-1 px-3 py-1.5 bg-gray-950 border border-gray-800 rounded-lg text-sm text-white placeholder-gray-600 focus:outline-none focus:border-indigo-500 transition-colors"
                      placeholder="Why reject this merge?"
                      value={rejectMergeFeedback}
                      onChange={(e) => setRejectMergeFeedback(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' && rejectMergeFeedback.trim() && todoId) {
                          rejectMerge(todoId, rejectMergeFeedback.trim())
                          setRejectMergeFeedback('')
                          setShowRejectMergeForm(false)
                        }
                      }}
                      autoFocus
                    />
                    <button
                      onClick={() => {
                        if (rejectMergeFeedback.trim() && todoId) {
                          rejectMerge(todoId, rejectMergeFeedback.trim())
                          setRejectMergeFeedback('')
                          setShowRejectMergeForm(false)
                        }
                      }}
                      disabled={!rejectMergeFeedback.trim()}
                      className="px-3 py-1.5 bg-gray-800 hover:bg-gray-700 disabled:opacity-40 rounded-lg text-sm text-gray-300 transition-colors"
                    >
                      Send
                    </button>
                    <button
                      onClick={() => setShowRejectMergeForm(false)}
                      className="text-xs text-gray-600 hover:text-gray-400 transition-colors"
                    >
                      Cancel
                    </button>
                  </div>
                ) : (
                  <button
                    onClick={() => setShowRejectMergeForm(true)}
                    className="px-4 py-1.5 bg-gray-800 hover:bg-gray-700 rounded-lg text-sm text-gray-400 transition-colors"
                  >
                    Reject Merge
                  </button>
                )}
              </div>
            </div>
          )
        })()}

        {/* External Merge Observer Panel */}
        {todo.sub_state === 'awaiting_external_merge' && (() => {
          const prDeliverable = taskDeliverables.find((d: Deliverable) => d.type === 'pull_request' && d.pr_url)
          return (
            <div className="mb-5 bg-gray-900 border border-cyan-500/20 rounded-lg overflow-hidden">
              <div className="px-4 py-3 border-b border-gray-800 flex items-center gap-2">
                <span className="w-2 h-2 rounded-full bg-cyan-400 animate-pulse" />
                <h2 className="text-sm font-medium text-white">Waiting for External Merge</h2>
              </div>
              <div className="px-4 py-3">
                <p className="text-sm text-gray-400 mb-2">
                  A PR has been created and is awaiting merge on your git provider. The system is watching for the merge — no action needed here.
                </p>
                {prDeliverable && prDeliverable.pr_url && (
                  <a href={prDeliverable.pr_url} target="_blank" rel="noreferrer"
                    className="text-sm text-indigo-400 hover:underline">
                    PR #{prDeliverable.pr_number}: {prDeliverable.title}
                  </a>
                )}
              </div>
            </div>
          )
        })()}

        {/* Release Approval Panel */}
        {todo.sub_state === 'awaiting_release_approval' && (
          <div className="mb-5 bg-gray-900 border border-amber-500/20 rounded-lg overflow-hidden">
            <div className="px-4 py-3 border-b border-gray-800 flex items-center gap-2">
              <span className="w-2 h-2 rounded-full bg-amber-400 animate-pulse" />
              <h2 className="text-sm font-medium text-white">Production Release Approval</h2>
            </div>
            <div className="px-4 py-3">
              <p className="text-sm text-gray-400">
                Staging deployment succeeded. Approve to deploy to production.
              </p>
            </div>
            <div className="px-4 py-3 border-t border-gray-800 flex items-center gap-2">
              <button
                onClick={() => todoId && todosApi.approveRelease(todoId).then(() => fetchTodo(todoId))}
                className="px-4 py-1.5 bg-emerald-600 hover:bg-emerald-500 rounded-lg text-sm font-medium text-white transition-colors"
              >
                Approve Production Release
              </button>
              {showRejectReleaseForm ? (
                <div className="flex-1 flex items-center gap-2">
                  <input
                    className="flex-1 px-3 py-1.5 bg-gray-950 border border-gray-800 rounded-lg text-sm text-white placeholder-gray-600 focus:outline-none focus:border-indigo-500 transition-colors"
                    placeholder="Why reject this release?"
                    value={rejectReleaseFeedback}
                    onChange={(e) => setRejectReleaseFeedback(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && rejectReleaseFeedback.trim() && todoId) {
                        todosApi.rejectRelease(todoId, rejectReleaseFeedback.trim()).then(() => fetchTodo(todoId))
                        setRejectReleaseFeedback('')
                        setShowRejectReleaseForm(false)
                      }
                    }}
                    autoFocus
                  />
                  <button
                    onClick={() => {
                      if (rejectReleaseFeedback.trim() && todoId) {
                        todosApi.rejectRelease(todoId, rejectReleaseFeedback.trim()).then(() => fetchTodo(todoId))
                        setRejectReleaseFeedback('')
                        setShowRejectReleaseForm(false)
                      }
                    }}
                    disabled={!rejectReleaseFeedback.trim()}
                    className="px-3 py-1.5 bg-gray-800 hover:bg-gray-700 disabled:opacity-40 rounded-lg text-sm text-gray-300 transition-colors"
                  >
                    Send
                  </button>
                  <button
                    onClick={() => setShowRejectReleaseForm(false)}
                    className="text-xs text-gray-600 hover:text-gray-400 transition-colors"
                  >
                    Cancel
                  </button>
                </div>
              ) : (
                <button
                  onClick={() => setShowRejectReleaseForm(true)}
                  className="px-4 py-1.5 bg-gray-800 hover:bg-gray-700 rounded-lg text-sm text-gray-400 transition-colors"
                >
                  Reject Release
                </button>
              )}
            </div>
          </div>
        )}

        {/* Plan Review Panel */}
        {todo.state === 'plan_ready' && todo.plan_json && (
          <div className="mb-5 bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
            <div className="px-4 py-3 border-b border-gray-800 flex items-center justify-between">
              <h2 className="text-sm font-medium text-white">Execution Plan</h2>
              {todo.plan_json.estimated_tokens && (
                <span className="text-[11px] text-gray-600 font-mono">
                  ~{todo.plan_json.estimated_tokens.toLocaleString()} tokens
                </span>
              )}
            </div>

            {todo.plan_json.summary && (
              <div className="px-4 py-3 border-b border-gray-800">
                <p className="text-sm text-gray-400 leading-relaxed">{todo.plan_json.summary}</p>
              </div>
            )}

            <div className="divide-y divide-gray-800">
              {(todo.plan_json.sub_tasks || []).map((st: PlanSubTask, i: number) => (
                <div key={i} className="px-4 py-3">
                  <div className="flex items-start gap-3">
                    <span className="text-[11px] text-gray-600 font-mono mt-0.5 w-5 text-right shrink-0">{i + 1}</span>
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 mb-0.5 flex-wrap">
                        <span className="text-sm text-gray-200">{st.title}</span>
                        <span className="text-[11px] px-1.5 py-0.5 bg-gray-800 rounded text-gray-500">
                          {ROLE_LABELS[st.agent_role] || st.agent_role}
                        </span>
                        {st.review_loop && (
                          <span className="px-1.5 py-0.5 bg-cyan-500/10 border border-cyan-500/20 rounded text-[10px] text-cyan-400/80">review loop</span>
                        )}
                        <span className={`px-1.5 py-0.5 rounded text-[10px] font-mono ${
                          st.target_repo && safeStr(st.target_repo) !== 'main'
                            ? 'bg-purple-500/10 border border-purple-500/20 text-purple-400/80'
                            : 'bg-gray-800 text-gray-500'
                        }`}>
                          {safeStr(st.target_repo) || 'main'}
                        </span>
                        {st.depends_on && st.depends_on.length > 0 && (
                          <span className="text-[11px] text-gray-700 font-mono">
                            depends on: {st.depends_on.map((d) => `#${d + 1}`).join(', ')}
                          </span>
                        )}
                      </div>
                      {st.description && (
                        <p className="text-[11px] text-gray-500 leading-relaxed mt-1">{safeStr(st.description)}</p>
                      )}
                      {/* Context details */}
                      {st.context && typeof st.context === 'object' && Object.keys(st.context).length > 0 && (
                        <div className="mt-2 space-y-1.5 pl-0.5">
                          {Array.isArray(st.context.relevant_files) && st.context.relevant_files.length > 0 && (
                            <div>
                              <span className="text-[10px] text-gray-600 uppercase tracking-wider">Files</span>
                              <div className="mt-0.5 flex flex-wrap gap-1">
                                {st.context.relevant_files.map((f, fi) => (
                                  <span key={fi} className="text-[11px] font-mono text-indigo-400/70 bg-indigo-500/5 px-1.5 py-0.5 rounded">
                                    {safeStr(f)}
                                  </span>
                                ))}
                              </div>
                            </div>
                          )}
                          {st.context.what_to_change && (
                            <div>
                              <span className="text-[10px] text-gray-600 uppercase tracking-wider">What to change</span>
                              <p className="text-[11px] text-gray-500 mt-0.5 leading-relaxed">{safeStr(st.context.what_to_change)}</p>
                            </div>
                          )}
                          {st.context.current_state && (
                            <div>
                              <span className="text-[10px] text-gray-600 uppercase tracking-wider">Current state</span>
                              <p className="text-[11px] text-gray-500 mt-0.5 leading-relaxed">{safeStr(st.context.current_state)}</p>
                            </div>
                          )}
                          {st.context.patterns_to_follow && (
                            <div>
                              <span className="text-[10px] text-gray-600 uppercase tracking-wider">Patterns to follow</span>
                              <p className="text-[11px] text-gray-500 mt-0.5 leading-relaxed">{safeStr(st.context.patterns_to_follow)}</p>
                            </div>
                          )}
                          {st.context.related_code && (
                            <div>
                              <span className="text-[10px] text-gray-600 uppercase tracking-wider">Related code</span>
                              <pre className="text-[11px] text-gray-500 mt-0.5 font-mono whitespace-pre-wrap leading-relaxed bg-gray-950 rounded px-2 py-1.5 border border-gray-800/50 max-h-32 overflow-y-auto">{safeStr(st.context.related_code)}</pre>
                            </div>
                          )}
                          {st.context.integration_points && (
                            <div>
                              <span className="text-[10px] text-gray-600 uppercase tracking-wider">Integration points</span>
                              <p className="text-[11px] text-gray-500 mt-0.5 leading-relaxed">{safeStr(st.context.integration_points)}</p>
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                    <span className="text-[11px] text-gray-700 font-mono shrink-0">
                      order {st.execution_order}
                    </span>
                  </div>
                </div>
              ))}
            </div>

            <div className="px-4 py-3 border-t border-gray-800 flex items-center gap-2">
              <button
                onClick={() => todoId && approvePlan(todoId)}
                className="px-4 py-1.5 bg-emerald-600 hover:bg-emerald-500 rounded-lg text-sm font-medium text-white transition-colors"
              >
                Approve Plan
              </button>
              {showRejectForm ? (
                <div className="flex-1 flex items-center gap-2">
                  <input
                    className="flex-1 px-3 py-1.5 bg-gray-950 border border-gray-800 rounded-lg text-sm text-white placeholder-gray-600 focus:outline-none focus:border-indigo-500 transition-colors"
                    placeholder="What should change?"
                    value={rejectFeedback}
                    onChange={(e) => setRejectFeedback(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && rejectFeedback.trim() && todoId) {
                        rejectPlan(todoId, rejectFeedback.trim())
                        setRejectFeedback('')
                        setShowRejectForm(false)
                      }
                    }}
                    autoFocus
                  />
                  <button
                    onClick={() => {
                      if (rejectFeedback.trim() && todoId) {
                        rejectPlan(todoId, rejectFeedback.trim())
                        setRejectFeedback('')
                        setShowRejectForm(false)
                      }
                    }}
                    disabled={!rejectFeedback.trim()}
                    className="px-3 py-1.5 bg-gray-800 hover:bg-gray-700 disabled:opacity-40 rounded-lg text-sm text-gray-300 transition-colors"
                  >
                    Send
                  </button>
                  <button
                    onClick={() => setShowRejectForm(false)}
                    className="text-xs text-gray-600 hover:text-gray-400 transition-colors"
                  >
                    Cancel
                  </button>
                </div>
              ) : (
                <button
                  onClick={() => setShowRejectForm(true)}
                  className="px-4 py-1.5 bg-gray-800 hover:bg-gray-700 rounded-lg text-sm text-gray-400 transition-colors"
                >
                  Reject & Re-plan
                </button>
              )}
            </div>
          </div>
        )}

        {/* Workspace Edited — Resume Banner */}
        {todo.sub_state === 'workspace_edited' && (
          <div className="mb-5 bg-gray-900 border border-amber-500/20 rounded-lg overflow-hidden">
            <div className="px-4 py-3 flex items-center gap-3">
              <span className="w-2 h-2 rounded-full bg-amber-400" />
              <div className="flex-1 min-w-0">
                <h2 className="text-sm font-medium text-white">Task paused — workspace edited</h2>
                <p className="text-xs text-gray-500 mt-0.5">
                  You committed changes in the workspace. Running sub-tasks were cancelled. Resume to let the agent re-plan and continue.
                </p>
              </div>
              <button
                onClick={() => todoId && resumeTodo(todoId)}
                className="px-4 py-1.5 bg-amber-600 hover:bg-amber-500 rounded-lg text-sm font-medium text-white transition-colors shrink-0"
              >
                Resume
              </button>
            </div>
          </div>
        )}

        {/* Actions */}
        <div className="flex gap-2 mb-6 flex-wrap">
          {todo.state === 'review' && (
            <>
              <button
                onClick={() => todoId && acceptDeliverables(todoId)}
                className="px-3 py-1.5 bg-emerald-600 hover:bg-emerald-500 rounded-lg text-sm text-white transition-colors"
              >
                Accept & Complete
              </button>
              <div className="flex flex-col gap-2 md:flex-row md:gap-1">
                <input
                  className="px-3 py-1.5 bg-gray-900 border border-gray-800 rounded-lg text-sm text-white placeholder-gray-600 focus:outline-none focus:border-indigo-500 transition-colors"
                  placeholder="Feedback..."
                  value={changesFeedback}
                  onChange={(e) => setChangesFeedback(e.target.value)}
                />
                <button
                  onClick={() => {
                    if (todoId && changesFeedback.trim()) {
                      requestChanges(todoId, changesFeedback.trim())
                      setChangesFeedback('')
                    }
                  }}
                  className="px-3 py-1.5 bg-orange-600 hover:bg-orange-500 rounded-lg text-sm text-white transition-colors"
                >
                  Request Changes
                </button>
              </div>
            </>
          )}
          {['intake', 'planning', 'plan_ready', 'in_progress', 'testing', 'review'].includes(todo.state) && (
            <button
              onClick={() => todoId && cancelTodo(todoId)}
              className="px-3 py-1.5 bg-gray-800 hover:bg-gray-700 rounded-lg text-sm text-gray-400 transition-colors"
            >
              Cancel
            </button>
          )}
          {['failed', 'cancelled', 'completed'].includes(todo.state) && (
            <div className="flex items-center gap-2">
              <button
                onClick={() => todoId && retryTodo(todoId)}
                className="px-3 py-1.5 bg-indigo-600 hover:bg-indigo-500 rounded-lg text-sm text-white transition-colors"
              >
                Retry
              </button>
              <button
                onClick={() => todoId && retryTodo(todoId, true)}
                className="px-3 py-1.5 bg-gray-800 hover:bg-gray-700 rounded-lg text-sm text-gray-300 transition-colors border border-gray-700"
              >
                Retry with Context
              </button>
            </div>
          )}
        </div>

        {/* RALPH Config Bar */}
        {(todo.max_iterations || todo.rules_override_json) && (
          <div className="mb-4 flex items-center gap-3 text-[11px] text-gray-600 font-mono">
            {todo.max_iterations && (
              <span className="px-2 py-0.5 bg-gray-900 border border-gray-800 rounded">
                max iterations: {todo.max_iterations}
              </span>
            )}
            {todo.rules_override_json && Object.keys(todo.rules_override_json).length > 0 && (
              <span className="px-2 py-0.5 bg-gray-900 border border-gray-800 rounded">
                rule overrides: {Object.keys(todo.rules_override_json).join(', ')}
              </span>
            )}
          </div>
        )}

        {/* Review Chain Visualization */}
        {todo.sub_tasks && todo.sub_tasks.some((st: SubTask) => st.review_chain_id) && (() => {
          // Group sub-tasks by review_chain_id
          const chains = new Map<string, SubTask[]>()
          for (const st of todo.sub_tasks!) {
            if (st.review_chain_id) {
              const chain = chains.get(st.review_chain_id) || []
              chain.push(st)
              chains.set(st.review_chain_id, chain)
            }
          }
          if (chains.size === 0) return null

          // Separate current vs previous-run chains
          const currentChains: [string, SubTask[]][] = []
          const previousChains: [string, SubTask[]][] = []
          for (const entry of chains.entries()) {
            const allPrevious = entry[1].every((st) => isSubTaskPreviousRun(st))
            if (allPrevious) previousChains.push(entry)
            else currentChains.push(entry)
          }

          const renderChain = ([chainId, chainTasks]: [string, SubTask[]]) => (
            <div key={chainId} className="p-3 bg-gray-900 rounded-lg border border-gray-800/50">
              <div className="flex items-center gap-1.5 flex-wrap">
                {chainTasks
                  .sort((a, b) => (a.execution_order || 0) - (b.execution_order || 0))
                  .map((st, i) => (
                    <div key={st.id} className="flex items-center gap-1.5">
                      {i > 0 && <span className="text-gray-700 text-xs">{'\u2192'}</span>}
                      <span className={`px-2 py-0.5 rounded text-[11px] font-medium flex items-center gap-1 ${
                        st.status === 'completed' ? 'bg-emerald-500/10 text-emerald-400/80 border border-emerald-500/20' :
                        st.status === 'running' ? 'bg-amber-500/10 text-amber-400/80 border border-amber-500/20' :
                        st.status === 'failed' ? 'bg-red-500/10 text-red-400/80 border border-red-500/20' :
                        'bg-gray-800 text-gray-400 border border-gray-700'
                      }`}>
                        <span>{ROLE_LABELS[st.agent_role] || st.agent_role}</span>
                        {st.review_verdict === 'approved' && <span className="text-emerald-400">{'\u2713'}</span>}
                        {st.review_verdict === 'needs_changes' && <span className="text-amber-400">{'\u2717'}</span>}
                      </span>
                    </div>
                  ))}
              </div>
            </div>
          )

          return (
            <CollapsibleSection title="Review Chains" count={currentChains.length} defaultOpen={false}>
              <div className="space-y-3">
                {currentChains.map(renderChain)}
              </div>
              {previousChains.length > 0 && (
                <details className="mt-3">
                  <summary className="text-[10px] text-gray-600 uppercase tracking-wider cursor-pointer hover:text-gray-500">
                    Previous Run ({previousChains.length})
                  </summary>
                  <div className="mt-1.5 space-y-3 opacity-40">
                    {previousChains.map(renderChain)}
                  </div>
                </details>
              )}
            </CollapsibleSection>
          )
        })()}

        {/* Sub-tasks with RALPH iteration details */}
        {todo.sub_tasks && todo.sub_tasks.length > 0 && (() => {
          const currentTasks = todo.sub_tasks.filter((st: SubTask) => !isSubTaskPreviousRun(st))
          const previousTasks = todo.sub_tasks.filter((st: SubTask) => isSubTaskPreviousRun(st))
          // Build UUID → 1-based index map for dependency display
          const stIdToIndex = new Map<string, number>()
          currentTasks.forEach((st: SubTask, idx: number) => stIdToIndex.set(st.id, idx + 1))

          return (
          <CollapsibleSection title="Sub-tasks" count={currentTasks.length}>
            <div className="space-y-1.5">
              {currentTasks.map((st: SubTask) => {
                const iterLog = st.iteration_log || []
                const hasIterations = iterLog.length > 0
                const isExpanded = expandedSubTasks.has(st.id)
                const passedCount = iterLog.filter((e) => e.outcome === 'passed').length
                const failedCount = iterLog.filter((e) => e.outcome !== 'passed').length
                const lastStuck = [...iterLog].reverse().find((e) => e.stuck_check?.stuck)

                return (
                  <div key={st.id} className="bg-gray-900 rounded-lg border border-gray-800/50 overflow-hidden animate-fade-in">
                    {/* Sub-task header */}
                    <div
                      className="p-3 cursor-pointer hover:bg-gray-800/30"
                      onClick={() => toggleSubTask(st.id)}
                    >
                      {/* Row 1: identity */}
                      <div className="flex items-center gap-2">
                        <span className="text-gray-600 text-[11px] w-3 shrink-0">{isExpanded ? '\u25BC' : '\u25B6'}</span>
                        <span className={`px-1.5 py-0.5 rounded text-[11px] font-medium text-white ${SUBTASK_COLORS[st.status]}`}>
                          {st.status}
                        </span>
                        <span className="text-[11px] text-indigo-400/70">{ROLE_LABELS[st.agent_role] || st.agent_role}</span>
                        <span className="text-sm text-gray-300 truncate">{st.title}</span>
                        <div className="ml-auto shrink-0 flex items-center gap-1.5">
                          {(() => {
                            if (st.started_at && st.completed_at) {
                              const dur = new Date(st.completed_at).getTime() - new Date(st.started_at).getTime()
                              const secs = Math.round(dur / 1000)
                              const durStr = secs >= 60 ? `${Math.floor(secs / 60)}m ${secs % 60}s` : `${secs}s`
                              return <span className="text-[11px] text-gray-600 font-mono">{durStr}</span>
                            }
                            if (st.started_at && st.status === 'running') {
                              const startTime = new Date(st.started_at).toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' })
                              return <span className="text-[11px] text-gray-600 font-mono">started {startTime}</span>
                            }
                            return null
                          })()}
                          {(st.status === 'pending' || st.status === 'failed') && todoId && (
                            <button
                              onClick={(e) => { e.stopPropagation(); triggerSubTask(todoId!, st.id, true) }}
                              className="px-2 py-0.5 bg-gray-800 hover:bg-gray-700 rounded text-[10px] text-gray-400 transition-colors"
                              title="Force run — skip dependency checks"
                            >
                              Force Run
                            </button>
                          )}
                          {(st.output_result || st.description || st.error_message || (st.input_context && Object.keys(st.input_context).length > 0)) && (
                            <button
                              onClick={(e) => {
                                e.stopPropagation()
                                setExpandedDetails((prev) => {
                                  const next = new Set(prev)
                                  if (next.has(st.id)) next.delete(st.id); else next.add(st.id)
                                  return next
                                })
                              }}
                              className="px-2 py-0.5 bg-gray-800 hover:bg-gray-700 rounded text-[10px] text-gray-400 transition-colors"
                            >
                              {expandedDetails.has(st.id) ? 'Hide' : 'Details'}
                            </button>
                          )}
                        </div>
                      </div>
                      {/* Row 2: metadata tags */}
                      {(st.target_repo || (st.depends_on && st.depends_on.length > 0) || st.review_loop || st.review_verdict || hasIterations) && (
                        <div className={`flex items-center gap-1.5 mt-1 flex-wrap ${hasIterations ? 'ml-5' : ''}`}>
                          <span className={`px-1.5 py-0.5 rounded text-[10px] font-mono ${
                            st.target_repo
                              ? 'bg-purple-500/10 border border-purple-500/20 text-purple-400/80'
                              : 'bg-gray-800 text-gray-500'
                          }`}>
                            {st.target_repo ? st.target_repo.name : 'main'}
                          </span>
                          {st.depends_on && st.depends_on.length > 0 && (
                            <span className={`text-[10px] font-mono ${
                              st.status === 'pending' ? 'text-amber-400/70' : 'text-gray-600'
                            }`}>
                              {st.status === 'pending' ? 'blocked by' : 'after'}{' '}
                              {st.depends_on.map((depId) => `#${stIdToIndex.get(depId) ?? '?'}`).join(', ')}
                            </span>
                          )}
                          {st.review_loop && (
                            <span className="px-1.5 py-0.5 bg-cyan-500/10 border border-cyan-500/20 rounded text-[10px] text-cyan-400/80">review</span>
                          )}
                          {st.review_verdict && (
                            <span className={`px-1.5 py-0.5 border rounded text-[10px] ${VERDICT_COLORS[st.review_verdict] || 'bg-gray-800 text-gray-400'}`}>
                              {st.review_verdict === 'approved' ? 'approved' : 'changes requested'}
                            </span>
                          )}
                          {hasIterations && (
                            <span className="text-[11px] text-gray-600 font-mono">
                              {iterLog.length} iter{iterLog.length !== 1 ? 's' : ''}
                              {failedCount > 0 && <span className="text-red-400/60 ml-1">{failedCount} fail</span>}
                              {passedCount > 0 && <span className="text-emerald-400/60 ml-1">{passedCount} pass</span>}
                            </span>
                          )}
                        </div>
                      )}
                      {/* Inline output summary for completed/failed subtasks */}
                      {(st.status === 'completed' || st.status === 'failed') && st.output_result && (() => {
                        const out = st.output_result
                        const summary = (out.summary || out.approach || out.root_cause || out.test_summary || out.executive_summary || out.merge_decision) as string | undefined
                        if (!summary) return null
                        return (
                          <p className="mt-1 ml-0 text-[11px] text-gray-500 leading-relaxed line-clamp-2">{summary}</p>
                        )
                      })()}
                      {st.status === 'running' && (
                        <div className="mt-2">
                          <div className="w-full bg-gray-800 rounded-full h-1">
                            <div
                              className="bg-indigo-500 h-1 rounded-full transition-all"
                              style={{ width: `${st.progress_pct}%` }}
                            />
                          </div>
                          {st.progress_message && (
                            <div className="text-[11px] text-gray-600 mt-1">{st.progress_message}</div>
                          )}
                          {/* Activity log */}
                          {(() => {
                            const logs = activityLogs[st.id]
                            if (!logs || logs.length === 0) return null
                            const isOpen = expandedActivity.has(st.id)
                            const latest = logs[logs.length - 1]

                            // Parse [iter N] prefix and color-code activity entries
                            const renderEntry = (entry: string) => {
                              const iterMatch = entry.match(/^\[iter (\d+)\]\s*(.*)$/)
                              const iterTag = iterMatch ? iterMatch[1] : null
                              const body = iterMatch ? iterMatch[2] : entry
                              // Determine color based on content
                              let textColor = 'text-gray-500'
                              if (/^(Reading|Writing|Editing|Listing|Searching|Running:)/.test(body) || /^\[(Architect|Editor)\]/.test(body)) {
                                textColor = 'text-cyan-600'
                              } else if (/^Thinking/.test(body)) {
                                textColor = 'text-indigo-500/70'
                              } else if (/^Quality check passed/.test(body)) {
                                textColor = 'text-emerald-600'
                              } else if (/^Quality check failed/.test(body) || /^Stuck detected/.test(body)) {
                                textColor = 'text-red-500/70'
                              } else if (/^Agent will:/.test(body)) {
                                textColor = 'text-gray-400'
                              } else if (/^Done \(/.test(body)) {
                                textColor = 'text-gray-400'
                              }
                              return (
                                <>
                                  {iterTag && <span className="text-gray-700 mr-1 select-none">[{iterTag}]</span>}
                                  <span className={textColor}>{body}</span>
                                </>
                              )
                            }

                            return (
                              <div className="mt-2">
                                <button
                                  type="button"
                                  className="flex items-center gap-1.5 text-[11px] text-gray-500 hover:text-gray-400 transition-colors"
                                  onClick={(e) => {
                                    e.stopPropagation()
                                    setExpandedActivity((prev) => {
                                      const next = new Set(prev)
                                      if (next.has(st.id)) next.delete(st.id); else next.add(st.id)
                                      return next
                                    })
                                  }}
                                >
                                  <span className="w-3">{isOpen ? '\u25BC' : '\u25B6'}</span>
                                  <span className="font-mono truncate max-w-md">{renderEntry(latest)}</span>
                                  <span className="text-gray-700 shrink-0">({logs.length})</span>
                                </button>
                                {isOpen && (
                                  <div className="mt-1.5 max-h-40 overflow-y-auto bg-gray-950 border border-gray-800/50 rounded px-2.5 py-1.5 space-y-0.5">
                                    {logs.map((entry, i) => (
                                      <div key={i} className="text-[11px] font-mono leading-relaxed">
                                        <span className="text-gray-700 mr-1.5 select-none">{'\u203A'}</span>{renderEntry(entry)}
                                      </div>
                                    ))}
                                    <div ref={(el) => { activityEndRefs.current[st.id] = el }} />
                                  </div>
                                )}
                              </div>
                            )
                          })()}
                          {/* Inject input for running subtasks */}
                          <SubtaskInjectInput todoId={todoId!} subtaskId={st.id} />
                        </div>
                      )}
                      {st.error_message && (
                        <div className="mt-2 px-2.5 py-2 bg-red-500/5 border border-red-500/10 rounded">
                          <div className="flex items-center gap-1.5 mb-0.5">
                            <span className="w-1.5 h-1.5 rounded-full bg-red-400/70 shrink-0" />
                            <span className="text-[11px] text-red-400/70 font-medium">Error</span>
                          </div>
                          <pre className="text-[11px] text-red-300/50 font-mono whitespace-pre-wrap max-h-24 overflow-y-auto leading-relaxed">{st.error_message}</pre>
                        </div>
                      )}
                      {lastStuck && (
                        <div className="mt-1.5 px-2 py-1 bg-amber-500/5 border border-amber-500/10 rounded text-[11px] text-amber-300/70">
                          Stuck detected: {lastStuck.stuck_check?.pattern}
                        </div>
                      )}
                    </div>

                    {/* Collapsible details panel */}
                    {expandedDetails.has(st.id) && (
                      <div className="border-t border-gray-800 px-3 py-2.5 bg-gray-950/30 space-y-2">
                        {/* Task description / instructions */}
                        {st.description && (
                          <details>
                            <summary className="text-[10px] text-gray-600 uppercase tracking-wider cursor-pointer hover:text-gray-500">
                              Task Instructions
                            </summary>
                            <pre className="mt-1.5 text-[11px] text-gray-500 font-mono whitespace-pre-wrap max-h-32 overflow-y-auto leading-relaxed bg-gray-950 rounded px-2.5 py-2 border border-gray-800/50">
                              {st.description}
                            </pre>
                          </details>
                        )}

                        {/* Input context from planner */}
                        {st.input_context && Object.keys(st.input_context).length > 0 && (
                          <details>
                            <summary className="text-[10px] text-gray-600 uppercase tracking-wider cursor-pointer hover:text-gray-500">
                              Agent Context
                            </summary>
                            <div className="mt-1.5 space-y-1.5">
                              {st.input_context.relevant_files && st.input_context.relevant_files.length > 0 && (
                                <div>
                                  <span className="text-[10px] text-gray-600">Files:</span>
                                  <div className="mt-0.5 flex flex-wrap gap-1">
                                    {st.input_context.relevant_files.map((f: string, fi: number) => (
                                      <span key={fi} className="text-[11px] font-mono text-indigo-400/70 bg-indigo-500/5 px-1.5 py-0.5 rounded">{f}</span>
                                    ))}
                                  </div>
                                </div>
                              )}
                              {st.input_context.what_to_change && (
                                <div>
                                  <span className="text-[10px] text-gray-600">What to change:</span>
                                  <p className="text-[11px] text-gray-500 mt-0.5 leading-relaxed">{st.input_context.what_to_change}</p>
                                </div>
                              )}
                              {st.input_context.current_state && (
                                <div>
                                  <span className="text-[10px] text-gray-600">Current state:</span>
                                  <p className="text-[11px] text-gray-500 mt-0.5 leading-relaxed">{st.input_context.current_state}</p>
                                </div>
                              )}
                              {st.input_context.patterns_to_follow && (
                                <div>
                                  <span className="text-[10px] text-gray-600">Patterns to follow:</span>
                                  <p className="text-[11px] text-gray-500 mt-0.5 leading-relaxed">{st.input_context.patterns_to_follow}</p>
                                </div>
                              )}
                              {st.input_context.related_code && (
                                <div>
                                  <span className="text-[10px] text-gray-600">Related code:</span>
                                  <pre className="text-[11px] text-gray-500 mt-0.5 font-mono whitespace-pre-wrap leading-relaxed bg-gray-950 rounded px-2 py-1.5 border border-gray-800/50 max-h-32 overflow-y-auto">{st.input_context.related_code}</pre>
                                </div>
                              )}
                              {st.input_context.integration_points && (
                                <div>
                                  <span className="text-[10px] text-gray-600">Integration points:</span>
                                  <p className="text-[11px] text-gray-500 mt-0.5 leading-relaxed">{st.input_context.integration_points}</p>
                                </div>
                              )}
                            </div>
                          </details>
                        )}

                        {/* Role-specific output */}
                        {st.output_result && (() => {
                          const out = st.output_result!
                          const role = st.agent_role
                          const items: Array<{ label: string; value: string | string[] | boolean | undefined | null }> = []

                          if (role === 'coder') {
                            if (out.approach) items.push({ label: 'Approach', value: out.approach as string })
                            if (out.files_changed && (out.files_changed as string[]).length > 0) items.push({ label: 'Files Changed', value: out.files_changed as string[] })
                            if (out.setup_steps && (out.setup_steps as string[]).length > 0) items.push({ label: 'Setup Steps', value: out.setup_steps as string[] })
                          } else if (role === 'tester') {
                            if (out.test_summary) items.push({ label: 'Test Summary', value: out.test_summary as string })
                            if (out.test_files && (out.test_files as string[]).length > 0) items.push({ label: 'Test Files', value: out.test_files as string[] })
                            items.push({ label: 'Bug Reproduced Before Fix', value: (out.bug_reproduced_before_fix as boolean) ? 'Yes' : 'No' })
                            items.push({ label: 'Bug Resolved After Fix', value: (out.bug_resolved_after_fix as boolean) ? 'Yes' : 'No' })
                          } else if (role === 'debugger') {
                            if (out.root_cause) items.push({ label: 'Root Cause', value: out.root_cause as string })
                            if (out.evidence && (out.evidence as string[]).length > 0) items.push({ label: 'Evidence', value: out.evidence as string[] })
                            if (out.recommendation) items.push({ label: 'Recommendation', value: out.recommendation as string })
                            if (out.files_changed && (out.files_changed as string[]).length > 0) items.push({ label: 'Files Changed', value: out.files_changed as string[] })
                          } else if (role === 'pr_creator') {
                            if (out.pr_title) items.push({ label: 'PR Title', value: out.pr_title as string })
                            if (out.pr_body) items.push({ label: 'PR Body', value: out.pr_body as string })
                            if (out.breaking_changes && (out.breaking_changes as string[]).length > 0) items.push({ label: 'Breaking Changes', value: out.breaking_changes as string[] })
                          } else if (role === 'report_writer') {
                            if (out.executive_summary) items.push({ label: 'Summary', value: out.executive_summary as string })
                          } else if (role === 'merge_agent') {
                            if (out.merge_decision) items.push({ label: 'Decision', value: out.merge_decision as string })
                            if (out.reason) items.push({ label: 'Reason', value: out.reason as string })
                            items.push({ label: 'CI Passed', value: (out.ci_passed as boolean) ? 'Yes' : 'No' })
                          } else if (role === 'merge_observer') {
                            if (out.pr_merged != null) items.push({ label: 'PR Merged', value: (out.pr_merged as boolean) ? 'Yes' : 'No' })
                            if (out.merge_commit_sha) items.push({ label: 'Merge SHA', value: out.merge_commit_sha as string })
                          } else if (role === 'reviewer') {
                            if (out.summary) items.push({ label: 'Summary', value: out.summary as string })
                          } else {
                            // Unknown role — show any string/array fields
                            for (const [k, v] of Object.entries(out)) {
                              if (k === 'raw_content' || k === 'content') continue
                              if (typeof v === 'string' && v) items.push({ label: k, value: v })
                              if (Array.isArray(v) && v.length > 0) items.push({ label: k, value: v as string[] })
                            }
                          }

                          // Reviewer issues
                          type ReviewIssue = { severity: string; file?: string; line?: number | null; description: string; suggestion?: string }
                          const reviewerIssues = role === 'reviewer' && out.issues ? out.issues as ReviewIssue[] : null

                          if (items.length === 0 && (!reviewerIssues || reviewerIssues.length === 0)) return null

                          const severityClass = (sev: string) =>
                            sev === 'critical' ? 'bg-red-500/10 text-red-400' :
                            sev === 'major' ? 'bg-amber-500/10 text-amber-400' :
                            sev === 'minor' ? 'bg-gray-800 text-gray-400' :
                            'bg-gray-800 text-gray-600'

                          return (
                            <div className="space-y-1.5">
                              <div className="text-[10px] text-gray-600 uppercase tracking-wider">Output</div>
                              {items.map((item, idx) => (
                                <div key={idx}>
                                  <span className="text-[10px] text-gray-600">{item.label}:</span>
                                  {Array.isArray(item.value) ? (
                                    <div className="mt-0.5 pl-2 space-y-0.5">
                                      {(item.value as string[]).map((v, vi) => (
                                        <div key={vi} className="text-[11px] text-gray-400 font-mono flex items-start gap-1.5">
                                          <span className="w-1 h-1 rounded-full bg-gray-700 mt-1.5 shrink-0" />
                                          {String(v)}
                                        </div>
                                      ))}
                                    </div>
                                  ) : (
                                    <pre className="mt-0.5 text-[11px] text-gray-400 whitespace-pre-wrap leading-relaxed max-h-24 overflow-y-auto">
                                      {String(item.value)}
                                    </pre>
                                  )}
                                </div>
                              ))}
                              {reviewerIssues && reviewerIssues.length > 0 && (() => {
                                const grouped = new Map<string, ReviewIssue[]>()
                                for (const issue of reviewerIssues) {
                                  const key = issue.file || '_general'
                                  if (!grouped.has(key)) grouped.set(key, [])
                                  grouped.get(key)!.push(issue)
                                }
                                const renderIssue = (issue: ReviewIssue, idx: number) => (
                                  <div key={idx} className="flex items-start gap-2">
                                    <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded shrink-0 ${severityClass(issue.severity)}`}>
                                      {issue.severity}
                                    </span>
                                    <div className="flex-1 min-w-0">
                                      <div className="text-[11px] text-gray-300">
                                        {issue.line != null && (
                                          <span className="text-[10px] text-gray-600 font-mono mr-1.5">L{issue.line}</span>
                                        )}
                                        {issue.description}
                                      </div>
                                      {issue.suggestion && (
                                        <p className="text-[11px] text-gray-600 mt-0.5">{'\u2192'} {issue.suggestion}</p>
                                      )}
                                    </div>
                                  </div>
                                )
                                if (grouped.size <= 1 && (grouped.has('_general') || grouped.size === 0)) {
                                  return <div className="space-y-1.5 mt-2">{reviewerIssues.map(renderIssue)}</div>
                                }
                                return (
                                  <div className="space-y-2.5 mt-2">
                                    {Array.from(grouped.entries()).map(([fileKey, fileIssues]) => (
                                      <div key={fileKey}>
                                        {fileKey !== '_general' ? (
                                          <div className="text-[11px] text-gray-500 font-mono flex items-center gap-1.5 mb-1">
                                            <span className="w-1 h-1 rounded-full bg-gray-700 shrink-0" />
                                            {fileKey}
                                          </div>
                                        ) : (
                                          <div className="text-[11px] text-gray-600 uppercase tracking-wider mb-1">General</div>
                                        )}
                                        <div className="space-y-1.5 pl-2.5">
                                          {fileIssues.map(renderIssue)}
                                        </div>
                                      </div>
                                    ))}
                                  </div>
                                )
                              })()}
                              {out.needs_human_review && (
                                <div className="mt-2 text-[10px] text-amber-400/70 flex items-center gap-1">
                                  <span>{'\u26A0'}</span> Needs human review
                                </div>
                              )}
                            </div>
                          )
                        })()}

                        {/* Raw LLM content (truncated, collapsible) */}
                        {st.output_result?.raw_content && (
                          <details>
                            <summary className="text-[10px] text-gray-600 uppercase tracking-wider cursor-pointer hover:text-gray-500">
                              Raw LLM Output ({(st.output_result.raw_content as string).length.toLocaleString()} chars)
                            </summary>
                            <pre className="mt-1.5 text-[11px] text-gray-500 font-mono whitespace-pre-wrap max-h-48 overflow-y-auto leading-relaxed bg-gray-950 rounded px-2.5 py-2 border border-gray-800/50">
                              {(st.output_result.raw_content as string).slice(0, 5000)}
                              {(st.output_result.raw_content as string).length > 5000 && '\n\n... truncated ...'}
                            </pre>
                          </details>
                        )}

                        {/* Iterations (full log) */}
                        {iterLog.length > 0 && (
                          <details>
                            <summary className="text-[10px] text-gray-600 uppercase tracking-wider cursor-pointer hover:text-gray-500">
                              Iterations ({iterLog.length}) — {iterLog.reduce((s, e) => s + e.tokens_used, 0).toLocaleString()} tokens
                            </summary>
                            <div className="mt-1.5 max-h-64 overflow-y-auto divide-y divide-gray-800/50">
                              {iterLog.map((entry: IterationLogEntry, i: number) => (
                                <div key={i} className="py-1.5 text-[11px]">
                                  <div className="flex items-center gap-2">
                                    <span className="text-gray-600 font-mono w-5 shrink-0 text-right">#{entry.iteration}</span>
                                    <span className={`px-1 py-0.5 rounded text-[10px] font-medium shrink-0 ${
                                      entry.outcome === 'passed' ? 'bg-emerald-500/10 text-emerald-400/70' : 'bg-red-500/10 text-red-400/70'
                                    }`}>{entry.outcome}</span>
                                    <span className="text-gray-500 truncate flex-1">{entry.action}</span>
                                    <span className="text-gray-700 font-mono shrink-0">{entry.tokens_used.toLocaleString()} tok</span>
                                  </div>
                                  {entry.learnings.length > 0 && (
                                    <div className="mt-1 pl-7 space-y-0.5">
                                      {entry.learnings.map((l, li) => (
                                        <div key={li} className="text-gray-500 flex items-start gap-1.5">
                                          <span className="w-1 h-1 rounded-full bg-gray-700 mt-1.5 shrink-0" />
                                          {l}
                                        </div>
                                      ))}
                                    </div>
                                  )}
                                  {entry.llm_response && (
                                    <details className="mt-1 pl-7">
                                      <summary className="text-indigo-400/60 cursor-pointer hover:text-indigo-400/80 text-[10px]">
                                        LLM Response ({entry.llm_response.length} chars)
                                      </summary>
                                      <pre className="mt-1 text-gray-500 font-mono whitespace-pre-wrap max-h-40 overflow-y-auto leading-relaxed">
                                        {entry.llm_response}
                                      </pre>
                                    </details>
                                  )}
                                  {entry.error_output && (
                                    <pre className="mt-1 pl-7 text-red-400/50 font-mono whitespace-pre-wrap max-h-20 overflow-y-auto">
                                      {entry.error_output}
                                    </pre>
                                  )}
                                  {entry.stuck_check?.stuck && (
                                    <div className="mt-1 pl-7 px-2 py-1 bg-amber-500/5 border border-amber-500/10 rounded">
                                      <span className="text-amber-300/70">Stuck: {entry.stuck_check.pattern}</span>
                                      {entry.stuck_check.advice && (
                                        <div className="text-amber-200/50 mt-0.5">Advice: {entry.stuck_check.advice}</div>
                                      )}
                                    </div>
                                  )}
                                </div>
                              ))}
                            </div>
                          </details>
                        )}

                        {/* Error message if present */}
                        {st.error_message && (
                          <div>
                            <div className="text-[10px] text-red-400/70 uppercase tracking-wider mb-1">Error</div>
                            <pre className="text-[11px] text-red-300/50 font-mono whitespace-pre-wrap max-h-24 overflow-y-auto leading-relaxed bg-red-500/5 rounded px-2.5 py-2 border border-red-500/10">
                              {st.error_message}
                            </pre>
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>

            {/* Previous run sub-tasks */}
            {previousTasks.length > 0 && (
              <details className="mt-4">
                <summary className="text-[10px] text-gray-600 uppercase tracking-wider cursor-pointer hover:text-gray-500">
                  Previous Run ({previousTasks.length} sub-tasks)
                </summary>
                <div className="mt-1.5 space-y-1.5 opacity-50">
                  {previousTasks.map((st: SubTask) => {
                    const hasOutputDetails = st.output_result && Object.keys(st.output_result).some(k => k !== 'raw_content' && k !== 'content')
                    const hasInputCtx = st.input_context && Object.keys(st.input_context).length > 0
                    const hasDetails = hasOutputDetails || st.description || st.error_message || hasInputCtx
                    const isOpen = expandedDetails.has(st.id)

                    return (
                      <div key={st.id} className="bg-gray-900/50 rounded-lg border border-gray-800/30 overflow-hidden">
                        <div className="px-3 py-2">
                          <div className="flex items-center gap-2 flex-wrap">
                            <span className={`px-1.5 py-0.5 rounded text-[11px] font-medium text-white ${SUBTASK_COLORS[st.status]}`}>
                              {st.status}
                            </span>
                            <span className="text-[11px] text-indigo-400/70">{ROLE_LABELS[st.agent_role] || st.agent_role}</span>
                            <span className="text-sm text-gray-400">{st.title}</span>
                            {st.target_repo && (
                              <span className="px-1.5 py-0.5 rounded text-[10px] font-mono bg-purple-500/10 border border-purple-500/20 text-purple-400/80">
                                {st.target_repo.name || 'main'}
                              </span>
                            )}
                            {st.error_message && !isOpen && (
                              <span className="text-[11px] text-red-400/60 font-mono truncate max-w-xs">{st.error_message}</span>
                            )}
                            {hasDetails && (
                              <button
                                onClick={() => setExpandedDetails((prev) => {
                                  const next = new Set(prev)
                                  if (next.has(st.id)) next.delete(st.id); else next.add(st.id)
                                  return next
                                })}
                                className="ml-auto px-2 py-0.5 bg-gray-800 hover:bg-gray-700 rounded text-[10px] text-gray-400 transition-colors shrink-0"
                              >
                                {isOpen ? 'Hide' : 'Details'}
                              </button>
                            )}
                          </div>

                          {/* Inline output summary for completed subtasks */}
                          {st.status === 'completed' && st.output_result && (() => {
                            const out = st.output_result
                            const summary = (out.summary || out.approach || out.root_cause || out.test_summary || out.executive_summary || out.merge_decision) as string | undefined
                            if (!summary) return null
                            return (
                              <p className="mt-1.5 text-[11px] text-gray-500 leading-relaxed line-clamp-2">{summary}</p>
                            )
                          })()}
                        </div>

                        {/* Expanded details */}
                        {isOpen && (
                          <div className="border-t border-gray-800/30 px-3 py-2.5 bg-gray-950/30 space-y-2">
                            {st.description && (
                              <details open>
                                <summary className="text-[10px] text-gray-600 uppercase tracking-wider cursor-pointer hover:text-gray-500">
                                  Task Instructions
                                </summary>
                                <pre className="mt-1.5 text-[11px] text-gray-500 font-mono whitespace-pre-wrap max-h-32 overflow-y-auto leading-relaxed bg-gray-950 rounded px-2.5 py-2 border border-gray-800/50">
                                  {st.description}
                                </pre>
                              </details>
                            )}

                            {hasInputCtx && (
                              <details>
                                <summary className="text-[10px] text-gray-600 uppercase tracking-wider cursor-pointer hover:text-gray-500">
                                  Agent Context
                                </summary>
                                <div className="mt-1.5 space-y-1.5">
                                  {st.input_context!.relevant_files && st.input_context!.relevant_files.length > 0 && (
                                    <div>
                                      <span className="text-[10px] text-gray-600">Files:</span>
                                      <div className="mt-0.5 flex flex-wrap gap-1">
                                        {st.input_context!.relevant_files.map((f: string, fi: number) => (
                                          <span key={fi} className="text-[11px] font-mono text-indigo-400/70 bg-indigo-500/5 px-1.5 py-0.5 rounded">{f}</span>
                                        ))}
                                      </div>
                                    </div>
                                  )}
                                  {st.input_context!.what_to_change && (
                                    <div>
                                      <span className="text-[10px] text-gray-600">What to change:</span>
                                      <p className="text-[11px] text-gray-500 mt-0.5 leading-relaxed">{st.input_context!.what_to_change}</p>
                                    </div>
                                  )}
                                </div>
                              </details>
                            )}

                            {/* Output result details */}
                            {st.output_result && (() => {
                              const out = st.output_result!
                              const role = st.agent_role
                              const items: Array<{ label: string; value: string | string[] | boolean | undefined | null }> = []

                              if (role === 'coder') {
                                if (out.approach) items.push({ label: 'Approach', value: out.approach as string })
                                if (out.files_changed && (out.files_changed as string[]).length > 0) items.push({ label: 'Files Changed', value: out.files_changed as string[] })
                              } else if (role === 'tester') {
                                if (out.test_summary) items.push({ label: 'Test Summary', value: out.test_summary as string })
                                if (out.passed != null) items.push({ label: 'Passed', value: (out.passed as boolean) ? 'Yes' : 'No' })
                              } else if (role === 'debugger') {
                                if (out.root_cause) items.push({ label: 'Root Cause', value: out.root_cause as string })
                                if (out.evidence && (out.evidence as string[]).length > 0) items.push({ label: 'Evidence', value: out.evidence as string[] })
                                if (out.recommendation) items.push({ label: 'Recommendation', value: out.recommendation as string })
                              } else if (role === 'reviewer') {
                                if (out.summary) items.push({ label: 'Summary', value: out.summary as string })
                              } else if (role === 'merge_agent') {
                                if (out.merge_decision) items.push({ label: 'Decision', value: out.merge_decision as string })
                                if (out.reason) items.push({ label: 'Reason', value: out.reason as string })
                              } else if (role === 'merge_observer') {
                                if (out.pr_merged != null) items.push({ label: 'PR Merged', value: (out.pr_merged as boolean) ? 'Yes' : 'No' })
                                if (out.merge_commit_sha) items.push({ label: 'Merge SHA', value: out.merge_commit_sha as string })
                              } else {
                                for (const [k, v] of Object.entries(out)) {
                                  if (k === 'raw_content' || k === 'content') continue
                                  if (typeof v === 'string' && v) items.push({ label: k, value: v })
                                  if (Array.isArray(v) && v.length > 0) items.push({ label: k, value: v as string[] })
                                }
                              }

                              if (items.length === 0) return null
                              return (
                                <div className="space-y-1.5">
                                  <div className="text-[10px] text-gray-600 uppercase tracking-wider">Output</div>
                                  {items.map((item, idx) => (
                                    <div key={idx}>
                                      <span className="text-[10px] text-gray-600">{item.label}:</span>
                                      {Array.isArray(item.value) ? (
                                        <div className="mt-0.5 pl-2 space-y-0.5">
                                          {(item.value as string[]).map((v, vi) => (
                                            <div key={vi} className="text-[11px] text-gray-400 font-mono flex items-start gap-1.5">
                                              <span className="w-1 h-1 rounded-full bg-gray-700 mt-1.5 shrink-0" />
                                              {String(v)}
                                            </div>
                                          ))}
                                        </div>
                                      ) : (
                                        <pre className="mt-0.5 text-[11px] text-gray-400 whitespace-pre-wrap leading-relaxed max-h-24 overflow-y-auto">
                                          {String(item.value)}
                                        </pre>
                                      )}
                                    </div>
                                  ))}
                                </div>
                              )
                            })()}

                            {st.error_message && (
                              <div>
                                <div className="text-[10px] text-red-400/70 uppercase tracking-wider mb-1">Error</div>
                                <pre className="text-[11px] text-red-300/50 font-mono whitespace-pre-wrap max-h-24 overflow-y-auto leading-relaxed bg-red-500/5 rounded px-2.5 py-2 border border-red-500/10">
                                  {st.error_message}
                                </pre>
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    )
                  })}
                </div>
              </details>
            )}

            {/* Deliverables */}
            {taskDeliverables.length > 0 && (() => {
              const currentDeliverables = taskDeliverables.filter((d: Deliverable) => !isTimestampPreviousRun(d.created_at))
              const previousDeliverables = taskDeliverables.filter((d: Deliverable) => isTimestampPreviousRun(d.created_at))
              if (currentDeliverables.length === 0 && previousDeliverables.length === 0) return null

              const renderDel = (d: Deliverable) => (
                <div key={d.id} className="p-3 bg-gray-900 rounded-lg border border-gray-800/50">
                  <div className="flex items-center gap-2 mb-1 flex-wrap">
                    <span className="px-1.5 py-0.5 bg-indigo-600/30 border border-indigo-500/20 rounded text-[11px] text-indigo-300">{d.type.replace('_', ' ')}</span>
                    <span className="text-sm text-gray-300">{d.title}</span>
                    {d.merged_at && (
                      <span className="px-1.5 py-0.5 bg-emerald-500/10 border border-emerald-500/20 rounded text-[10px] text-emerald-400/80">
                        merged{d.merge_method ? ` (${d.merge_method})` : ''}
                      </span>
                    )}
                    {d.pr_state && d.pr_state !== 'merged' && (
                      <span className="px-1.5 py-0.5 bg-gray-800 rounded text-[10px] text-gray-400">{d.pr_state}</span>
                    )}
                    {d.target_repo_name && (
                      <span className="px-1.5 py-0.5 bg-purple-500/10 border border-purple-500/20 rounded text-[10px] text-purple-400/80 font-mono">{d.target_repo_name}</span>
                    )}
                  </div>
                  {d.pr_url && (
                    <a href={d.pr_url} target="_blank" rel="noreferrer" className="text-[11px] text-indigo-400 hover:underline">{d.pr_url}</a>
                  )}
                  {d.type === 'code_diff' && d.content_json && (d.content_json as Record<string, unknown>).diff ? (
                    <DiffViewer
                      diff={(d.content_json as Record<string, unknown>).diff as string}
                      stats={(d.content_json as Record<string, unknown>).stats as string}
                      files={(d.content_json as Record<string, unknown>).files as Array<{status: string; path: string}>}
                    />
                  ) : d.content_md ? (
                    <pre className="mt-2 text-[11px] text-gray-500 whitespace-pre-wrap max-h-40 overflow-y-auto font-mono leading-relaxed">{d.content_md}</pre>
                  ) : null}
                </div>
              )

              return (
                <details className="mt-4" open>
                  <summary className="text-[10px] text-gray-600 uppercase tracking-wider cursor-pointer hover:text-gray-500">
                    Deliverables ({currentDeliverables.length}{previousDeliverables.length > 0 ? ` + ${previousDeliverables.length} previous` : ''})
                  </summary>
                  <div className="mt-1.5 space-y-1.5">
                    {currentDeliverables.map(renderDel)}
                    {previousDeliverables.length > 0 && (
                      <details className="mt-2">
                        <summary className="text-[10px] text-gray-600 cursor-pointer hover:text-gray-500">
                          Previous Run ({previousDeliverables.length})
                        </summary>
                        <div className="mt-1.5 space-y-1.5 opacity-40">
                          {previousDeliverables.map(renderDel)}
                        </div>
                      </details>
                    )}
                  </div>
                </details>
              )
            })()}
          </CollapsibleSection>
          )
        })()}

        {/* Execution Log (streaming tool events) */}
        {todoId && executionEvents.length > 0 && (
          <CollapsibleSection title="Execution Log" count={executionEvents.length} defaultOpen={false}>
            <div className="bg-gray-900 rounded-lg border border-gray-800/50 overflow-hidden">
              <ExecutionLog events={executionEvents} maxHeight="400px" />
            </div>
          </CollapsibleSection>
        )}

        {/* Progress Log (RALPH learnings) */}
        {todo.progress_log && todo.progress_log.length > 0 && (() => {
          const currentLog = todo.progress_log.filter((e: ProgressLogEntry) => !isTimestampPreviousRun(e.completed_at))
          const previousLog = todo.progress_log.filter((e: ProgressLogEntry) => isTimestampPreviousRun(e.completed_at))

          const renderLogEntry = (entry: ProgressLogEntry, i: number) => (
            <div key={i} className="p-3 bg-gray-900 rounded-lg border border-gray-800/50">
              <div className="flex items-center gap-2 mb-1">
                <span className={`px-1.5 py-0.5 rounded text-[11px] font-medium ${
                  entry.outcome === 'completed'
                    ? 'bg-emerald-500/10 text-emerald-400/80'
                    : 'bg-red-500/10 text-red-400/80'
                }`}>
                  {entry.outcome}
                </span>
                <span className="text-sm text-gray-300">{entry.sub_task_title}</span>
                <span className="ml-auto text-[11px] text-gray-600 font-mono">
                  {entry.iterations_used} iter{entry.iterations_used !== 1 ? 's' : ''}
                </span>
              </div>
              {entry.key_learnings.length > 0 && (
                <div className="mt-1.5 space-y-0.5">
                  {entry.key_learnings.map((l, li) => (
                    <div key={li} className="text-[11px] text-gray-500 flex items-start gap-1.5">
                      <span className="w-1 h-1 rounded-full bg-gray-700 mt-1.5 shrink-0" />
                      {l}
                    </div>
                  ))}
                </div>
              )}
            </div>
          )

          return (
            <CollapsibleSection title="Progress Log" count={currentLog.length} defaultOpen={false}>
              {currentLog.length > 0 && (
                <div className="space-y-1.5">
                  {currentLog.map(renderLogEntry)}
                </div>
              )}
              {previousLog.length > 0 && (
                <details className={currentLog.length > 0 ? 'mt-3' : ''}>
                  <summary className="text-[10px] text-gray-600 uppercase tracking-wider cursor-pointer hover:text-gray-500">
                    Previous Run ({previousLog.length})
                  </summary>
                  <div className="mt-1.5 space-y-1.5 opacity-40">
                    {previousLog.map(renderLogEntry)}
                  </div>
                </details>
              )}
            </CollapsibleSection>
          )
        })()}

        {/* Mobile chat toggle */}
        <button
          onClick={() => setShowMobileChat(!showMobileChat)}
          className="md:hidden mt-4 w-full flex items-center justify-center gap-2 px-4 py-2.5 bg-gray-900 border border-gray-800 rounded-lg text-sm text-gray-400 hover:text-white transition-colors"
        >
          <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M7.5 8.25h9m-9 3H12m-9.75 1.51c0 1.6 1.123 2.994 2.707 3.227 1.129.166 2.27.293 3.423.379.35.026.67.21.865.501L12 21l2.755-4.133a1.14 1.14 0 01.865-.501 48.172 48.172 0 003.423-.379c1.584-.233 2.707-1.626 2.707-3.228V6.741c0-1.602-1.123-2.995-2.707-3.228A48.394 48.394 0 0012 3c-2.392 0-4.744.175-7.043.513C3.373 3.746 2.25 5.14 2.25 6.741v6.018z" />
          </svg>
          {showMobileChat ? 'Hide Chat' : 'Show Chat'}
          {messages.length > 0 && (
            <span className="px-1.5 py-0.5 bg-gray-800 rounded text-[10px] text-gray-500">{messages.length}</span>
          )}
        </button>
        </>
        )}
      </div>

      {/* Chat sidebar */}
      <div className={`${showMobileChat ? 'flex' : 'hidden'} md:flex w-full md:w-96 border-t md:border-t-0 md:border-l border-gray-900 flex-col bg-gray-950 ${showMobileChat ? 'h-[50vh] md:h-auto' : ''}`}>
        <div className="px-4 py-3 border-b border-gray-900">
          <h2 className="text-sm font-medium text-gray-300">Chat</h2>
        </div>

        <div className="flex-1 overflow-y-auto px-4 py-3 space-y-2.5">
          {(() => {
            const previousMessages = messages.filter((msg: ChatMessage) => isTimestampPreviousRun(msg.created_at))
            const currentMessages = messages.filter((msg: ChatMessage) => !isTimestampPreviousRun(msg.created_at))

            const renderMessage = (msg: ChatMessage) => {
              const time = new Date(msg.created_at).toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' })
              const meta = msg.metadata_json as Record<string, unknown> | undefined
              return (
              <div
                key={msg.id}
                className={`text-sm rounded-lg p-3 ${
                  msg.role === 'user'
                    ? 'ml-6 bg-indigo-600/10 border border-indigo-500/15'
                    : msg.role === 'system'
                    ? 'bg-gray-900 border border-gray-800/50'
                    : 'mr-6 bg-gray-900 border border-gray-800/50'
                }`}
              >
                <div className="flex items-center gap-2 text-[11px] text-gray-600 mb-1">
                  <span>{msg.role === 'user' ? 'You' : msg.role === 'system' ? 'System' : 'Agent'}</span>
                  <span className="text-gray-700 font-mono">{time}</span>
                </div>
                <div className="text-gray-300 whitespace-pre-wrap text-[13px] leading-relaxed">{msg.content}</div>
                {meta?.action === 'plan_review_verdict' && (
                  <ReviewFeedbackCard
                    type="plan"
                    approved={!!meta.approved}
                    feedback={meta.feedback as string | undefined}
                    iteration={meta.iteration as number | undefined}
                  />
                )}
                {meta?.action === 'code_review_verdict' && (
                  <ReviewFeedbackCard
                    type="code"
                    approved={meta.verdict === 'approved'}
                    feedback={meta.feedback as string | undefined}
                    summary={meta.summary as string | undefined}
                    issues={meta.issues as Array<{ severity: 'critical' | 'major' | 'minor' | 'nit'; file?: string; line?: number | null; description: string; suggestion?: string }>}
                    subtaskTitle={meta.subtask_title as string | undefined}
                  />
                )}
              </div>
            )}

            return (
              <>
                {previousMessages.length > 0 && (
                  <details className="pb-2 mb-2 border-b border-gray-800/50">
                    <summary className="text-[10px] text-gray-600 uppercase tracking-wider cursor-pointer hover:text-gray-500">
                      Previous Run ({previousMessages.length} messages)
                    </summary>
                    <div className="mt-1.5 opacity-40 space-y-2.5">
                      {previousMessages.map(renderMessage)}
                    </div>
                  </details>
                )}
                {currentMessages.map(renderMessage)}
              </>
            )
          })()}
          <div ref={chatEndRef} />
        </div>

        <div className="px-4 py-3 border-t border-gray-900">
          <div className="flex gap-2">
            <input
              className="flex-1 px-3 py-2 bg-gray-900 border border-gray-800 rounded-lg text-white text-sm placeholder-gray-600 focus:outline-none focus:border-indigo-500 transition-colors"
              placeholder="Message the agent..."
              value={chatInput}
              onChange={(e) => setChatInput(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleSendChat()}
            />
            <button
              onClick={handleSendChat}
              disabled={!chatInput.trim()}
              className="px-3 py-2 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 rounded-lg text-sm text-white transition-colors"
            >
              Send
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

/** Inline input to inject guidance into a running subtask */
function SubtaskInjectInput({ todoId, subtaskId }: { todoId: string; subtaskId: string }) {
  const [value, setValue] = useState('')
  const [injecting, setInjecting] = useState(false)

  const handleInject = async () => {
    if (!value.trim() || injecting) return
    setInjecting(true)
    try {
      await todosApi.injectSubtask(todoId, subtaskId, value.trim())
      setValue('')
    } catch {
      // ignore
    } finally {
      setInjecting(false)
    }
  }

  return (
    <div className="mt-2 flex items-center gap-1.5">
      <input
        type="text"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => e.key === 'Enter' && handleInject()}
        placeholder="Guide this agent..."
        className="flex-1 px-2.5 py-1 bg-gray-950 border border-gray-800 rounded-lg text-[11px] text-white placeholder-gray-600 focus:outline-none focus:border-indigo-500 transition-colors"
      />
      <button
        onClick={handleInject}
        disabled={!value.trim() || injecting}
        className="px-2.5 py-1 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 rounded-lg text-[11px] text-white transition-colors shrink-0"
      >
        {injecting ? '...' : 'Inject'}
      </button>
    </div>
  )
}
