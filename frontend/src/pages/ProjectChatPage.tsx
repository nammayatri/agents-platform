import { useEffect, useRef, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { projectChat, projects as projectsApi } from '../services/api'
import PlanReviewCard from '../components/chat/PlanReviewCard'
import { useChatSessionWebSocket } from '../hooks/useChatSessionWebSocket'
import type { Project, ChatSession, ProjectChatMessage } from '../types'

const intents = [
  { key: 'create_task', label: 'Create a task', icon: 'M12 4.5v15m7.5-7.5h-15' },
  { key: 'ask', label: 'Ask about project', icon: 'M9.879 7.519c1.171-1.025 3.071-1.025 4.242 0 1.172 1.025 1.172 2.687 0 3.712-.203.179-.43.326-.67.442-.745.361-1.45.999-1.45 1.827v.75M21 12a9 9 0 11-18 0 9 9 0 0118 0zm-9 5.25h.008v.008H12v-.008z' },
  { key: 'debug', label: 'Debug an issue', icon: 'M12 12.75c1.148 0 2.278.08 3.383.237 1.037.146 1.866.966 1.866 2.013 0 3.728-2.35 6.75-5.25 6.75S6.75 18.728 6.75 15c0-1.046.83-1.867 1.866-2.013A24.204 24.204 0 0112 12.75zm0 0c2.883 0 5.647.508 8.207 1.44a23.91 23.91 0 01-1.152-6.135c-.078-.759-.633-1.38-1.398-1.43A22.38 22.38 0 0012 6.375c-1.94 0-3.84.158-5.657.46-.764.05-1.32.671-1.398 1.43a23.91 23.91 0 01-1.152 6.135A24.084 24.084 0 0112 12.75zM9.75 8.625a.375.375 0 11-.75 0 .375.375 0 01.75 0zm4.5 0a.375.375 0 11-.75 0 .375.375 0 01.75 0z' },
  { key: null, label: 'General chat', icon: 'M7.5 8.25h9m-9 3H12m-9.75 1.51c0 1.6 1.123 2.994 2.707 3.227 1.129.166 2.27.293 3.423.379.35.026.67.21.865.501L12 21l2.755-4.133a1.14 1.14 0 01.865-.501 48.172 48.172 0 003.423-.379c1.584-.233 2.707-1.626 2.707-3.228V6.741c0-1.602-1.123-2.995-2.707-3.228A48.394 48.394 0 0012 3c-2.392 0-4.744.175-7.043.513C3.373 3.746 2.25 5.14 2.25 6.741v6.018z' },
]

export default function ProjectChatPage() {
  const { projectId } = useParams<{ projectId: string }>()
  const navigate = useNavigate()
  const [project, setProject] = useState<Project | null>(null)
  const [sessions, setSessions] = useState<ChatSession[]>([])
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null)
  const [messages, setMessages] = useState<ProjectChatMessage[]>([])
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [selectedIntent, setSelectedIntent] = useState<string | null>(null)
  const [showSidebar, setShowSidebar] = useState(true)
  const [creatingSession, setCreatingSession] = useState(false)
  const messagesEnd = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const { activity, clearActivity } = useChatSessionWebSocket(activeSessionId)
  const [showFeedbackFor, setShowFeedbackFor] = useState<string | null>(null)
  const [feedbackInput, setFeedbackInput] = useState('')

  useEffect(() => {
    if (!projectId) return
    projectsApi.get(projectId).then((p) => setProject(p as Project))
    loadSessions()
  }, [projectId])

  useEffect(() => {
    messagesEnd.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

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

  async function togglePlanMode() {
    if (!projectId || !activeSessionId) return
    try {
      const result = await projectChat.sessions.togglePlanMode(projectId, activeSessionId)
      setSessions((prev) =>
        prev.map((s) => (s.id === activeSessionId ? { ...s, plan_mode: result.plan_mode } : s))
      )
    } catch {
      // ignore
    }
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
  const isPlanMode = activeSession?.plan_mode === true

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
        selectedIntent || undefined
      )

      // Optimistic plan_mode update when plan is accepted
      if (result.assistant_message.metadata_json?.action === 'plan_accepted') {
        setSessions((prev) =>
          prev.map((s) => (s.id === sessionId ? { ...s, plan_mode: false } : s))
        )
      }

      setMessages((prev) => [
        ...prev.filter((m) => m.id !== tempUserMsg.id),
        result.user_message,
        result.assistant_message,
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
      setSelectedIntent(null)
      setShowFeedbackFor(null)
      setFeedbackInput('')
      clearActivity()
      inputRef.current?.focus()
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
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
      {/* Session sidebar */}
      {showSidebar && (
        <div className="w-56 border-r border-gray-900 flex flex-col shrink-0">
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
                  onClick={() => selectSession(s.id)}
                  className="flex-1 flex items-center gap-2 px-3 py-2 text-left min-w-0"
                >
                  {s.plan_mode ? (
                    <span className="w-1.5 h-1.5 rounded-full bg-amber-500 shrink-0" />
                  ) : (
                    <span className="w-1.5 h-1.5 rounded-full bg-gray-600 shrink-0" />
                  )}
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
                  className="hidden group-hover:block px-2 text-gray-700 hover:text-red-400 transition-colors"
                >
                  <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Main chat area */}
      <div className="flex-1 flex flex-col overflow-hidden">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-3 border-b border-gray-900">
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
                {isPlanMode && (
                  <span className="px-1.5 py-0.5 rounded text-[10px] bg-amber-500/10 text-amber-400">
                    Plan
                  </span>
                )}
              </div>
              <p className="text-[11px] text-gray-600">
                {activeSession ? activeSession.title : 'Chat with AI about this project'}
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            {activeSessionId && (
              <button
                onClick={togglePlanMode}
                className={`flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs transition-colors ${
                  isPlanMode
                    ? 'bg-amber-500/15 text-amber-400 hover:bg-amber-500/25'
                    : 'text-gray-500 hover:text-gray-300 hover:bg-gray-900'
                }`}
                title={isPlanMode ? 'Exit plan mode' : 'Enter plan mode'}
              >
                <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M9 12h3.75M9 15h3.75M9 18h3.75m3 .75H18a2.25 2.25 0 002.25-2.25V6.108c0-1.135-.845-2.098-1.976-2.192a48.424 48.424 0 00-1.123-.08m-5.801 0c-.065.21-.1.433-.1.664 0 .414.336.75.75.75h4.5a.75.75 0 00.75-.75 2.25 2.25 0 00-.1-.664m-5.8 0A2.251 2.251 0 0113.5 2.25H15c1.012 0 1.867.668 2.15 1.586m-5.8 0c-.376.023-.75.05-1.124.08C9.095 4.01 8.25 4.973 8.25 6.108V8.25m0 0H4.875c-.621 0-1.125.504-1.125 1.125v11.25c0 .621.504 1.125 1.125 1.125h9.75c.621 0 1.125-.504 1.125-1.125V9.375c0-.621-.504-1.125-1.125-1.125H8.25z" />
                </svg>
                {isPlanMode ? 'Plan Mode' : 'Plan'}
              </button>
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
                  {isPlanMode ? 'Plan your project' : 'What would you like to do?'}
                </h2>
                <p className="text-sm text-gray-500">
                  {isPlanMode
                    ? 'Discuss scope and requirements. When ready, I\'ll generate a structured plan with tasks and subtasks.'
                    : `Chat with AI to create tasks, ask questions, debug issues, or talk about ${project.name}.`}
                </p>
              </div>

              {!isPlanMode && (
                <div className="grid grid-cols-2 gap-3 w-full max-w-md">
                  {intents.map((intent) => (
                    <button
                      key={intent.key ?? 'general'}
                      onClick={() => {
                        setSelectedIntent(intent.key)
                        inputRef.current?.focus()
                      }}
                      className="flex items-center gap-3 p-4 bg-gray-900 border border-gray-800 rounded-xl text-left hover:border-gray-700 hover:bg-gray-900/80 transition-all group"
                    >
                      <svg
                        className="w-5 h-5 text-gray-600 group-hover:text-indigo-400 transition-colors shrink-0"
                        fill="none"
                        viewBox="0 0 24 24"
                        stroke="currentColor"
                        strokeWidth={1.5}
                      >
                        <path strokeLinecap="round" strokeLinejoin="round" d={intent.icon} />
                      </svg>
                      <span className="text-sm text-gray-400 group-hover:text-gray-300 transition-colors">
                        {intent.label}
                      </span>
                    </button>
                  ))}
                </div>
              )}

              {isPlanMode && (
                <div className="space-y-2 w-full max-w-md">
                  {[
                    'I want to build a new feature — let\'s plan it out',
                    'Help me plan the next sprint for this project',
                    'I need to refactor the authentication module',
                  ].map((suggestion) => (
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
              )}
            </div>
          ) : (
            <div className="px-6 py-4 space-y-4 max-w-3xl mx-auto">
              {messages.map((msg) => (
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
                    {msg.role === 'assistant' ? (
                      <div className="space-y-2">
                        <RenderMarkdown content={msg.content} />
                        {/* Task created badge */}
                        {msg.metadata_json?.action === 'task_created' && msg.metadata_json.task_id && (
                          <div className="flex items-center gap-2 mt-1">
                            <button
                              onClick={() => navigate(`/todos/${msg.metadata_json!.task_id}`)}
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
                        {msg.metadata_json?.action === 'plan_proposed' && msg.metadata_json.plan_data && (
                          <PlanReviewCard
                            planData={msg.metadata_json.plan_data}
                            isAccepted={messages.some(
                              (m) =>
                                m.metadata_json?.action === 'plan_accepted' &&
                                m.created_at > msg.created_at
                            )}
                            onAccept={() => handleSend('approve')}
                            onReject={(fb) => handleSend(fb)}
                            disabled={sending}
                          />
                        )}
                        {/* Fallback: plan_proposed without plan_data (legacy) */}
                        {msg.metadata_json?.action === 'plan_proposed' && !msg.metadata_json.plan_data && (
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
                        {msg.metadata_json?.action === 'plan_accepted' && (
                          <div className="flex items-center gap-2 px-3 py-1.5 bg-emerald-500/5 border border-emerald-500/20 rounded-lg mt-1">
                            <svg className="w-3.5 h-3.5 text-emerald-400 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                              <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
                            </svg>
                            <span className="text-xs text-emerald-400">
                              Plan accepted — {String(msg.metadata_json?.tasks_created ?? 0)} tasks created
                            </span>
                          </div>
                        )}
                        {/* Quick-action buttons on last assistant message (regular chat only, no action metadata) */}
                        {!isPlanMode &&
                          msg.id === messages.filter((m) => m.role === 'assistant').at(-1)?.id &&
                          !msg.metadata_json?.action &&
                          !msg.metadata_json?.tasks_created &&
                          !sending && (
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
                                  onClick={() => handleSend('Looks good, go ahead and create the tasks.')}
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
                      </div>
                    ) : (
                      <div className="whitespace-pre-wrap">{msg.content}</div>
                    )}
                  </div>
                </div>
              ))}
              {sending && (
                <div className="flex justify-start">
                  <div className="bg-gray-900 border border-gray-800 rounded-xl px-4 py-2.5 text-sm text-gray-500">
                    {activity ? (
                      <span className="inline-flex items-center gap-2">
                        <svg className="w-3 h-3 animate-spin text-indigo-400" fill="none" viewBox="0 0 24 24">
                          <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                          <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                        </svg>
                        <span className="text-gray-400">{activity}</span>
                      </span>
                    ) : (
                      <span className="inline-flex gap-1">
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

        {/* Intent indicator */}
        {selectedIntent && (
          <div className="px-6 pb-1">
            <div className="max-w-3xl mx-auto">
              <span className="inline-flex items-center gap-1.5 px-2.5 py-1 bg-indigo-900/30 rounded-full text-xs text-indigo-400">
                {intents.find((i) => i.key === selectedIntent)?.label}
                <button onClick={() => setSelectedIntent(null)} className="hover:text-indigo-300">
                  <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              </span>
            </div>
          </div>
        )}

        {/* Input area */}
        <div className="px-6 py-4 border-t border-gray-900">
          <div className="max-w-3xl mx-auto flex items-end gap-3">
            <div className="flex-1 relative">
              <textarea
                ref={inputRef}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={handleKeyDown}
                placeholder={
                  isPlanMode
                    ? 'Describe what you want to plan...'
                    : selectedIntent === 'create_task'
                      ? 'Describe the task you want to create...'
                      : selectedIntent === 'ask'
                        ? 'Ask a question about the project...'
                        : selectedIntent === 'debug'
                          ? 'Describe the issue you need help debugging...'
                          : 'Type a message...'
                }
                rows={1}
                className="w-full px-4 py-3 bg-gray-900 border border-gray-800 rounded-xl text-sm text-white placeholder-gray-600 resize-none focus:outline-none focus:border-indigo-500 transition-colors"
                style={{ maxHeight: '120px' }}
                onInput={(e) => {
                  const el = e.target as HTMLTextAreaElement
                  el.style.height = 'auto'
                  el.style.height = Math.min(el.scrollHeight, 120) + 'px'
                }}
              />
            </div>
            <button
              onClick={() => handleSend()}
              disabled={!input.trim() || sending}
              className={`px-4 py-3 rounded-xl text-sm text-white transition-colors ${
                isPlanMode
                  ? 'bg-amber-600 hover:bg-amber-500 disabled:bg-gray-800 disabled:text-gray-600'
                  : 'bg-indigo-600 hover:bg-indigo-500 disabled:bg-gray-800 disabled:text-gray-600'
              }`}
            >
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M6 12L3.269 3.126A59.768 59.768 0 0121.485 12 59.77 59.77 0 013.27 20.876L5.999 12zm0 0h7.5"
                />
              </svg>
            </button>
          </div>
        </div>
      </div>
    </div>
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
