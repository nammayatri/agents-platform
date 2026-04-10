import { useState } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'
import type { ChatPlanData } from '../../types'

const ROLE_LABELS: Record<string, string> = {
  coder: 'Code',
  tester: 'Test',
  reviewer: 'Review',
  pr_creator: 'PR',
  report_writer: 'Report',
  merge_agent: 'Merge',
  debugger: 'Debug',
  release_build_watcher: 'Build',
  release_deployer: 'Deploy',
}

const PRIORITY_COLORS: Record<string, string> = {
  critical: 'text-red-400',
  high: 'text-amber-400',
  medium: 'text-gray-400',
  low: 'text-gray-600',
}

interface PlanReviewCardProps {
  planData: ChatPlanData
  isAccepted: boolean
  isLatest?: boolean
  onAccept: () => void
  onReject: (feedback: string) => void
  disabled?: boolean
}

export default function PlanReviewCard({
  planData,
  isAccepted,
  isLatest,
  onAccept,
  onReject,
  disabled,
}: PlanReviewCardProps) {
  const [showRejectInput, setShowRejectInput] = useState(false)
  const [feedback, setFeedback] = useState('')
  const [expandedSubtasks, setExpandedSubtasks] = useState<Set<number>>(new Set())

  // Support both new (singular task) and legacy (tasks array) formats
  const tasks = planData.task ? [planData.task] : (planData.tasks || [])
  const title = planData.plan_title || 'Execution Plan'

  const toggleSubtask = (idx: number) => {
    setExpandedSubtasks(prev => {
      const next = new Set(prev)
      if (next.has(idx)) next.delete(idx); else next.add(idx)
      return next
    })
  }

  const handleRejectSubmit = () => {
    if (!feedback.trim()) return
    onReject(feedback.trim())
    setFeedback('')
    setShowRejectInput(false)
  }

  return (
    <div
      className={`mt-2 rounded-lg overflow-hidden border ${
        isAccepted
          ? 'border-emerald-500/20 bg-gray-900'
          : 'border-amber-500/20 bg-gray-900'
      }`}
    >
      {/* Header */}
      <div
        className={`px-4 py-2.5 border-b border-gray-800 flex items-center gap-2 ${
          isAccepted ? 'bg-emerald-500/5' : 'bg-amber-500/5'
        }`}
      >
        <svg
          className={`w-4 h-4 shrink-0 ${isAccepted ? 'text-emerald-400' : 'text-amber-400'}`}
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth={2}
        >
          {isAccepted ? (
            <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
          ) : (
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M9 12h3.75M9 15h3.75M9 18h3.75m3 .75H18a2.25 2.25 0 002.25-2.25V6.108c0-1.135-.845-2.098-1.976-2.192a48.424 48.424 0 00-1.123-.08m-5.801 0c-.065.21-.1.433-.1.664 0 .414.336.75.75.75h4.5a.75.75 0 00.75-.75 2.25 2.25 0 00-.1-.664m-5.8 0A2.251 2.251 0 0113.5 2.25H15c1.012 0 1.867.668 2.15 1.586m-5.8 0c-.376.023-.75.05-1.124.08C9.095 4.01 8.25 4.973 8.25 6.108V8.25m0 0H4.875c-.621 0-1.125.504-1.125 1.125v11.25c0 .621.504 1.125 1.125 1.125h9.75c.621 0 1.125-.504 1.125-1.125V9.375c0-.621-.504-1.125-1.125-1.125H8.25z"
            />
          )}
        </svg>
        <span className={`text-sm font-medium ${isAccepted ? 'text-emerald-400' : 'text-amber-400'}`}>
          {isAccepted ? 'Plan Accepted' : title}
        </span>
        <span className="text-[11px] text-gray-600 ml-auto">
          {tasks[0]?.subtasks?.length || 0} subtasks
        </span>
      </div>

      {/* Tasks */}
      <div className="divide-y divide-gray-800">
        {tasks.map((task, taskIdx) => (
          <div key={taskIdx} className="px-4 py-3">
            {/* Task header */}
            <div className="flex items-center gap-2 mb-1.5">
              <span className="text-[11px] text-gray-600 font-mono w-4 text-right shrink-0">
                {taskIdx + 1}
              </span>
              <span className="text-sm text-gray-200 font-medium">{task.title}</span>
              {task.priority && task.priority !== 'medium' && (
                <span className={`text-[10px] ${PRIORITY_COLORS[task.priority] || 'text-gray-500'}`}>
                  {task.priority}
                </span>
              )}
            </div>
            {task.description && (
              <p className="text-[11px] text-gray-500 ml-6 mb-2 leading-relaxed">
                {task.description}
              </p>
            )}

            {/* Subtasks */}
            {task.subtasks && task.subtasks.length > 0 && (
              <div className="ml-6 space-y-0.5">
                {task.subtasks.map((st, stIdx) => {
                  const globalIdx = taskIdx * 100 + stIdx
                  const isExpanded = expandedSubtasks.has(globalIdx)
                  const hasDetails = st.scope || st.requirements || st.approach || st.goal || st.context || st.description

                  return (
                    <div key={stIdx}>
                      <div
                        className={`flex items-start gap-2 py-1 ${hasDetails ? 'cursor-pointer hover:bg-gray-800/30 rounded px-1 -mx-1' : ''}`}
                        onClick={() => hasDetails && toggleSubtask(globalIdx)}
                      >
                        {hasDetails ? (
                          <span className="text-gray-600 mt-0.5 shrink-0">
                            {isExpanded ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
                          </span>
                        ) : (
                          <span className="text-[10px] text-gray-700 font-mono w-3 text-right shrink-0 mt-0.5">
                            {stIdx + 1}
                          </span>
                        )}
                        <span className="text-[11px] px-1.5 py-0.5 bg-gray-800 rounded text-gray-500 shrink-0">
                          {ROLE_LABELS[st.agent_role] || st.agent_role}
                        </span>
                        <span className="text-xs text-gray-400 flex-1">{st.title}</span>
                        {st.review_loop && (
                          <span className="px-1 py-0.5 bg-cyan-500/10 border border-cyan-500/20 rounded text-[10px] text-cyan-400/80 shrink-0">review</span>
                        )}
                        <span className={`px-1 py-0.5 rounded text-[10px] font-mono shrink-0 ${
                          st.target_repo && String(st.target_repo) !== 'main'
                            ? 'bg-purple-500/10 border border-purple-500/20 text-purple-400/80'
                            : 'bg-gray-800 text-gray-500'
                        }`}>
                          {String(st.target_repo || 'main')}
                        </span>
                        {st.depends_on && st.depends_on.length > 0 && (
                          <span className="text-[10px] text-gray-700 font-mono shrink-0">
                            {'\u2192'} #{st.depends_on.map((d) => d + 1).join(', #')}
                          </span>
                        )}
                      </div>
                      {isExpanded && (
                        <div className="ml-5 pl-3 border-l border-gray-800 mb-2 space-y-1.5 py-1.5">
                          {st.scope && (
                            <div>
                              <span className="text-[10px] text-gray-600 uppercase tracking-wider">Scope</span>
                              <p className="text-[11px] text-gray-400 mt-0.5 leading-relaxed">{String(st.scope)}</p>
                            </div>
                          )}
                          {st.requirements && (
                            <div>
                              <span className="text-[10px] text-gray-600 uppercase tracking-wider">Requirements</span>
                              <p className="text-[11px] text-gray-400 mt-0.5 leading-relaxed">{String(st.requirements)}</p>
                            </div>
                          )}
                          {st.approach && (
                            <div>
                              <span className="text-[10px] text-gray-600 uppercase tracking-wider">Approach</span>
                              <p className="text-[11px] text-gray-400 mt-0.5 leading-relaxed">{String(st.approach)}</p>
                            </div>
                          )}
                          {st.goal && (
                            <div>
                              <span className="text-[10px] text-gray-600 uppercase tracking-wider">Goal</span>
                              <p className="text-[11px] text-emerald-400/70 mt-0.5 leading-relaxed">{String(st.goal)}</p>
                            </div>
                          )}
                          {st.context && (
                            <div>
                              <span className="text-[10px] text-gray-600 uppercase tracking-wider">Context</span>
                              <pre className="text-[11px] text-gray-500 mt-0.5 font-mono whitespace-pre-wrap leading-relaxed bg-gray-950 rounded px-2 py-1.5 border border-gray-800/50 max-h-32 overflow-y-auto">{String(st.context)}</pre>
                            </div>
                          )}
                          {/* Legacy: plain description fallback */}
                          {!st.scope && !st.requirements && st.description && (
                            <p className="text-[11px] text-gray-500 leading-relaxed">{String(st.description)}</p>
                          )}
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        ))}
      </div>

      {/* Action buttons */}
      {!isAccepted && isLatest === false && (
        <div className="px-4 py-2 border-t border-gray-800">
          <span className="text-[11px] text-gray-600">Earlier version — see latest plan below</span>
        </div>
      )}
      {!isAccepted && isLatest !== false && (
        <div className="px-4 py-2.5 border-t border-gray-800">
          {showRejectInput ? (
            <div className="flex items-center gap-2">
              <input
                type="text"
                value={feedback}
                onChange={(e) => setFeedback(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') handleRejectSubmit()
                  if (e.key === 'Escape') setShowRejectInput(false)
                }}
                placeholder="What should change?"
                className="flex-1 px-3 py-1.5 bg-gray-950 border border-gray-800 rounded-lg text-sm text-white focus:outline-none focus:border-amber-500/50 transition-colors"
                autoFocus
              />
              <button
                onClick={handleRejectSubmit}
                disabled={!feedback.trim()}
                className="px-3 py-1.5 bg-gray-800 hover:bg-gray-700 disabled:opacity-40 rounded-lg text-sm text-gray-300 transition-colors"
              >
                Send
              </button>
              <button
                onClick={() => {
                  setShowRejectInput(false)
                  setFeedback('')
                }}
                className="text-xs text-gray-600 hover:text-gray-400 transition-colors"
              >
                Cancel
              </button>
            </div>
          ) : (
            <div className="flex items-center gap-2">
              <button
                onClick={onAccept}
                disabled={disabled}
                className="px-4 py-1.5 bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40 rounded-lg text-sm font-medium text-white transition-colors"
              >
                Accept Plan
              </button>
              <button
                onClick={() => setShowRejectInput(true)}
                disabled={disabled}
                className="px-4 py-1.5 bg-gray-800 hover:bg-gray-700 disabled:opacity-40 rounded-lg text-sm text-gray-400 transition-colors"
              >
                Request Changes
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
