import { useState } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'

const SEVERITY_STYLES: Record<string, { badge: string; text: string }> = {
  critical: { badge: 'bg-red-500/10 border-red-500/20 text-red-400', text: 'text-red-400' },
  major: { badge: 'bg-amber-500/10 border-amber-500/20 text-amber-400', text: 'text-amber-400' },
  minor: { badge: 'bg-gray-800 border-gray-700 text-gray-400', text: 'text-gray-400' },
  nit: { badge: 'bg-gray-800 border-gray-700 text-gray-600', text: 'text-gray-600' },
}

interface ReviewIssue {
  severity: 'critical' | 'major' | 'minor' | 'nit'
  file?: string
  line?: number | null
  description: string
  suggestion?: string
}

interface ReviewFeedbackCardProps {
  type: 'plan' | 'code'
  approved: boolean
  feedback?: string
  summary?: string
  issues?: ReviewIssue[]
  subtaskTitle?: string
  iteration?: number
}

export default function ReviewFeedbackCard({
  type,
  approved,
  feedback,
  summary,
  issues,
  subtaskTitle,
  iteration,
}: ReviewFeedbackCardProps) {
  const [expandedIssues, setExpandedIssues] = useState<Set<number>>(new Set())

  const toggleIssue = (idx: number) => {
    setExpandedIssues(prev => {
      const next = new Set(prev)
      if (next.has(idx)) next.delete(idx); else next.add(idx)
      return next
    })
  }

  const borderColor = approved ? 'border-emerald-500/20' : 'border-amber-500/20'
  const headerBg = approved ? 'bg-emerald-500/5' : 'bg-amber-500/5'
  const iconColor = approved ? 'text-emerald-400' : 'text-amber-400'
  const label = type === 'plan' ? 'Plan Review' : 'Code Review'
  const iterLabel = iteration && iteration > 1 ? ` (revision ${iteration})` : ''

  return (
    <div className={`mt-2 rounded-lg overflow-hidden border ${borderColor} bg-gray-900`}>
      {/* Header */}
      <div className={`px-4 py-2.5 border-b border-gray-800 flex items-center gap-2 ${headerBg}`}>
        <svg
          className={`w-4 h-4 shrink-0 ${iconColor}`}
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth={2}
        >
          {approved ? (
            <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
          ) : (
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z" />
          )}
        </svg>
        <span className={`text-sm font-medium ${iconColor}`}>
          {label}{iterLabel}: {approved ? 'Approved' : 'Changes Requested'}
        </span>
        {subtaskTitle && (
          <span className="text-[11px] text-gray-600 ml-auto truncate max-w-[200px]">
            {subtaskTitle}
          </span>
        )}
      </div>

      {/* Summary / Feedback */}
      {(summary || feedback) && (
        <div className="px-4 py-2.5 border-b border-gray-800/50">
          {summary && <p className="text-xs text-gray-400 leading-relaxed">{summary}</p>}
          {feedback && !summary && (
            <p className="text-xs text-gray-400 leading-relaxed">{feedback}</p>
          )}
          {feedback && summary && feedback !== summary && (
            <p className="text-[11px] text-gray-500 mt-1.5 leading-relaxed">{feedback}</p>
          )}
        </div>
      )}

      {/* Issues list (code review) */}
      {issues && issues.length > 0 && (
        <div className="divide-y divide-gray-800/50">
          {issues.map((issue, idx) => {
            const sev = SEVERITY_STYLES[issue.severity] || SEVERITY_STYLES.major
            const hasDetails = !!issue.suggestion
            const isExpanded = expandedIssues.has(idx)

            return (
              <div key={idx} className="px-4 py-2">
                <div
                  className={`flex items-start gap-2 ${hasDetails ? 'cursor-pointer' : ''}`}
                  onClick={() => hasDetails && toggleIssue(idx)}
                >
                  {hasDetails && (
                    <span className="text-gray-600 mt-0.5 shrink-0">
                      {isExpanded ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
                    </span>
                  )}
                  <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium border shrink-0 ${sev.badge}`}>
                    {issue.severity}
                  </span>
                  {issue.file && (
                    <span className="text-[11px] font-mono text-indigo-400/70 shrink-0">
                      {issue.file}{issue.line ? `:${issue.line}` : ''}
                    </span>
                  )}
                  <span className="text-xs text-gray-400 flex-1">{issue.description}</span>
                </div>
                {isExpanded && issue.suggestion && (
                  <div className="ml-5 mt-1.5 pl-3 border-l border-gray-800">
                    <span className="text-[10px] text-gray-600 uppercase tracking-wider">Suggestion</span>
                    <p className="text-[11px] text-gray-500 mt-0.5 leading-relaxed">{issue.suggestion}</p>
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
