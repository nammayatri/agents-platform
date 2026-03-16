import { useEffect, useState, useRef } from 'react'
import { useParams } from 'react-router-dom'
import { useTodoStore } from '../stores/todoStore'
import { useTaskWebSocket } from '../hooks/useTaskWebSocket'
import DiffViewer from '../components/DiffViewer'
import type { SubTask, ChatMessage, Deliverable, AgentRun, PlanSubTask, IterationLogEntry, ProgressLogEntry } from '../types'

const STATE_COLORS: Record<string, string> = {
  intake: 'bg-violet-600',
  planning: 'bg-blue-600',
  plan_ready: 'bg-cyan-600',
  in_progress: 'bg-amber-600',
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
}

const VERDICT_COLORS: Record<string, string> = {
  approved: 'bg-emerald-500/10 text-emerald-400/80 border-emerald-500/20',
  needs_changes: 'bg-amber-500/10 text-amber-400/80 border-amber-500/20',
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
  const llmResponses = useTodoStore((s) => s.llmResponses)
  const agentRunsByTodo = useTodoStore((s) => s.agentRunsByTodo)
  const fetchAgentRuns = useTodoStore((s) => s.fetchAgentRuns)

  useTaskWebSocket(todoId || null)

  const [chatInput, setChatInput] = useState('')
  const [changesFeedback, setChangesFeedback] = useState('')
  const [rejectFeedback, setRejectFeedback] = useState('')
  const [showRejectForm, setShowRejectForm] = useState(false)
  const [rejectMergeFeedback, setRejectMergeFeedback] = useState('')
  const [showRejectMergeForm, setShowRejectMergeForm] = useState(false)
  const [expandedSubTasks, setExpandedSubTasks] = useState<Set<string>>(new Set())
  const [expandedActivity, setExpandedActivity] = useState<Set<string>>(new Set())
  const [expandedLlmResponse, setExpandedLlmResponse] = useState<Set<string>>(new Set())
  const [showMobileChat, setShowMobileChat] = useState(false)
  const activityEndRefs = useRef<Record<string, HTMLDivElement | null>>({})
  const chatEndRef = useRef<HTMLDivElement>(null)

  const toggleSubTask = (id: string) => {
    setExpandedSubTasks((prev) => {
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

  // Previous-run detection: when a todo is retried, items from before the last
  // state change are considered "previous run" and greyed out.
  const todoActive = todo ? !['completed', 'failed', 'cancelled'].includes(todo.state) : false
  const stateChangedAt = todo?.state_changed_at ? new Date(todo.state_changed_at).getTime() : 0
  const hasPreviousRun = todoActive && stateChangedAt > 0

  const isSubTaskPreviousRun = (st: SubTask) =>
    hasPreviousRun &&
    new Date(st.created_at).getTime() < stateChangedAt &&
    (st.status === 'completed' || st.status === 'failed')

  const isTimestampPreviousRun = (createdAt: string) =>
    hasPreviousRun && new Date(createdAt).getTime() < stateChangedAt

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
    return <div className="p-6 text-gray-600 text-sm">Loading...</div>
  }

  return (
    <div className="flex flex-col md:flex-row h-full">
      {/* Main content */}
      <div className="flex-1 p-4 md:p-6 overflow-y-auto">
        {/* Header */}
        <div className="mb-6">
          <div className="flex items-center gap-2.5 mb-2">
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
          {todo.provider_name && (
            <div className="mt-2 inline-flex items-center gap-1.5 px-2.5 py-1 bg-gray-900 border border-gray-800 rounded text-[11px] text-gray-500">
              <span className="w-1.5 h-1.5 rounded-full bg-emerald-500" />
              <span>{todo.provider_name}</span>
              {todo.provider_model && (
                <span className="text-gray-600 font-mono">· {todo.provider_model}</span>
              )}
            </div>
          )}
        </div>

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
              {todo.plan_json.sub_tasks.map((st: PlanSubTask, i: number) => (
                <div key={i} className="px-4 py-3 flex items-start gap-3">
                  <span className="text-[11px] text-gray-600 font-mono mt-0.5 w-5 text-right shrink-0">{i + 1}</span>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 mb-0.5">
                      <span className="text-sm text-gray-200">{st.title}</span>
                      <span className="text-[11px] px-1.5 py-0.5 bg-gray-800 rounded text-gray-500">
                        {ROLE_LABELS[st.agent_role] || st.agent_role}
                      </span>
                    </div>
                    {st.description && (
                      <p className="text-[11px] text-gray-600 leading-relaxed">{st.description}</p>
                    )}
                    {st.depends_on.length > 0 && (
                      <span className="text-[11px] text-gray-700 font-mono">
                        depends on: {st.depends_on.map((d) => `#${d + 1}`).join(', ')}
                      </span>
                    )}
                  </div>
                  <span className="text-[11px] text-gray-700 font-mono shrink-0">
                    order {st.execution_order}
                  </span>
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
          {['intake', 'planning', 'plan_ready', 'in_progress', 'review'].includes(todo.state) && (
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
            <div className="mb-6">
              <h2 className="text-sm font-medium text-gray-300 mb-3 uppercase tracking-wider">Review Chains</h2>
              <div className="space-y-3">
                {currentChains.map(renderChain)}
              </div>
              {previousChains.length > 0 && (
                <div className="mt-3">
                  <div className="text-[10px] text-gray-600 uppercase tracking-wider mb-1.5">Previous Run</div>
                  <div className="space-y-3 opacity-40">
                    {previousChains.map(renderChain)}
                  </div>
                </div>
              )}
            </div>
          )
        })()}

        {/* Sub-tasks with RALPH iteration details */}
        {todo.sub_tasks && todo.sub_tasks.length > 0 && (() => {
          const currentTasks = todo.sub_tasks.filter((st: SubTask) => !isSubTaskPreviousRun(st))
          const previousTasks = todo.sub_tasks.filter((st: SubTask) => isSubTaskPreviousRun(st))

          return (
          <div className="mb-6">
            <h2 className="text-sm font-medium text-gray-300 mb-3 uppercase tracking-wider">Sub-tasks</h2>
            <div className="space-y-1.5">
              {currentTasks.map((st: SubTask) => {
                const iterLog = st.iteration_log || []
                const hasIterations = iterLog.length > 0
                const isExpanded = expandedSubTasks.has(st.id)
                const passedCount = iterLog.filter((e) => e.outcome === 'passed').length
                const failedCount = iterLog.filter((e) => e.outcome !== 'passed').length
                const lastStuck = [...iterLog].reverse().find((e) => e.stuck_check?.stuck)

                return (
                  <div key={st.id} className="bg-gray-900 rounded-lg border border-gray-800/50 overflow-hidden">
                    {/* Sub-task header */}
                    <div
                      className={`p-3 ${hasIterations ? 'cursor-pointer hover:bg-gray-800/30' : ''}`}
                      onClick={() => hasIterations && toggleSubTask(st.id)}
                    >
                      <div className="flex items-center gap-2 mb-1 flex-wrap">
                        {hasIterations && (
                          <span className="text-gray-600 text-[11px] w-3 shrink-0">{isExpanded ? '\u25BC' : '\u25B6'}</span>
                        )}
                        <span className={`px-1.5 py-0.5 rounded text-[11px] font-medium text-white ${SUBTASK_COLORS[st.status]}`}>
                          {st.status}
                        </span>
                        <span className="text-[11px] text-indigo-400/70">{ROLE_LABELS[st.agent_role] || st.agent_role}</span>
                        <span className="text-sm text-gray-300">{st.title}</span>
                        {st.review_loop && (
                          <span className="px-1.5 py-0.5 bg-cyan-500/10 border border-cyan-500/20 rounded text-[10px] text-cyan-400/80">review loop</span>
                        )}
                        {st.review_verdict && (
                          <span className={`px-1.5 py-0.5 border rounded text-[10px] ${VERDICT_COLORS[st.review_verdict] || 'bg-gray-800 text-gray-400'}`}>
                            {st.review_verdict === 'approved' ? 'approved' : 'changes requested'}
                          </span>
                        )}
                        {st.target_repo && (
                          <span className="px-1.5 py-0.5 bg-purple-500/10 border border-purple-500/20 rounded text-[10px] text-purple-400/80 font-mono">
                            {st.target_repo.name}
                          </span>
                        )}
                        {hasIterations && (
                          <span className="text-[11px] text-gray-600 font-mono shrink-0">
                            {iterLog.length} iter{iterLog.length !== 1 ? 's' : ''}
                            {failedCount > 0 && <span className="text-red-400/60 ml-1">{failedCount} fail</span>}
                            {passedCount > 0 && <span className="text-emerald-400/60 ml-1">{passedCount} pass</span>}
                          </span>
                        )}
                        {(st.status === 'pending' || st.status === 'failed') && todoId && (
                          <div className="ml-auto shrink-0">
                            <button
                              onClick={(e) => { e.stopPropagation(); triggerSubTask(todoId, st.id, true) }}
                              className="px-2 py-0.5 bg-gray-800 hover:bg-gray-700 rounded text-[10px] text-gray-400 transition-colors"
                              title="Force run — skip dependency checks"
                            >
                              Force Run
                            </button>
                          </div>
                        )}
                      </div>
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
                                  <span className="font-mono truncate max-w-xs">{latest}</span>
                                  <span className="text-gray-700 shrink-0">({logs.length})</span>
                                </button>
                                {isOpen && (
                                  <div className="mt-1.5 max-h-40 overflow-y-auto bg-gray-950 border border-gray-800/50 rounded px-2.5 py-1.5 space-y-0.5">
                                    {logs.map((entry, i) => (
                                      <div key={i} className="text-[11px] font-mono text-gray-600 leading-relaxed">
                                        <span className="text-gray-700 mr-1.5 select-none">{'\u203A'}</span>{entry}
                                      </div>
                                    ))}
                                    <div ref={(el) => { activityEndRefs.current[st.id] = el }} />
                                  </div>
                                )}
                              </div>
                            )
                          })()}
                        </div>
                      )}
                      {/* LLM Response */}
                      {(() => {
                        const lr = llmResponses[st.id]
                        if (!lr) return null
                        const isOpen = expandedLlmResponse.has(st.id)
                        const preview = lr.content.length > 120 ? lr.content.slice(0, 120) + '...' : lr.content
                        return (
                          <div className="mt-2">
                            <button
                              type="button"
                              className="flex items-center gap-1.5 text-[11px] text-indigo-400/70 hover:text-indigo-400 transition-colors"
                              onClick={(e) => {
                                e.stopPropagation()
                                setExpandedLlmResponse((prev) => {
                                  const next = new Set(prev)
                                  if (next.has(st.id)) next.delete(st.id); else next.add(st.id)
                                  return next
                                })
                              }}
                            >
                              <span className="w-3">{isOpen ? '\u25BC' : '\u25B6'}</span>
                              <span className="shrink-0">LLM Response</span>
                              <span className="text-gray-700">(iter {lr.iteration})</span>
                              {!isOpen && <span className="text-gray-600 truncate max-w-xs font-mono">{preview}</span>}
                            </button>
                            {isOpen && (
                              <div className="mt-1.5 max-h-60 overflow-y-auto bg-gray-950 border border-indigo-500/10 rounded px-3 py-2">
                                <pre className="text-[11px] text-gray-400 whitespace-pre-wrap font-mono leading-relaxed">{lr.content}</pre>
                              </div>
                            )}
                          </div>
                        )
                      })()}
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

                    {/* Reviewer output details */}
                    {st.agent_role === 'reviewer' && st.output_result && (
                      <div className="border-t border-gray-800 px-3 py-2.5">
                        {st.output_result.summary && (
                          <p className="text-[11px] text-gray-400 mb-2">{st.output_result.summary}</p>
                        )}

                        {st.output_result.issues && st.output_result.issues.length > 0 && (
                          <div className="space-y-1.5">
                            {(st.output_result.issues as Array<{ severity: string; description: string; suggestion?: string }>).map((issue, idx) => (
                              <div key={idx} className="flex items-start gap-2">
                                <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded shrink-0 ${
                                  issue.severity === 'critical' ? 'bg-red-500/10 text-red-400' :
                                  issue.severity === 'major' ? 'bg-amber-500/10 text-amber-400' :
                                  issue.severity === 'minor' ? 'bg-gray-800 text-gray-400' :
                                  'bg-gray-800 text-gray-600'
                                }`}>
                                  {issue.severity}
                                </span>
                                <div className="flex-1 min-w-0">
                                  <p className="text-[11px] text-gray-300">{issue.description}</p>
                                  {issue.suggestion && (
                                    <p className="text-[11px] text-gray-600 mt-0.5">{'\u2192'} {issue.suggestion}</p>
                                  )}
                                </div>
                              </div>
                            ))}
                          </div>
                        )}

                        {st.output_result.needs_human_review && (
                          <div className="mt-2 text-[10px] text-amber-400/70 flex items-center gap-1">
                            <span>{'\u26A0'}</span> Needs human review
                          </div>
                        )}
                      </div>
                    )}

                    {/* Expanded iteration log */}
                    {isExpanded && hasIterations && (
                      <div className="border-t border-gray-800">
                        <div className="px-3 py-2 bg-gray-950/50">
                          <span className="text-[10px] text-gray-600 uppercase tracking-wider">Iteration Log</span>
                        </div>
                        <div className="max-h-64 overflow-y-auto divide-y divide-gray-800/50">
                          {iterLog.map((entry: IterationLogEntry, i: number) => (
                            <div key={i} className="px-3 py-2 text-[11px]">
                              <div className="flex items-center gap-2">
                                <span className="text-gray-600 font-mono w-6 shrink-0">#{entry.iteration}</span>
                                <span className={`px-1.5 py-0.5 rounded font-medium ${
                                  entry.outcome === 'passed'
                                    ? 'bg-emerald-500/10 text-emerald-400/80'
                                    : 'bg-red-500/10 text-red-400/80'
                                }`}>
                                  {entry.outcome}
                                </span>
                                <span className="text-gray-700">{entry.action}</span>
                                <span className="ml-auto text-gray-700 font-mono">{entry.tokens_used.toLocaleString()} tok</span>
                              </div>
                              {entry.learnings.length > 0 && (
                                <div className="mt-1 pl-8 space-y-0.5">
                                  {entry.learnings.map((l, li) => (
                                    <div key={li} className="text-gray-500 flex items-start gap-1.5">
                                      <span className="w-1 h-1 rounded-full bg-gray-700 mt-1.5 shrink-0" />
                                      {l}
                                    </div>
                                  ))}
                                </div>
                              )}
                              {entry.llm_response && (
                                <details className="mt-1 pl-8">
                                  <summary className="text-indigo-400/60 cursor-pointer hover:text-indigo-400/80 text-[10px]">
                                    LLM Response ({entry.llm_response.length} chars)
                                  </summary>
                                  <pre className="mt-1 text-gray-500 font-mono whitespace-pre-wrap max-h-40 overflow-y-auto leading-relaxed">
                                    {entry.llm_response}
                                  </pre>
                                </details>
                              )}
                              {entry.error_output && (
                                <pre className="mt-1 pl-8 text-red-400/50 font-mono whitespace-pre-wrap max-h-20 overflow-y-auto">
                                  {entry.error_output}
                                </pre>
                              )}
                              {entry.stuck_check?.stuck && (
                                <div className="mt-1 pl-8 px-2 py-1 bg-amber-500/5 border border-amber-500/10 rounded">
                                  <span className="text-amber-300/70">Stuck: {entry.stuck_check.pattern}</span>
                                  {entry.stuck_check.advice && (
                                    <div className="text-amber-200/50 mt-0.5">Advice: {entry.stuck_check.advice}</div>
                                  )}
                                </div>
                              )}
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                )
              })}
            </div>

            {/* Previous run sub-tasks (greyed out) */}
            {previousTasks.length > 0 && (
              <div className="mt-4">
                <div className="text-[10px] text-gray-600 uppercase tracking-wider mb-1.5">Previous Run</div>
                <div className="space-y-1 opacity-40">
                  {previousTasks.map((st: SubTask) => (
                    <div key={st.id} className="px-3 py-2 bg-gray-900/50 rounded-lg border border-gray-800/30">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className={`px-1.5 py-0.5 rounded text-[11px] font-medium text-white ${SUBTASK_COLORS[st.status]}`}>
                          {st.status}
                        </span>
                        <span className="text-[11px] text-indigo-400/70">{ROLE_LABELS[st.agent_role] || st.agent_role}</span>
                        <span className="text-sm text-gray-400">{st.title}</span>
                        {st.error_message && (
                          <span className="ml-auto text-[11px] text-red-400/60 font-mono truncate max-w-xs">{st.error_message}</span>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
          )
        })()}

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
            <div className="mb-6">
              <h2 className="text-sm font-medium text-gray-300 mb-3 uppercase tracking-wider">Progress Log</h2>
              {currentLog.length > 0 && (
                <div className="space-y-1.5">
                  {currentLog.map(renderLogEntry)}
                </div>
              )}
              {previousLog.length > 0 && (
                <div className={currentLog.length > 0 ? 'mt-3' : ''}>
                  <div className="text-[10px] text-gray-600 uppercase tracking-wider mb-1.5">Previous Run</div>
                  <div className="space-y-1.5 opacity-40">
                    {previousLog.map(renderLogEntry)}
                  </div>
                </div>
              )}
            </div>
          )
        })()}

        {/* Deliverables */}
        {taskDeliverables.length > 0 && (() => {
          const currentDeliverables = taskDeliverables.filter((d: Deliverable) => !isTimestampPreviousRun(d.created_at))
          const previousDeliverables = taskDeliverables.filter((d: Deliverable) => isTimestampPreviousRun(d.created_at))

          const renderDeliverable = (d: Deliverable) => (
            <div key={d.id} className="p-3 bg-gray-900 rounded-lg border border-gray-800/50">
              <div className="flex items-center gap-2 mb-1">
                <span className="px-1.5 py-0.5 bg-indigo-600/30 border border-indigo-500/20 rounded text-[11px] text-indigo-300">{d.type.replace('_', ' ')}</span>
                <span className="text-sm text-gray-300">{d.title}</span>
                {d.merged_at && (
                  <span className="px-1.5 py-0.5 bg-emerald-500/10 border border-emerald-500/20 rounded text-[10px] text-emerald-400/80">
                    merged{d.merge_method ? ` (${d.merge_method})` : ''}
                  </span>
                )}
                {d.pr_state && d.pr_state !== 'merged' && (
                  <span className="px-1.5 py-0.5 bg-gray-800 rounded text-[10px] text-gray-400">
                    {d.pr_state}
                  </span>
                )}
                {d.target_repo_name && (
                  <span className="px-1.5 py-0.5 bg-purple-500/10 border border-purple-500/20 rounded text-[10px] text-purple-400/80 font-mono">
                    {d.target_repo_name}
                  </span>
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
                <pre className="mt-2 text-[11px] text-gray-500 whitespace-pre-wrap max-h-40 overflow-y-auto font-mono leading-relaxed">
                  {d.content_md}
                </pre>
              ) : null}
            </div>
          )

          return (
            <div className="mb-6">
              <h2 className="text-sm font-medium text-gray-300 mb-3 uppercase tracking-wider">Deliverables</h2>
              {currentDeliverables.length > 0 && (
                <div className="space-y-1.5">
                  {currentDeliverables.map(renderDeliverable)}
                </div>
              )}
              {previousDeliverables.length > 0 && (
                <div className={currentDeliverables.length > 0 ? 'mt-3' : ''}>
                  <div className="text-[10px] text-gray-600 uppercase tracking-wider mb-1.5">Previous Run</div>
                  <div className="space-y-1.5 opacity-40">
                    {previousDeliverables.map(renderDeliverable)}
                  </div>
                </div>
              )}
            </div>
          )
        })()}

        {/* Metrics */}
        <div className="flex gap-4 text-[11px] text-gray-600 font-mono border-t border-gray-900 pt-4">
          <span>tokens: {todo.actual_tokens.toLocaleString()}</span>
          <span>cost: ${todo.cost_usd.toFixed(4)}</span>
          <span>retries: {todo.retry_count}</span>
        </div>

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

            const renderMessage = (msg: ChatMessage) => (
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
                <div className="text-[11px] text-gray-600 mb-1">
                  {msg.role === 'user' ? 'You' : msg.role === 'system' ? 'System' : 'Agent'}
                </div>
                <div className="text-gray-300 whitespace-pre-wrap text-[13px] leading-relaxed">{msg.content}</div>
              </div>
            )

            return (
              <>
                {previousMessages.length > 0 && (
                  <div className="opacity-40 space-y-2.5 pb-2 mb-2 border-b border-gray-800/50">
                    <div className="text-[10px] text-gray-600 uppercase tracking-wider">Previous Run</div>
                    {previousMessages.map(renderMessage)}
                  </div>
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
