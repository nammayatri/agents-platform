import { useEffect, useState, useRef } from 'react'
import { useParams } from 'react-router-dom'
import { useTodoStore } from '../stores/todoStore'
import { useTaskWebSocket } from '../hooks/useTaskWebSocket'
import DiffViewer from '../components/DiffViewer'
import type { SubTask, ChatMessage, Deliverable, PlanSubTask, IterationLogEntry, ProgressLogEntry } from '../types'

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
}

const ROLE_LABELS: Record<string, string> = {
  coder: 'Code',
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
  const {
    todos, chatMessages, deliverablesByTodo,
    fetchTodo, fetchChat, fetchDeliverables, sendChat,
    cancelTodo, retryTodo, acceptDeliverables, requestChanges,
    approvePlan, rejectPlan,
  } = useTodoStore()

  useTaskWebSocket(todoId || null)

  const [chatInput, setChatInput] = useState('')
  const [changesFeedback, setChangesFeedback] = useState('')
  const [rejectFeedback, setRejectFeedback] = useState('')
  const [showRejectForm, setShowRejectForm] = useState(false)
  const [expandedSubTasks, setExpandedSubTasks] = useState<Set<string>>(new Set())
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
    }
  }, [todoId, fetchTodo, fetchChat, fetchDeliverables])

  const todo = todoId ? todos[todoId] : undefined
  const messages = todoId ? chatMessages[todoId] || [] : []
  const taskDeliverables = todoId ? deliverablesByTodo[todoId] || []  : []

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages.length])

  const handleSendChat = async () => {
    if (!todoId || !chatInput.trim()) return
    await sendChat(todoId, chatInput.trim())
    setChatInput('')
  }

  if (!todo) {
    return <div className="p-6 text-gray-600 text-sm">Loading...</div>
  }

  return (
    <div className="flex h-full">
      {/* Main content */}
      <div className="flex-1 p-6 overflow-y-auto">
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
            <p className="text-sm text-red-300/60 font-mono break-words">{todo.error_message}</p>
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
        <div className="flex gap-2 mb-6">
          {todo.state === 'review' && (
            <>
              <button
                onClick={() => todoId && acceptDeliverables(todoId)}
                className="px-3 py-1.5 bg-emerald-600 hover:bg-emerald-500 rounded-lg text-sm text-white transition-colors"
              >
                Accept & Complete
              </button>
              <div className="flex gap-1">
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
          return (
            <div className="mb-6">
              <h2 className="text-sm font-medium text-gray-300 mb-3 uppercase tracking-wider">Review Chains</h2>
              <div className="space-y-3">
                {Array.from(chains.entries()).map(([chainId, chainTasks]) => (
                  <div key={chainId} className="p-3 bg-gray-900 rounded-lg border border-gray-800/50">
                    <div className="flex items-center gap-1.5 flex-wrap">
                      {chainTasks
                        .sort((a, b) => (a.execution_order || 0) - (b.execution_order || 0))
                        .map((st, i) => (
                          <div key={st.id} className="flex items-center gap-1.5">
                            {i > 0 && <span className="text-gray-700 text-xs">→</span>}
                            <span className={`px-2 py-0.5 rounded text-[11px] font-medium flex items-center gap-1 ${
                              st.status === 'completed' ? 'bg-emerald-500/10 text-emerald-400/80 border border-emerald-500/20' :
                              st.status === 'running' ? 'bg-amber-500/10 text-amber-400/80 border border-amber-500/20' :
                              st.status === 'failed' ? 'bg-red-500/10 text-red-400/80 border border-red-500/20' :
                              'bg-gray-800 text-gray-400 border border-gray-700'
                            }`}>
                              <span>{ROLE_LABELS[st.agent_role] || st.agent_role}</span>
                              {st.review_verdict === 'approved' && <span className="text-emerald-400">✓</span>}
                              {st.review_verdict === 'needs_changes' && <span className="text-amber-400">✗</span>}
                            </span>
                          </div>
                        ))}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )
        })()}

        {/* Sub-tasks with RALPH iteration details */}
        {todo.sub_tasks && todo.sub_tasks.length > 0 && (
          <div className="mb-6">
            <h2 className="text-sm font-medium text-gray-300 mb-3 uppercase tracking-wider">Sub-tasks</h2>
            <div className="space-y-1.5">
              {todo.sub_tasks.map((st: SubTask) => {
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
                          <span className="ml-auto text-[11px] text-gray-600 font-mono shrink-0">
                            {iterLog.length} iter{iterLog.length !== 1 ? 's' : ''}
                            {failedCount > 0 && <span className="text-red-400/60 ml-1">{failedCount} fail</span>}
                            {passedCount > 0 && <span className="text-emerald-400/60 ml-1">{passedCount} pass</span>}
                          </span>
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
                        </div>
                      )}
                      {st.error_message && (
                        <div className="text-[11px] text-red-400/60 mt-1 font-mono">{st.error_message}</div>
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
          </div>
        )}

        {/* Progress Log (RALPH learnings) */}
        {todo.progress_log && todo.progress_log.length > 0 && (
          <div className="mb-6">
            <h2 className="text-sm font-medium text-gray-300 mb-3 uppercase tracking-wider">Progress Log</h2>
            <div className="space-y-1.5">
              {todo.progress_log.map((entry: ProgressLogEntry, i: number) => (
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
              ))}
            </div>
          </div>
        )}

        {/* Deliverables */}
        {taskDeliverables.length > 0 && (
          <div className="mb-6">
            <h2 className="text-sm font-medium text-gray-300 mb-3 uppercase tracking-wider">Deliverables</h2>
            <div className="space-y-1.5">
              {taskDeliverables.map((d: Deliverable) => (
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
              ))}
            </div>
          </div>
        )}

        {/* Metrics */}
        <div className="flex gap-4 text-[11px] text-gray-600 font-mono border-t border-gray-900 pt-4">
          <span>tokens: {todo.actual_tokens.toLocaleString()}</span>
          <span>cost: ${todo.cost_usd.toFixed(4)}</span>
          <span>retries: {todo.retry_count}</span>
        </div>
      </div>

      {/* Chat sidebar */}
      <div className="w-96 border-l border-gray-900 flex flex-col bg-gray-950">
        <div className="px-4 py-3 border-b border-gray-900">
          <h2 className="text-sm font-medium text-gray-300">Chat</h2>
        </div>

        <div className="flex-1 overflow-y-auto px-4 py-3 space-y-2.5">
          {messages.map((msg: ChatMessage) => (
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
          ))}
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
