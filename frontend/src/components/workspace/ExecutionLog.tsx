import { useState, useEffect, useRef, useCallback } from 'react'
import { ChevronDown, ChevronRight, Terminal, Wrench, Brain, CheckCircle, XCircle, Play, Pause } from 'lucide-react'

export interface ExecutionEvent {
  type: 'iteration_start' | 'tool_start' | 'tool_result' | 'llm_thinking' | 'iteration_end' | 'activity'
  timestamp: number
  iteration?: number
  subtask?: string
  name?: string
  args_summary?: string
  result_preview?: string
  chars?: number
  tokens_in?: number
  tokens_out?: number
  round?: number
  status?: string
  tool_index?: number
  total_tools?: number
  message?: string
}

interface Props {
  events: ExecutionEvent[]
  maxHeight?: string
}

export default function ExecutionLog({ events, maxHeight = '500px' }: Props) {
  const [expandedEvents, setExpandedEvents] = useState<Set<number>>(new Set())
  const [autoScroll, setAutoScroll] = useState(true)
  const [compact, setCompact] = useState(false)
  const scrollRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (autoScroll && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [events.length, autoScroll])

  const handleScroll = useCallback(() => {
    if (!scrollRef.current) return
    const { scrollTop, scrollHeight, clientHeight } = scrollRef.current
    const isAtBottom = scrollHeight - scrollTop - clientHeight < 40
    setAutoScroll(isAtBottom)
  }, [])

  const toggleExpand = (index: number) => {
    setExpandedEvents(prev => {
      const next = new Set(prev)
      if (next.has(index)) next.delete(index)
      else next.add(index)
      return next
    })
  }

  if (!events.length) {
    return (
      <div className="flex items-center justify-center py-8 text-sm text-gray-600">
        <Terminal className="w-4 h-4 mr-2" />
        Waiting for execution events...
      </div>
    )
  }

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-gray-800 bg-gray-900/50">
        <div className="flex items-center gap-2 text-xs text-gray-400">
          <Terminal className="w-3.5 h-3.5" />
          <span>Execution Log</span>
          <span className="text-gray-600">({events.length} events)</span>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setCompact(!compact)}
            className="px-2 py-0.5 text-[10px] text-gray-500 hover:text-gray-300 bg-gray-800 rounded transition-colors"
          >
            {compact ? 'Expand' : 'Compact'}
          </button>
          <button
            onClick={() => setAutoScroll(!autoScroll)}
            className={`p-1 rounded transition-colors ${autoScroll ? 'text-indigo-400' : 'text-gray-600 hover:text-gray-400'}`}
            title={autoScroll ? 'Auto-scroll on' : 'Auto-scroll off'}
          >
            {autoScroll ? <Play className="w-3 h-3" /> : <Pause className="w-3 h-3" />}
          </button>
        </div>
      </div>

      {/* Event list */}
      <div
        ref={scrollRef}
        onScroll={handleScroll}
        className="flex-1 overflow-y-auto font-mono text-xs"
        style={{ maxHeight }}
      >
        {events.map((event, index) => (
          <EventRow
            key={index}
            event={event}
            index={index}
            compact={compact}
            expanded={expandedEvents.has(index)}
            onToggle={() => toggleExpand(index)}
          />
        ))}
      </div>
    </div>
  )
}


function EventRow({
  event,
  index: _index,
  compact,
  expanded,
  onToggle,
}: {
  event: ExecutionEvent
  index: number
  compact: boolean
  expanded: boolean
  onToggle: () => void
}) {
  const hasDetails = event.type === 'tool_result' || event.type === 'tool_start'

  if (event.type === 'iteration_start') {
    return (
      <div className="flex items-center gap-2 px-3 py-1.5 bg-indigo-500/5 border-b border-gray-800/50 text-indigo-400">
        <Play className="w-3 h-3" />
        <span>Iteration {event.iteration}</span>
        {event.subtask && <span className="text-gray-500">— {event.subtask}</span>}
      </div>
    )
  }

  if (event.type === 'iteration_end') {
    const passed = event.status === 'passed'
    return (
      <div className={`flex items-center gap-2 px-3 py-1.5 border-b border-gray-800/50 ${passed ? 'text-emerald-400 bg-emerald-500/5' : 'text-red-400 bg-red-500/5'}`}>
        {passed ? <CheckCircle className="w-3 h-3" /> : <XCircle className="w-3 h-3" />}
        <span>Iteration {event.iteration} — {event.status}</span>
      </div>
    )
  }

  if (event.type === 'tool_start') {
    if (compact) {
      return (
        <div className="flex items-center gap-2 px-3 py-0.5 text-blue-400 border-b border-gray-900/50">
          <Wrench className="w-3 h-3 flex-shrink-0" />
          <span className="truncate">{event.name}</span>
          {event.total_tools && event.total_tools > 1 && (
            <span className="text-gray-600">({event.tool_index}/{event.total_tools})</span>
          )}
        </div>
      )
    }
    return (
      <div className="border-b border-gray-900/50">
        <div
          className="flex items-center gap-2 px-3 py-1 text-blue-400 cursor-pointer hover:bg-gray-900/30"
          onClick={onToggle}
        >
          {hasDetails ? (
            expanded ? <ChevronDown className="w-3 h-3 flex-shrink-0" /> : <ChevronRight className="w-3 h-3 flex-shrink-0" />
          ) : (
            <Wrench className="w-3 h-3 flex-shrink-0" />
          )}
          <span>{event.name}</span>
          {event.total_tools && event.total_tools > 1 && (
            <span className="text-gray-600">({event.tool_index}/{event.total_tools})</span>
          )}
        </div>
        {expanded && event.args_summary && (
          <div className="px-6 py-1 text-gray-500 whitespace-pre-wrap bg-gray-900/30">
            {event.args_summary}
          </div>
        )}
      </div>
    )
  }

  if (event.type === 'tool_result') {
    if (compact) {
      return (
        <div className="flex items-center gap-2 px-3 py-0.5 text-gray-500 border-b border-gray-900/50">
          <span className="text-gray-600">↳</span>
          <span className="truncate">{event.name}: {event.chars?.toLocaleString()} chars</span>
        </div>
      )
    }
    return (
      <div className="border-b border-gray-900/50">
        <div
          className="flex items-center gap-2 px-3 py-1 text-gray-400 cursor-pointer hover:bg-gray-900/30"
          onClick={onToggle}
        >
          {expanded ? <ChevronDown className="w-3 h-3 flex-shrink-0 text-gray-600" /> : <ChevronRight className="w-3 h-3 flex-shrink-0 text-gray-600" />}
          <span className="text-gray-600">↳</span>
          <span>{event.name} result</span>
          <span className="text-gray-600">({event.chars?.toLocaleString()} chars)</span>
        </div>
        {expanded && event.result_preview && (
          <div className="px-6 py-1 text-gray-500 whitespace-pre-wrap bg-gray-900/30 max-h-40 overflow-y-auto">
            {event.result_preview}
          </div>
        )}
      </div>
    )
  }

  if (event.type === 'llm_thinking') {
    return (
      <div className="flex items-center gap-2 px-3 py-1 text-indigo-400/60 border-b border-gray-900/50">
        <Brain className="w-3 h-3 flex-shrink-0" />
        <span>LLM response</span>
        <span className="text-gray-600">
          (round {event.round}, {event.tokens_in?.toLocaleString()}→{event.tokens_out?.toLocaleString()} tokens)
        </span>
      </div>
    )
  }

  // Activity / generic event
  return (
    <div className="flex items-center gap-2 px-3 py-0.5 text-gray-500 border-b border-gray-900/50">
      <span className="text-gray-700">•</span>
      <span className="truncate">{event.message || JSON.stringify(event)}</span>
    </div>
  )
}
