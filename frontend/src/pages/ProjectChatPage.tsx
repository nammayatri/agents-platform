import { useEffect, useRef, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { projectChat, projects as projectsApi, providers as providersApi, todos as todosApi } from '../services/api'
import PlanReviewCard from '../components/chat/PlanReviewCard'
import { useChatSessionWebSocket } from '../hooks/useChatSessionWebSocket'
import type { Project, ChatSession, ProjectChatMessage, ModelInfo, ChatMode, ChatExecutionInfo } from '../types'

const chatModes: { key: ChatMode; label: string; hint: string }[] = [
  { key: 'auto', label: 'Auto', hint: 'Automatically detects your intent' },
  { key: 'chat', label: 'General Chat', hint: 'Ask questions, discuss the project' },
  { key: 'plan', label: 'Plan', hint: 'Build a structured execution plan' },
  { key: 'debug', label: 'Debug', hint: 'Investigate bugs and issues' },
  { key: 'create_task', label: 'Create Task', hint: 'Quickly create a task from description' },
]

const modeStyles: Record<ChatMode, string> = {
  auto: 'text-blue-400 bg-blue-500/10 border border-blue-500/20',
  chat: 'text-gray-400 hover:text-gray-300 bg-gray-900 border border-gray-800',
  plan: 'text-amber-400 bg-amber-500/10 border border-amber-500/20',
  debug: 'text-red-400 bg-red-500/10 border border-red-500/20',
  create_task: 'text-indigo-400 bg-indigo-500/10 border border-indigo-500/20',
}

const routingModeLabels: Record<string, string> = {
  chat: 'Chat',
  plan: 'Plan',
  debug: 'Debug',
  create_task: 'Task',
}

const routingModeColors: Record<string, string> = {
  chat: 'text-gray-400 bg-gray-800',
  plan: 'text-amber-400 bg-amber-500/10',
  debug: 'text-red-400 bg-red-500/10',
  create_task: 'text-indigo-400 bg-indigo-500/10',
}

const sendButtonStyles: Record<ChatMode, string> = {
  auto: 'bg-blue-600 hover:bg-blue-500 disabled:bg-gray-800 disabled:text-gray-600',
  chat: 'bg-indigo-600 hover:bg-indigo-500 disabled:bg-gray-800 disabled:text-gray-600',
  plan: 'bg-amber-600 hover:bg-amber-500 disabled:bg-gray-800 disabled:text-gray-600',
  debug: 'bg-red-600 hover:bg-red-500 disabled:bg-gray-800 disabled:text-gray-600',
  create_task: 'bg-indigo-600 hover:bg-indigo-500 disabled:bg-gray-800 disabled:text-gray-600',
}

const sessionDotColors: Record<ChatMode, string> = {
  auto: 'bg-blue-500',
  chat: 'bg-gray-600',
  plan: 'bg-amber-500',
  debug: 'bg-red-500',
  create_task: 'bg-indigo-500',
}

/** Ensure metadata_json is always a parsed object (handles double-encoded JSONB strings). */
function parseMeta(msg: ProjectChatMessage): ProjectChatMessage['metadata_json'] {
  const m = msg.metadata_json
  if (!m) return undefined
  if (typeof m === 'string') {
    try { return JSON.parse(m) } catch { return undefined }
  }
  return m
}

function ExecutionDetails({ execution }: { execution: ChatExecutionInfo }) {
  const [expanded, setExpanded] = useState(false)
  const toolCalls = execution.tool_calls || []
  const rounds = execution.rounds || 0
  const uniqueTools = [...new Set(toolCalls.map((t) => t.name))]

  // Collapsed summary line
  const summary = toolCalls.length > 0
    ? `Used ${toolCalls.length} tool${toolCalls.length !== 1 ? 's' : ''} in ${rounds} round${rounds !== 1 ? 's' : ''} (${uniqueTools.slice(0, 4).join(', ')}${uniqueTools.length > 4 ? '...' : ''})`
    : `No tools used`
  const modelLabel = execution.model ? ` \u00b7 ${execution.model}` : ''
  const stopLabel = !toolCalls.length && execution.stop_reason ? ` \u00b7 ${execution.stop_reason}` : ''

  return (
    <div className="mt-1.5 border-t border-gray-800/50 pt-1.5">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-1 text-[11px] text-gray-600 hover:text-gray-400 transition-colors"
      >
        <svg
          className={`w-3 h-3 transition-transform ${expanded ? 'rotate-90' : ''}`}
          fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
        </svg>
        <span>{summary}{modelLabel}{stopLabel}</span>
      </button>
      {expanded && toolCalls.length > 0 && (
        <div className="mt-1.5 space-y-1 pl-4">
          {toolCalls.map((tc, i) => (
            <div key={i} className="flex items-start gap-2 text-[11px]">
              <span className="text-indigo-400/70 font-mono shrink-0">{tc.name}</span>
              {tc.result_preview && (
                <span className="text-gray-600 truncate max-w-[300px]">{tc.result_preview}</span>
              )}
            </div>
          ))}
          <div className="text-[10px] text-gray-700 mt-1">
            {execution.total_tokens_in != null && execution.total_tokens_out != null && (
              <span>Tokens: {execution.total_tokens_in.toLocaleString()} in / {execution.total_tokens_out.toLocaleString()} out</span>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

function RawOutputToggle({ content }: { content: string }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="mt-1.5 border-t border-gray-800/50 pt-1.5">
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-1 text-[11px] text-gray-600 hover:text-gray-400 transition-colors"
      >
        <svg
          className={`w-3 h-3 transition-transform ${open ? 'rotate-90' : ''}`}
          fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
        </svg>
        <span>Tool loop output</span>
      </button>
      {open && (
        <div className="mt-1.5 max-h-80 overflow-y-auto bg-gray-950 border border-gray-800/50 rounded-lg px-3 py-2 text-[11px] text-gray-500 whitespace-pre-wrap font-mono">
          {content}
        </div>
      )}
    </div>
  )
}

export default function ProjectChatPage() {
  const { projectId } = useParams<{ projectId: string }>()
  const navigate = useNavigate()
  const [project, setProject] = useState<Project | null>(null)
  const [sessions, setSessions] = useState<ChatSession[]>([])
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null)
  const [messages, setMessages] = useState<ProjectChatMessage[]>([])
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [showModeDropdown, setShowModeDropdown] = useState(false)
  const [showSidebar, setShowSidebar] = useState(true)
  const [creatingSession, setCreatingSession] = useState(false)
  const messagesEnd = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const { activity, streamingText, clearActivity } = useChatSessionWebSocket(activeSessionId)
  const [showFeedbackFor, setShowFeedbackFor] = useState<string | null>(null)
  const [feedbackInput, setFeedbackInput] = useState('')
  const [chatModels, setChatModels] = useState<ModelInfo[]>([])
  const [selectedModel, setSelectedModel] = useState('')
  const [approvedTaskPlans, setApprovedTaskPlans] = useState<Set<string>>(new Set())
  const [taskPlanFeedbackFor, setTaskPlanFeedbackFor] = useState<string | null>(null)
  const [taskPlanFeedback, setTaskPlanFeedback] = useState('')

  useEffect(() => {
    if (!projectId) return
    projectsApi.get(projectId).then((p) => setProject(p as Project))
    loadSessions()
  }, [projectId])

  // Load available models when project's provider is known
  useEffect(() => {
    if (!project?.ai_provider_id) return
    providersApi.listModels(project.ai_provider_id).then(res => {
      setChatModels(res.models)
      const session = sessions.find(s => s.id === activeSessionId)
      const sessionModel = session?.ai_model
      const defaultModel = res.models.find(m => m.is_default)
      setSelectedModel(sessionModel || defaultModel?.id || '')
    }).catch(() => setChatModels([]))
  }, [project?.ai_provider_id, activeSessionId, sessions])

  useEffect(() => {
    messagesEnd.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, streamingText])

  async function loadSessions() {
    if (!projectId) return
    try {
      const list = await projectChat.sessions.list(projectId)
      const sessionList = list as ChatSession[]
      setSessions(sessionList)

      // Auto-select the most recent session if none is active
      if (sessionList.length > 0 && !activeSessionId) {
        setActiveSessionId(sessionList[0].id)
        loadSessionMessages(sessionList[0].id)
      }
    } catch {
      setSessions([])
    }
  }

  async function loadSessionMessages(sessionId: string) {
    if (!projectId) return
    try {
      const data = await projectChat.sessions.get(projectId, sessionId)
      setMessages(data.messages || [])
    } catch {
      setMessages([])
    }
  }

  async function loadLegacyMessages() {
    if (!projectId) return
    const msgs = await projectChat.history(projectId)
    setMessages(msgs as ProjectChatMessage[])
  }

  function selectSession(sessionId: string | null) {
    setActiveSessionId(sessionId)
    setMessages([])
    if (sessionId) {
      loadSessionMessages(sessionId)
    } else {
      loadLegacyMessages()
    }
  }

  async function createSession() {
    if (!projectId || creatingSession) return
    setCreatingSession(true)
    try {
      const session = (await projectChat.sessions.create(projectId, {})) as ChatSession
      setSessions((prev) => [session, ...prev])
      setActiveSessionId(session.id)
      setMessages([])
    } catch {
      // fallback
    } finally {
      setCreatingSession(false)
    }
  }

  async function handleModeChange(mode: ChatMode) {
    if (!projectId || !activeSessionId) return
    try {
      await projectChat.sessions.setChatMode(projectId, activeSessionId, mode)
      setSessions((prev) =>
        prev.map((s) =>
          s.id === activeSessionId
            ? { ...s, chat_mode: mode, plan_mode: mode === 'plan' }
            : s
        )
      )
    } catch {
      // ignore
    }
    setShowModeDropdown(false)
  }

  async function deleteSession(sessionId: string) {
    if (!projectId) return
    try {
      await projectChat.sessions.delete(projectId, sessionId)
      setSessions((prev) => prev.filter((s) => s.id !== sessionId))
      if (activeSessionId === sessionId) {
        setActiveSessionId(null)
        setMessages([])
      }
    } catch {
      // ignore
    }
  }

  const activeSession = sessions.find((s) => s.id === activeSessionId)
  const chatMode: ChatMode = (activeSession?.chat_mode as ChatMode) || (activeSession?.plan_mode ? 'plan' : 'auto')
  const [lastRoutingMode, setLastRoutingMode] = useState<string>('chat')

  const handleSend = async (overrideContent?: string) => {
    const content = overrideContent || input.trim()
    if (!content || !projectId || sending) return
    if (!overrideContent) setInput('')
    setSending(true)

    const tempUserMsg: ProjectChatMessage = {
      id: `temp-${Date.now()}`,
      project_id: projectId,
      user_id: '',
      role: 'user',
      content,
      created_at: new Date().toISOString(),
    }
    setMessages((prev) => [...prev, tempUserMsg])

    try {
      // Create a session on first message if none exists
      let sessionId = activeSessionId
      if (!sessionId && !creatingSession) {
        setCreatingSession(true)
        try {
          const session = (await projectChat.sessions.create(projectId, {})) as ChatSession
          sessionId = session.id
          setSessions((prev) => [session, ...prev])
          setActiveSessionId(sessionId)
        } finally {
          setCreatingSession(false)
        }
      }
      if (!sessionId) return

      const result = await projectChat.sendInSession(
        projectId,
        sessionId,
        content,
        undefined,
        selectedModel || undefined
      )

      // Optimistic plan_mode update when plan is accepted
      const resMeta = typeof result.assistant_message.metadata_json === 'string'
        ? (() => { try { return JSON.parse(result.assistant_message.metadata_json) } catch { return undefined } })()
        : result.assistant_message.metadata_json
      if (resMeta?.action === 'plan_accepted') {
        setSessions((prev) =>
          prev.map((s) => (s.id === sessionId ? { ...s, plan_mode: false } : s))
        )
      }

      // Track auto-detected routing mode
      if (result.routing_mode) {
        setLastRoutingMode(result.routing_mode)
      }

      // Attach routing_mode to assistant message metadata for display
      const assistantMsg = result.routing_mode
        ? {
            ...result.assistant_message,
            metadata_json: {
              ...(typeof result.assistant_message.metadata_json === 'string'
                ? (() => { try { return JSON.parse(result.assistant_message.metadata_json) } catch { return {} } })()
                : result.assistant_message.metadata_json || {}),
              routing_mode: result.routing_mode,
              mode_auto_switched: result.mode_auto_switched,
            },
          }
        : result.assistant_message

      setMessages((prev) => [
        ...prev.filter((m) => m.id !== tempUserMsg.id),
        result.user_message,
        assistantMsg,
      ])

      // Refresh sessions list to pick up title changes
      loadSessions()
    } catch (err) {
      setMessages((prev) => prev.filter((m) => m.id !== tempUserMsg.id))
      const errorMsg: ProjectChatMessage = {
        id: `error-${Date.now()}`,
        project_id: projectId,
        user_id: '',
        role: 'system',
        content: `Error: ${err instanceof Error ? err.message : 'Failed to send message'}`,
        created_at: new Date().toISOString(),
      }
      setMessages((prev) => [...prev, errorMsg])
    } finally {
      setSending(false)
      setShowFeedbackFor(null)
      setFeedbackInput('')
      clearActivity()
      inputRef.current?.focus()
    }
  }

  const handleAcceptPlan = async () => {
    if (!projectId || !activeSessionId || sending) return
    setSending(true)
    try {
      const result = await projectChat.sessions.acceptPlan(projectId, activeSessionId)
      setMessages((prev) => [...prev, result.user_message, result.assistant_message])
      // Update session plan_mode
      setSessions((prev) =>
        prev.map((s) => (s.id === activeSessionId ? { ...s, plan_mode: false } : s))
      )
      loadSessions()
    } catch (err) {
      const errorMsg: ProjectChatMessage = {
        id: `error-${Date.now()}`,
        project_id: projectId,
        user_id: '',
        role: 'system',
        content: `Error: ${err instanceof Error ? err.message : 'Failed to accept plan'}`,
        created_at: new Date().toISOString(),
      }
      setMessages((prev) => [...prev, errorMsg])
    } finally {
      setSending(false)
    }
  }

  const handleApproveTaskPlan = async (taskId: string) => {
    if (sending) return
    setSending(true)
    try {
      await todosApi.approvePlan(taskId)
      setApprovedTaskPlans((prev) => new Set(prev).add(taskId))
      const confirmMsg: ProjectChatMessage = {
        id: `plan-approved-${Date.now()}`,
        project_id: projectId!,
        user_id: '',
        role: 'system',
        content: 'Plan approved. Starting execution.',
        created_at: new Date().toISOString(),
      }
      setMessages((prev) => [...prev, confirmMsg])
    } catch (err) {
      const errorMsg: ProjectChatMessage = {
        id: `error-${Date.now()}`,
        project_id: projectId!,
        user_id: '',
        role: 'system',
        content: `Error: ${err instanceof Error ? err.message : 'Failed to approve plan'}`,
        created_at: new Date().toISOString(),
      }
      setMessages((prev) => [...prev, errorMsg])
    } finally {
      setSending(false)
    }
  }

  const handleRejectTaskPlan = async (taskId: string, feedback: string) => {
    if (sending || !feedback.trim()) return
    setSending(true)
    try {
      await todosApi.rejectPlan(taskId, feedback.trim())
      const confirmMsg: ProjectChatMessage = {
        id: `plan-rejected-${Date.now()}`,
        project_id: projectId!,
        user_id: '',
        role: 'system',
        content: `Plan rejected with feedback: "${feedback.trim()}". Re-planning...`,
        created_at: new Date().toISOString(),
      }
      setMessages((prev) => [...prev, confirmMsg])
    } catch (err) {
      const errorMsg: ProjectChatMessage = {
        id: `error-${Date.now()}`,
        project_id: projectId!,
        user_id: '',
        role: 'system',
        content: `Error: ${err instanceof Error ? err.message : 'Failed to reject plan'}`,
        created_at: new Date().toISOString(),
      }
      setMessages((prev) => [...prev, errorMsg])
    } finally {
      setSending(false)
      setTaskPlanFeedbackFor(null)
      setTaskPlanFeedback('')
    }
  }

  const handleInject = async () => {
    if (!input.trim() || !projectId || !activeSessionId) return
    const content = input.trim()
    setInput('')
    try {
      await projectChat.injectInSession(projectId, activeSessionId, content)
      // Show injected message in the chat as a visual indicator
      const injectMsg: ProjectChatMessage = {
        id: `inject-${Date.now()}`,
        project_id: projectId,
        user_id: '',
        role: 'user',
        content: `[Injected] ${content}`,
        created_at: new Date().toISOString(),
      }
      setMessages((prev) => [...prev, injectMsg])
    } catch {
      // ignore
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      if (sending) {
        handleInject()
      } else {
        handleSend()
      }
    }
  }

  const handleClear = async () => {
    if (!projectId) return
    if (!confirm('Clear chat history?')) return
    if (activeSessionId) {
      await deleteSession(activeSessionId)
    } else {
      await projectChat.clear(projectId)
      setMessages([])
    }
  }

  const handleDeleteMessage = async (msgId: string) => {
    if (!projectId || !confirm('Remove this message?')) return
    try {
      if (activeSessionId) {
        await projectChat.deleteSessionMessage(projectId, activeSessionId, msgId)
      } else {
        await projectChat.deleteMessage(projectId, msgId)
      }
      setMessages((prev) => prev.filter((m) => m.id !== msgId))
    } catch (err) {
      console.error('Failed to remove:', err)
    }
  }

  if (!projectId || !project) {
    return (
      <div className="flex items-center justify-center h-full text-gray-600 text-sm">
        Loading...
      </div>
    )
  }

  const hasMessages = messages.length > 0

  return (
    <div className="flex h-full">
      {/* Session sidebar — overlay on mobile, inline on desktop */}
      {showSidebar && (
        <>
          {/* Mobile backdrop */}
          <div
            className="fixed inset-0 z-20 bg-black/50 md:hidden"
            onClick={() => setShowSidebar(false)}
          />
          <div className="fixed inset-y-0 left-0 z-30 w-56 border-r border-gray-900 flex flex-col shrink-0 bg-gray-950 md:relative md:z-auto">
            <div className="p-3">
              <button
                onClick={() => createSession()}
                className="w-full flex items-center gap-2 px-3 py-2 rounded-lg text-xs text-gray-400 hover:text-white hover:bg-gray-900 transition-colors"
              >
                <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 4.5v15m7.5-7.5h-15" />
                </svg>
                New Chat
              </button>
            </div>

            <div className="flex-1 overflow-y-auto px-2 pb-2 space-y-0.5">
              {sessions.map((s) => (
                <div
                  key={s.id}
                  className={`group flex items-center rounded-lg transition-colors ${
                    activeSessionId === s.id ? 'bg-gray-900' : 'hover:bg-gray-900/50'
                  }`}
                >
                  <button
                    onClick={() => {
                      selectSession(s.id)
                      // Close sidebar on mobile after selecting
                      if (window.innerWidth < 768) setShowSidebar(false)
                    }}
                    className="flex-1 flex items-center gap-2 px-3 py-2 text-left min-w-0"
                  >
                    <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${sessionDotColors[(s.chat_mode as ChatMode) || (s.plan_mode ? 'plan' : 'chat')]}`} />
                    <span
                      className={`text-xs truncate ${
                        activeSessionId === s.id ? 'text-white' : 'text-gray-500'
                      }`}
                    >
                      {s.title}
                    </span>
                  </button>
                  <button
                    onClick={(e) => {
                      e.stopPropagation()
                      deleteSession(s.id)
                    }}
                    className="block md:hidden md:group-hover:block px-2 text-gray-700 hover:text-red-400 transition-colors"
                  >
                    <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                    </svg>
                  </button>
                </div>
              ))}
            </div>
          </div>
        </>
      )}

      {/* Main chat area */}
      <div className="flex-1 flex flex-col overflow-hidden">
        {/* Header */}
        <div className="flex items-center justify-between px-4 md:px-6 py-3 border-b border-gray-900">
          <div className="flex items-center gap-3">
            <button
              onClick={() => setShowSidebar(!showSidebar)}
              className="text-gray-600 hover:text-gray-400 transition-colors"
            >
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6.75h16.5M3.75 12h16.5m-16.5 5.25h16.5" />
              </svg>
            </button>
            <button
              onClick={() => navigate(`/projects/${projectId}`)}
              className="text-gray-500 hover:text-gray-300 transition-colors"
            >
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M10.5 19.5L3 12m0 0l7.5-7.5M3 12h18" />
              </svg>
            </button>
            <div>
              <div className="flex items-center gap-2">
                <h1 className="text-sm font-medium text-white">{project.name}</h1>
                {chatMode !== 'chat' && (
                  <span className={`px-1.5 py-0.5 rounded text-[10px] ${
                    chatMode === 'auto' ? 'bg-blue-500/10 text-blue-400'
                    : chatMode === 'plan' ? 'bg-amber-500/10 text-amber-400'
                    : chatMode === 'debug' ? 'bg-red-500/10 text-red-400'
                    : 'bg-indigo-500/10 text-indigo-400'
                  }`}>
                    {chatMode === 'auto'
                      ? `Auto${lastRoutingMode !== 'chat' ? ` \u2192 ${routingModeLabels[lastRoutingMode] || lastRoutingMode}` : ''}`
                      : chatModes.find(m => m.key === chatMode)?.label}
                  </span>
                )}
              </div>
              <p className="text-[11px] text-gray-600">
                {activeSession ? activeSession.title : 'Chat with AI about this project'}
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            {chatModels.length > 0 && (
              <select
                value={selectedModel}
                onChange={(e) => setSelectedModel(e.target.value)}
                className="px-2 py-1 bg-gray-950 border border-gray-800 rounded-lg text-[11px] text-gray-400 focus:outline-none focus:border-indigo-500 transition-colors max-w-[120px] md:max-w-[180px]"
              >
                {chatModels.map(m => (
                  <option key={m.id} value={m.id}>
                    {m.name}{m.is_default ? ' (default)' : ''}
                  </option>
                ))}
              </select>
            )}
            {hasMessages && (
              <button
                onClick={handleClear}
                className="text-xs text-gray-600 hover:text-gray-400 transition-colors"
              >
                Clear
              </button>
            )}
          </div>
        </div>

        {/* Messages */}
        <div className="flex-1 overflow-y-auto">
          {!hasMessages ? (
            <div className="flex flex-col items-center justify-center h-full px-6">
              <div className="max-w-md text-center mb-8">
                <h2 className="text-lg font-medium text-white mb-2">
                  {chatMode === 'auto' ? 'What would you like to do?'
                    : chatMode === 'plan' ? 'Plan your project'
                    : chatMode === 'debug' ? 'Debug an issue'
                    : chatMode === 'create_task' ? 'Create a task'
                    : 'What would you like to do?'}
                </h2>
                <p className="text-sm text-gray-500">
                  {chatMode === 'auto'
                    ? `Ask anything — I'll automatically detect whether to chat, plan, debug, or create tasks for ${project.name}.`
                    : chatMode === 'plan'
                    ? 'Discuss scope and requirements. When ready, I\'ll generate a structured plan with tasks and subtasks.'
                    : chatMode === 'debug'
                    ? 'Describe the bug or issue and I\'ll help investigate using code search, logs, and available tools.'
                    : chatMode === 'create_task'
                    ? 'Describe what you want done and I\'ll create a task with the right structure and subtasks.'
                    : `Chat with AI to create tasks, ask questions, debug issues, or talk about ${project.name}.`}
                </p>
              </div>

              <div className="space-y-2 w-full max-w-md">
                {(chatMode === 'auto' ? [
                  'I want to build a new feature — let\'s plan it out',
                  'There\'s a bug in the login flow — help me debug it',
                  'What does this project do?',
                ] : chatMode === 'plan' ? [
                  'I want to build a new feature — let\'s plan it out',
                  'Help me plan the next sprint for this project',
                  'I need to refactor the authentication module',
                ] : chatMode === 'debug' ? [
                  'Users are reporting 500 errors on the login page',
                  'The API response time has degraded — help me find why',
                  'Tests are failing on CI but passing locally',
                ] : chatMode === 'create_task' ? [
                  'Add dark mode support to the frontend',
                  'Write tests for the authentication module',
                  'Set up CI/CD pipeline with GitHub Actions',
                ] : [
                  'What does this project do?',
                  'Show me the recent open tasks',
                  'Help me understand the codebase structure',
                ]).map((suggestion) => (
                  <button
                    key={suggestion}
                    onClick={() => {
                      setInput(suggestion)
                      inputRef.current?.focus()
                    }}
                    className="w-full text-left px-4 py-3 bg-gray-900 border border-gray-800 rounded-xl text-xs text-gray-400 hover:text-gray-300 hover:border-gray-700 transition-colors"
                  >
                    {suggestion}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <div className="px-6 py-4 space-y-4 max-w-3xl mx-auto">
              {messages.map((msg) => {
                const meta = parseMeta(msg)
                return (
                <div
                  key={msg.id}
                  className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}
                >
                  <div
                    className={`max-w-[80%] rounded-xl px-4 py-2.5 text-sm ${
                      msg.role === 'user'
                        ? 'bg-indigo-600 text-white'
                        : msg.role === 'system'
                          ? 'bg-red-900/30 text-red-400 border border-red-800/50'
                          : 'bg-gray-900 border border-gray-800 text-gray-300'
                    }`}
                  >
                    <div className={`text-[10px] mb-1 font-mono ${msg.role === 'user' ? 'text-indigo-300/50' : 'text-gray-600'}`}>
                      {new Date(msg.created_at).toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' })}
                    </div>
                    {msg.role === 'assistant' ? (
                      <div className="space-y-2">
                        {chatMode === 'auto' && meta?.routing_mode && (
                          <span className={`inline-block px-1.5 py-0.5 rounded text-[10px] font-medium mb-1 ${routingModeColors[meta.routing_mode] || 'text-gray-400 bg-gray-800'} ${meta.mode_auto_switched ? 'animate-pulse' : ''}`}>
                            {routingModeLabels[meta.routing_mode] || meta.routing_mode}
                          </span>
                        )}
                        <RenderMarkdown content={msg.content} />
                        {/* Task created badge */}
                        {meta?.action === 'task_created' && meta.task_id && (
                          <div className="flex items-center gap-2 mt-1">
                            <button
                              onClick={() => navigate(`/todos/${meta!.task_id}`)}
                              className="inline-flex items-center gap-1.5 px-2.5 py-1 bg-indigo-900/30 text-indigo-400 rounded-lg text-xs hover:bg-indigo-900/50 transition-colors"
                            >
                              <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                                <path strokeLinecap="round" strokeLinejoin="round" d="M13.5 6H5.25A2.25 2.25 0 003 8.25v10.5A2.25 2.25 0 005.25 21h10.5A2.25 2.25 0 0018 18.75V10.5m-10.5 6L21 3m0 0h-5.25M21 3v5.25" />
                              </svg>
                              View task
                            </button>
                            <button
                              onClick={() => handleDeleteMessage(msg.id)}
                              className="inline-flex items-center gap-1 px-2 py-1 text-red-400/60 rounded-lg text-xs hover:text-red-400 hover:bg-red-900/20 transition-colors"
                            >
                              <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                                <path strokeLinecap="round" strokeLinejoin="round" d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0" />
                              </svg>
                              Remove
                            </button>
                          </div>
                        )}
                        {/* Plan review card */}
                        {meta?.action === 'plan_proposed' && meta.plan_data && (
                          <PlanReviewCard
                            planData={meta.plan_data}
                            isAccepted={messages.some(
                              (m) =>
                                parseMeta(m)?.action === 'plan_accepted' &&
                                m.created_at > msg.created_at
                            )}
                            onAccept={handleAcceptPlan}
                            onReject={(fb) => handleSend(fb)}
                            disabled={sending}
                          />
                        )}
                        {/* Fallback: plan_proposed without plan_data (legacy) */}
                        {meta?.action === 'plan_proposed' && !meta.plan_data && (
                          <div className="flex items-center gap-2 px-3 py-1.5 bg-amber-500/5 border border-amber-500/20 rounded-lg mt-1">
                            <svg className="w-3.5 h-3.5 text-amber-400 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                              <path strokeLinecap="round" strokeLinejoin="round" d="M9 12h3.75M9 15h3.75M9 18h3.75m3 .75H18a2.25 2.25 0 002.25-2.25V6.108c0-1.135-.845-2.098-1.976-2.192a48.424 48.424 0 00-1.123-.08m-5.801 0c-.065.21-.1.433-.1.664 0 .414.336.75.75.75h4.5a.75.75 0 00.75-.75 2.25 2.25 0 00-.1-.664m-5.8 0A2.251 2.251 0 0113.5 2.25H15c1.012 0 1.867.668 2.15 1.586m-5.8 0c-.376.023-.75.05-1.124.08C9.095 4.01 8.25 4.973 8.25 6.108V8.25m0 0H4.875c-.621 0-1.125.504-1.125 1.125v11.25c0 .621.504 1.125 1.125 1.125h9.75c.621 0 1.125-.504 1.125-1.125V9.375c0-.621-.504-1.125-1.125-1.125H8.25z" />
                            </svg>
                            <span className="text-xs text-amber-400">
                              Plan proposed — say "looks good" to accept
                            </span>
                          </div>
                        )}
                        {/* Plan accepted badge */}
                        {meta?.action === 'plan_accepted' && (
                          <div className="flex items-center gap-2 px-3 py-1.5 bg-emerald-500/5 border border-emerald-500/20 rounded-lg mt-1">
                            <svg className="w-3.5 h-3.5 text-emerald-400 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                              <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
                            </svg>
                            <span className="text-xs text-emerald-400">
                              Plan accepted — {String(meta?.tasks_created ?? 0)} tasks created
                            </span>
                          </div>
                        )}
                        {/* Task plan ready — orchestrator generated plan, needs user approval */}
                        {meta?.action === 'task_plan_ready' && meta.task_id && !approvedTaskPlans.has(meta.task_id) && (
                          <div className="mt-2 bg-gray-950 border border-cyan-500/20 rounded-lg overflow-hidden">
                            <div className="px-3 py-2 border-b border-gray-800/50 flex items-center gap-2">
                              <svg className="w-3.5 h-3.5 text-cyan-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                                <path strokeLinecap="round" strokeLinejoin="round" d="M9 12h3.75M9 15h3.75M9 18h3.75m3 .75H18a2.25 2.25 0 002.25-2.25V6.108c0-1.135-.845-2.098-1.976-2.192a48.424 48.424 0 00-1.123-.08m-5.801 0c-.065.21-.1.433-.1.664 0 .414.336.75.75.75h4.5a.75.75 0 00.75-.75 2.25 2.25 0 00-.1-.664m-5.8 0A2.251 2.251 0 0113.5 2.25H15c1.012 0 1.867.668 2.15 1.586m-5.8 0c-.376.023-.75.05-1.124.08C9.095 4.01 8.25 4.973 8.25 6.108V8.25m0 0H4.875c-.621 0-1.125.504-1.125 1.125v11.25c0 .621.504 1.125 1.125 1.125h9.75c.621 0 1.125-.504 1.125-1.125V9.375c0-.621-.504-1.125-1.125-1.125H8.25z" />
                              </svg>
                              <span className="text-xs font-medium text-cyan-400">Execution Plan</span>
                              <span className="text-[10px] text-gray-600">
                                {meta.plan_data?.sub_tasks?.length ?? 0} sub-tasks
                              </span>
                            </div>
                            {meta.plan_data?.sub_tasks && (
                              <div className="px-3 py-2 space-y-1">
                                {meta.plan_data.sub_tasks.map((st: { title: string; agent_role: string; description?: string }, i: number) => (
                                  <div key={i} className="flex items-start gap-2 text-xs">
                                    <span className="px-1.5 py-0.5 bg-gray-800 rounded text-[10px] text-gray-500 font-mono shrink-0">
                                      {st.agent_role}
                                    </span>
                                    <span className="text-gray-400">{st.title}</span>
                                  </div>
                                ))}
                              </div>
                            )}
                            <div className="px-3 py-2 border-t border-gray-800/50">
                              {taskPlanFeedbackFor === meta.task_id ? (
                                <div className="flex items-center gap-2">
                                  <input
                                    type="text"
                                    value={taskPlanFeedback}
                                    onChange={(e) => setTaskPlanFeedback(e.target.value)}
                                    onKeyDown={(e) => {
                                      if (e.key === 'Enter' && taskPlanFeedback.trim()) {
                                        handleRejectTaskPlan(meta.task_id!, taskPlanFeedback)
                                      }
                                      if (e.key === 'Escape') {
                                        setTaskPlanFeedbackFor(null)
                                        setTaskPlanFeedback('')
                                      }
                                    }}
                                    placeholder="What should change?"
                                    className="flex-1 px-3 py-1.5 bg-gray-900 border border-gray-800 rounded-lg text-xs text-white focus:outline-none focus:border-cyan-500/50 transition-colors"
                                    autoFocus
                                  />
                                  <button
                                    onClick={() => handleRejectTaskPlan(meta.task_id!, taskPlanFeedback)}
                                    disabled={!taskPlanFeedback.trim() || sending}
                                    className="px-2.5 py-1.5 bg-gray-800 hover:bg-gray-700 disabled:opacity-40 rounded-lg text-xs text-gray-300 transition-colors"
                                  >
                                    Send
                                  </button>
                                  <button
                                    onClick={() => { setTaskPlanFeedbackFor(null); setTaskPlanFeedback('') }}
                                    className="text-[11px] text-gray-600 hover:text-gray-400 transition-colors"
                                  >
                                    Cancel
                                  </button>
                                </div>
                              ) : (
                                <div className="flex items-center gap-2">
                                  <button
                                    onClick={() => handleApproveTaskPlan(meta.task_id!)}
                                    disabled={sending}
                                    className="px-3 py-1.5 bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 rounded-lg text-xs font-medium text-white transition-colors"
                                  >
                                    Approve Plan
                                  </button>
                                  <button
                                    onClick={() => setTaskPlanFeedbackFor(meta.task_id!)}
                                    disabled={sending}
                                    className="px-3 py-1.5 bg-gray-800 hover:bg-gray-700 disabled:opacity-50 rounded-lg text-xs text-gray-400 transition-colors"
                                  >
                                    Reject with Feedback
                                  </button>
                                </div>
                              )}
                            </div>
                          </div>
                        )}
                        {/* Task plan approved badge */}
                        {meta?.action === 'task_plan_ready' && meta.task_id && approvedTaskPlans.has(meta.task_id) && (
                          <div className="flex items-center gap-2 px-3 py-1.5 bg-emerald-500/5 border border-emerald-500/20 rounded-lg mt-1">
                            <svg className="w-3.5 h-3.5 text-emerald-400 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                              <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
                            </svg>
                            <span className="text-xs text-emerald-400">Plan approved — executing</span>
                          </div>
                        )}
                        {/* Quick-action buttons: only show when there's a pending plan_proposed that hasn't been accepted */}
                        {msg.id === messages.filter((m) => m.role === 'assistant').slice(-1)[0]?.id &&
                          !meta?.action &&
                          !sending &&
                          messages.some((m) => parseMeta(m)?.action === 'plan_proposed') &&
                          !messages.some((m) => parseMeta(m)?.action === 'plan_accepted') && (
                          <div className="mt-2 pt-2 border-t border-gray-800">
                            {showFeedbackFor === msg.id ? (
                              <div className="flex items-center gap-2">
                                <input
                                  type="text"
                                  value={feedbackInput}
                                  onChange={(e) => setFeedbackInput(e.target.value)}
                                  onKeyDown={(e) => {
                                    if (e.key === 'Enter' && feedbackInput.trim()) {
                                      handleSend(feedbackInput.trim())
                                      setFeedbackInput('')
                                      setShowFeedbackFor(null)
                                    }
                                    if (e.key === 'Escape') {
                                      setShowFeedbackFor(null)
                                      setFeedbackInput('')
                                    }
                                  }}
                                  placeholder="What should change?"
                                  className="flex-1 px-3 py-1.5 bg-gray-950 border border-gray-800 rounded-lg text-xs text-white focus:outline-none focus:border-indigo-500/50 transition-colors"
                                  autoFocus
                                />
                                <button
                                  onClick={() => {
                                    if (feedbackInput.trim()) {
                                      handleSend(feedbackInput.trim())
                                      setFeedbackInput('')
                                      setShowFeedbackFor(null)
                                    }
                                  }}
                                  disabled={!feedbackInput.trim()}
                                  className="px-2.5 py-1.5 bg-gray-800 hover:bg-gray-700 disabled:opacity-40 rounded-lg text-xs text-gray-300 transition-colors"
                                >
                                  Send
                                </button>
                                <button
                                  onClick={() => {
                                    setShowFeedbackFor(null)
                                    setFeedbackInput('')
                                  }}
                                  className="text-[11px] text-gray-600 hover:text-gray-400 transition-colors"
                                >
                                  Cancel
                                </button>
                              </div>
                            ) : (
                              <div className="flex items-center gap-2">
                                <button
                                  onClick={handleAcceptPlan}
                                  className="px-3 py-1.5 bg-indigo-600 hover:bg-indigo-500 rounded-lg text-xs font-medium text-white transition-colors"
                                >
                                  Create Tasks
                                </button>
                                <button
                                  onClick={() => setShowFeedbackFor(msg.id)}
                                  className="px-3 py-1.5 bg-gray-800 hover:bg-gray-700 rounded-lg text-xs text-gray-400 transition-colors"
                                >
                                  Suggest Changes
                                </button>
                              </div>
                            )}
                          </div>
                        )}
                        {/* Execution details (tool usage, model, rounds) */}
                        {meta?.execution && (
                          <ExecutionDetails execution={meta.execution} />
                        )}
                        {/* Raw tool loop output (collapsible) */}
                        {meta?.raw_output && (
                          <RawOutputToggle content={meta.raw_output} />
                        )}
                      </div>
                    ) : (
                      <div className="whitespace-pre-wrap">{msg.content}</div>
                    )}
                  </div>
                </div>
              )})}
              {sending && (
                <div className="flex justify-start">
                  <div className={`bg-gray-900 border border-gray-800 rounded-xl px-4 py-2.5 text-sm ${streamingText ? 'max-w-[80%]' : ''}`}>
                    {streamingText ? (
                      <div className="text-gray-300 whitespace-pre-wrap max-h-60 overflow-y-auto">
                        {streamingText}
                        <span className="inline-block w-1.5 h-4 bg-indigo-400 animate-pulse ml-0.5 align-text-bottom rounded-sm" />
                      </div>
                    ) : activity ? (
                      <span className="inline-flex items-center gap-2 text-gray-500">
                        <svg className="w-3 h-3 animate-spin text-indigo-400" fill="none" viewBox="0 0 24 24">
                          <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                          <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                        </svg>
                        <span className="text-gray-400">{activity}</span>
                      </span>
                    ) : (
                      <span className="inline-flex gap-1 text-gray-500">
                        <span className="animate-pulse">Thinking</span>
                        <span className="animate-bounce" style={{ animationDelay: '0.1s' }}>.</span>
                        <span className="animate-bounce" style={{ animationDelay: '0.2s' }}>.</span>
                        <span className="animate-bounce" style={{ animationDelay: '0.3s' }}>.</span>
                      </span>
                    )}
                  </div>
                </div>
              )}
              <div ref={messagesEnd} />
            </div>
          )}
        </div>

        {/* Input area */}
        <div className="px-4 md:px-6 py-4 border-t border-gray-900">
          <div className="max-w-3xl mx-auto flex items-end gap-2">
            {/* Mode dropdown */}
            <div className="relative shrink-0">
              <button
                onClick={() => setShowModeDropdown(!showModeDropdown)}
                className={`flex items-center gap-1.5 px-2.5 py-2.5 rounded-xl text-xs font-medium whitespace-nowrap transition-colors ${modeStyles[chatMode]}`}
                title="Switch chat mode"
              >
                <ModeIcon mode={chatMode} />
                <span className="hidden sm:inline">{chatModes.find(m => m.key === chatMode)?.label}</span>
                <svg className="w-3 h-3 opacity-50" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 8.25l-7.5 7.5-7.5-7.5" />
                </svg>
              </button>
              {showModeDropdown && (
                <>
                  <div
                    className="fixed inset-0 z-10"
                    onClick={() => setShowModeDropdown(false)}
                  />
                  <div className="absolute bottom-full left-0 mb-2 w-52 bg-gray-900 border border-gray-800 rounded-xl shadow-xl overflow-hidden z-20">
                    {chatModes.map((m) => (
                      <button
                        key={m.key}
                        onClick={() => handleModeChange(m.key)}
                        className={`w-full flex items-center gap-3 px-3 py-2.5 text-left text-xs transition-colors ${
                          chatMode === m.key
                            ? 'bg-gray-800 text-white'
                            : 'text-gray-400 hover:bg-gray-800/50 hover:text-gray-300'
                        }`}
                      >
                        <ModeIcon mode={m.key} />
                        <div>
                          <div className="font-medium">{m.label}</div>
                          <div className="text-[10px] text-gray-600">{m.hint}</div>
                        </div>
                      </button>
                    ))}
                  </div>
                </>
              )}
            </div>
            <div className="flex-1 min-w-0">
              <textarea
                ref={inputRef}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={handleKeyDown}
                placeholder={
                  sending ? 'Inject guidance into the running agent...'
                  : chatMode === 'auto' ? 'Ask anything — intent auto-detected...'
                  : chatMode === 'plan' ? 'Describe what you want to plan...'
                  : chatMode === 'debug' ? 'Describe the bug or issue...'
                  : chatMode === 'create_task' ? 'Describe the task to create...'
                  : 'Type a message...'
                }
                rows={1}
                className="w-full px-4 py-2.5 bg-gray-900 border border-gray-800 rounded-xl text-sm text-white placeholder-gray-600 resize-none focus:outline-none focus:border-indigo-500 transition-colors"
                style={{ maxHeight: '120px' }}
                onInput={(e) => {
                  const el = e.target as HTMLTextAreaElement
                  el.style.height = 'auto'
                  el.style.height = Math.min(el.scrollHeight, 120) + 'px'
                }}
              />
            </div>
            {sending ? (
              <button
                onClick={handleInject}
                disabled={!input.trim()}
                className="shrink-0 px-3 py-2.5 rounded-xl text-sm text-white transition-colors bg-cyan-600 hover:bg-cyan-500 disabled:bg-gray-800 disabled:text-gray-600"
                title="Inject guidance into the running agent"
              >
                <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 4.5v15m7.5-7.5h-15" />
                </svg>
              </button>
            ) : (
              <button
                onClick={() => handleSend()}
                disabled={!input.trim() || sending}
                className={`shrink-0 px-3 py-2.5 rounded-xl text-sm text-white transition-colors ${sendButtonStyles[chatMode]}`}
              >
                <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    d="M6 12L3.269 3.126A59.768 59.768 0 0121.485 12 59.77 59.77 0 013.27 20.876L5.999 12zm0 0h7.5"
                  />
                </svg>
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

/** Mode icon for the dropdown */
function ModeIcon({ mode }: { mode: ChatMode }) {
  const paths: Record<ChatMode, string> = {
    auto: 'M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09zM18.259 8.715L18 9.75l-.259-1.035a3.375 3.375 0 00-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 002.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 002.455 2.456L21.75 6l-1.036.259a3.375 3.375 0 00-2.455 2.456z',
    chat: 'M7.5 8.25h9m-9 3H12m-9.75 1.51c0 1.6 1.123 2.994 2.707 3.227 1.129.166 2.27.293 3.423.379.35.026.67.21.865.501L12 21l2.755-4.133a1.14 1.14 0 01.865-.501 48.172 48.172 0 003.423-.379c1.584-.233 2.707-1.626 2.707-3.228V6.741c0-1.602-1.123-2.995-2.707-3.228A48.394 48.394 0 0012 3c-2.392 0-4.744.175-7.043.513C3.373 3.746 2.25 5.14 2.25 6.741v6.018z',
    plan: 'M9 12h3.75M9 15h3.75M9 18h3.75m3 .75H18a2.25 2.25 0 002.25-2.25V6.108c0-1.135-.845-2.098-1.976-2.192a48.424 48.424 0 00-1.123-.08m-5.801 0c-.065.21-.1.433-.1.664 0 .414.336.75.75.75h4.5a.75.75 0 00.75-.75 2.25 2.25 0 00-.1-.664m-5.8 0A2.251 2.251 0 0113.5 2.25H15c1.012 0 1.867.668 2.15 1.586m-5.8 0c-.376.023-.75.05-1.124.08C9.095 4.01 8.25 4.973 8.25 6.108V8.25m0 0H4.875c-.621 0-1.125.504-1.125 1.125v11.25c0 .621.504 1.125 1.125 1.125h9.75c.621 0 1.125-.504 1.125-1.125V9.375c0-.621-.504-1.125-1.125-1.125H8.25z',
    debug: 'M12 12.75c1.148 0 2.278.08 3.383.237 1.037.146 1.866.966 1.866 2.013 0 3.728-2.35 6.75-5.25 6.75S6.75 18.728 6.75 15c0-1.046.83-1.867 1.866-2.013A24.204 24.204 0 0112 12.75zm0 0c2.883 0 5.647.508 8.207 1.44a23.91 23.91 0 01-1.152-6.135c-.078-.759-.633-1.38-1.398-1.43A22.38 22.38 0 0012 6.375c-1.94 0-3.84.158-5.657.46-.764.05-1.32.671-1.398 1.43a23.91 23.91 0 01-1.152 6.135A24.084 24.084 0 0112 12.75zM9.75 8.625a.375.375 0 11-.75 0 .375.375 0 01.75 0zm4.5 0a.375.375 0 11-.75 0 .375.375 0 01.75 0z',
    create_task: 'M12 4.5v15m7.5-7.5h-15',
  }
  return (
    <svg className="w-3.5 h-3.5 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
      <path strokeLinecap="round" strokeLinejoin="round" d={paths[mode]} />
    </svg>
  )
}

/** Simple markdown-ish renderer for assistant messages */
function RenderMarkdown({ content }: { content: string }) {
  const parts = content.split(/(\*\*.*?\*\*)/g)

  return (
    <div className="whitespace-pre-wrap leading-relaxed">
      {parts.map((part, i) => {
        if (part.startsWith('**') && part.endsWith('**')) {
          return (
            <strong key={i} className="font-semibold text-white">
              {part.slice(2, -2)}
            </strong>
          )
        }
        return <span key={i}>{part}</span>
      })}
    </div>
  )
}
