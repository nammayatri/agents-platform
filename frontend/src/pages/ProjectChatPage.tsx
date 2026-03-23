import { useEffect, useRef, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { Plus, Trash2, PanelLeft, ArrowLeft, Bot, Send, Syringe, ChevronDown, ChevronRight, Loader2, Link2, Users } from 'lucide-react'
import { projectChat, projects as projectsApi, providers as providersApi } from '../services/api'
import PlanReviewCard from '../components/chat/PlanReviewCard'
import ReviewFeedbackCard from '../components/chat/ReviewFeedbackCard'
import { useChatSessionWebSocket } from '../hooks/useChatSessionWebSocket'
import { useAuthStore } from '../stores/authStore'
import type { Project, ChatSession, ProjectChatMessage, ProjectMember, ModelInfo, ChatMode, ChatExecutionInfo } from '../types'

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
  debug: 'text-orange-400 bg-orange-500/10 border border-orange-500/20',
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
  debug: 'text-orange-400 bg-orange-500/10',
  create_task: 'text-indigo-400 bg-indigo-500/10',
}

const sendButtonStyles: Record<ChatMode, string> = {
  auto: 'bg-blue-600 hover:bg-blue-500 disabled:bg-gray-800 disabled:text-gray-600',
  chat: 'bg-indigo-600 hover:bg-indigo-500 disabled:bg-gray-800 disabled:text-gray-600',
  plan: 'bg-amber-600 hover:bg-amber-500 disabled:bg-gray-800 disabled:text-gray-600',
  debug: 'bg-orange-600 hover:bg-orange-500 disabled:bg-gray-800 disabled:text-gray-600',
  create_task: 'bg-indigo-600 hover:bg-indigo-500 disabled:bg-gray-800 disabled:text-gray-600',
}

const sessionDotColors: Record<ChatMode, string> = {
  auto: 'bg-blue-500',
  chat: 'bg-gray-600',
  plan: 'bg-amber-500',
  debug: 'bg-orange-500',
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
        {expanded ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
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

function StreamingPanel({
  liveText,
  completedText,
  isStreaming,
}: {
  liveText: string
  completedText: string
  isStreaming: boolean
}) {
  const [expanded, setExpanded] = useState(false)
  const scrollRef = useRef<HTMLDivElement>(null)
  const prevIsStreaming = useRef(isStreaming)

  // Auto-expand when streaming starts, auto-collapse when done
  useEffect(() => {
    if (isStreaming && !prevIsStreaming.current) {
      setExpanded(true)
    } else if (!isStreaming && prevIsStreaming.current) {
      setExpanded(false)
    }
    prevIsStreaming.current = isStreaming
  }, [isStreaming])

  // Auto-scroll when live content grows
  useEffect(() => {
    if (expanded && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [liveText, expanded])

  const fullText = completedText && liveText
    ? completedText + '\n' + liveText
    : liveText || completedText

  if (!fullText) return null

  return (
    <div className="mt-1.5 border border-gray-800/50 rounded-lg overflow-hidden bg-gray-950/50">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center gap-1.5 px-2.5 py-1.5 text-[11px] text-gray-600 hover:text-gray-400 transition-colors"
      >
        {expanded ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
        <span>
          {isStreaming ? 'Streaming' : 'LLM output'} ({fullText.length.toLocaleString()} chars)
        </span>
        {isStreaming && (
          <span className="w-1.5 h-1.5 rounded-full bg-indigo-400 animate-pulse" />
        )}
      </button>
      {expanded && (
        <div
          ref={scrollRef}
          className="max-h-32 overflow-y-auto px-2.5 py-2 border-t border-gray-800/50"
        >
          <pre className="text-[11px] text-gray-500 font-mono whitespace-pre-wrap leading-relaxed">
            {fullText}
            {isStreaming && (
              <span className="inline-block w-1 h-3 bg-gray-600 animate-pulse ml-0.5 align-text-bottom rounded-sm" />
            )}
          </pre>
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
        {open ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
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
  const currentUser = useAuthStore((s) => s.user)
  const [project, setProject] = useState<Project | null>(null)
  const [sessions, setSessions] = useState<ChatSession[]>([])
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null)
  const [messages, setMessages] = useState<ProjectChatMessage[]>([])
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [showModeDropdown, setShowModeDropdown] = useState(false)
  const [showSidebar, setShowSidebar] = useState(true)
  const [creatingSession, setCreatingSession] = useState(false)
  const [members, setMembers] = useState<ProjectMember[]>([])
  const [filterUserId, setFilterUserId] = useState<string>('all')
  const messagesEnd = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const deletedSessionIdsRef = useRef<Set<string>>(new Set())
  const { activity, streamingText, completedStreaming, isStreaming, clearActivity, resetStreaming, incomingMessage, clearIncomingMessage } = useChatSessionWebSocket(activeSessionId)
  const [deleteDialog, setDeleteDialog] = useState<{
    sessionId: string
    sessionTitle: string
    linkedTodoId?: string
  } | null>(null)
  const [showFeedbackFor, setShowFeedbackFor] = useState<string | null>(null)
  const [feedbackInput, setFeedbackInput] = useState('')
  const [chatModels, setChatModels] = useState<ModelInfo[]>([])
  const [selectedModel, setSelectedModel] = useState('')
  const [approvedTaskPlans, setApprovedTaskPlans] = useState<Set<string>>(new Set())
  const [discardedTaskPlans, setDiscardedTaskPlans] = useState<Set<string>>(new Set())
  const [taskPlanFeedbackFor, setTaskPlanFeedbackFor] = useState<string | null>(null)
  const [taskPlanFeedback, setTaskPlanFeedback] = useState('')
  const [cancelDialog, setCancelDialog] = useState<{
    subtasks: Array<{ id: string; title: string; agent_role: string; status: string }>
    sessionId: string
    oldTodoId: string
  } | null>(null)
  const [cancelSelection, setCancelSelection] = useState<Set<string>>(new Set())
  const [cancelling, setCancelling] = useState(false)

  useEffect(() => {
    if (!projectId) return
    projectsApi.get(projectId).then((p) => setProject(p as Project))
    loadSessions()
    // Load project members for the filter dropdown
    projectsApi.members.list(projectId).then((res) => {
      const all: ProjectMember[] = []
      if (res.owner) all.push(res.owner as ProjectMember)
      if (res.members) all.push(...(res.members as ProjectMember[]))
      setMembers(all)
    }).catch(() => setMembers([]))
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
  }, [messages, isStreaming, completedStreaming])

  // Handle incoming chat messages from the coordinator via WebSocket
  // (e.g., task_plan_ready after re-planning, system messages during execution)
  useEffect(() => {
    if (!incomingMessage || !projectId) return
    const msg: ProjectChatMessage = {
      id: `ws-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      project_id: projectId,
      user_id: '',
      role: (incomingMessage.role as 'user' | 'assistant' | 'system') || 'system',
      content: incomingMessage.content,
      metadata_json: incomingMessage.metadata_json as ProjectChatMessage['metadata_json'],
      created_at: new Date().toISOString(),
    }
    setMessages((prev) => [...prev, msg])
    clearIncomingMessage()
  }, [incomingMessage, projectId, clearIncomingMessage])

  async function loadSessions() {
    if (!projectId) return
    try {
      const list = await projectChat.sessions.list(projectId)
      // Filter out sessions that were deleted locally (handles race with in-flight fetches)
      const sessionList = (list as ChatSession[]).filter(
        (s) => !deletedSessionIdsRef.current.has(s.id)
      )
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
      const msgs = data.messages || []
      setMessages(msgs)

      // Pre-populate approvedTaskPlans and discardedTaskPlans from message history
      const approved = new Set<string>()
      const discarded = new Set<string>()
      for (const msg of msgs) {
        const meta = parseMeta(msg)
        if (meta?.action === 'task_plan_ready') {
          const hasApproval = msgs.some((m) => {
            const mm = parseMeta(m)
            return (
              m.created_at > msg.created_at &&
              (mm?.action === 'task_created' ||
                (m.role === 'system' && m.content.includes('Plan approved')))
            )
          })
          const hasDiscard = msgs.some((m) =>
            m.created_at > msg.created_at &&
            m.role === 'system' &&
            m.content.includes('Plan discarded')
          )
          if (hasApproval) approved.add(msg.id)
          else if (hasDiscard) discarded.add(msg.id)
        }
      }
      setApprovedTaskPlans(approved)
      setDiscardedTaskPlans(discarded)
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

  function promptDeleteSession(sessionId: string) {
    const session = sessions.find((s) => s.id === sessionId)
    if (!session) return
    setDeleteDialog({
      sessionId,
      sessionTitle: session.title,
      linkedTodoId: session.linked_todo_id || undefined,
    })
  }

  async function confirmDeleteSession() {
    if (!projectId || !deleteDialog) return
    const { sessionId } = deleteDialog
    setDeleteDialog(null)
    try {
      await projectChat.sessions.delete(projectId, sessionId)
      deletedSessionIdsRef.current.add(sessionId)
      setSessions((prev) => {
        const remaining = prev.filter((s) => s.id !== sessionId)
        if (activeSessionId === sessionId && remaining.length > 0) {
          const next = remaining[0]
          setActiveSessionId(next.id)
          loadSessionMessages(next.id)
        } else if (activeSessionId === sessionId) {
          setActiveSessionId(null)
          setMessages([])
        }
        return remaining
      })
    } catch {
      // Backend rejected — likely linked to a task we didn't know about
      const session = sessions.find((s) => s.id === sessionId)
      if (session) {
        setDeleteDialog({
          sessionId,
          sessionTitle: session.title,
          linkedTodoId: session.linked_todo_id || 'unknown',
        })
      }
    }
  }

  const activeSession = sessions.find((s) => s.id === activeSessionId)
  const chatMode: ChatMode = (activeSession?.chat_mode as ChatMode) || (activeSession?.plan_mode ? 'plan' : 'auto')
  const [lastRoutingMode, setLastRoutingMode] = useState<string>('chat')

  const handleSend = async (overrideContent?: string) => {
    const content = overrideContent || input.trim()
    if (!content || !projectId || sending) return
    if (!overrideContent) setInput('')
    resetStreaming()
    setSending(true)

    const tempUserMsg: ProjectChatMessage = {
      id: `temp-${Date.now()}`,
      project_id: projectId,
      user_id: currentUser?.id || '',
      role: 'user',
      content,
      sender_name: currentUser?.display_name || '',
      sender_avatar_url: currentUser?.avatar_url || null,
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

  const handleApproveTaskPlan = async () => {
    if (!projectId || !activeSessionId || sending) return
    setSending(true)
    try {
      const result = await projectChat.sessions.acceptTaskPlan(projectId, activeSessionId)
      setMessages((prev) => [...prev, result.user_message, result.assistant_message])
      // Mark the task_plan_ready message as approved using the message ID
      if (latestTaskPlanMsgId) {
        setApprovedTaskPlans((prev) => new Set(prev).add(latestTaskPlanMsgId))
      }
      // Show cancel dialog if there are existing active subtasks on old linked todo
      if (result.existing_active_subtasks && result.existing_active_subtasks.length > 0 && result.old_todo_id) {
        setCancelDialog({
          subtasks: result.existing_active_subtasks,
          sessionId: activeSessionId,
          oldTodoId: result.old_todo_id,
        })
        setCancelSelection(new Set(result.existing_active_subtasks.map((s) => s.id)))
      }
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

  const handleCancelSubtasks = async () => {
    if (!projectId || !cancelDialog || cancelling) return
    setCancelling(true)
    try {
      const ids = [...cancelSelection]
      if (ids.length > 0) {
        await projectChat.sessions.cancelSubtasks(projectId, cancelDialog.sessionId, ids)
      }
      setCancelDialog(null)
      setCancelSelection(new Set())
    } catch (err) {
      const errorMsg: ProjectChatMessage = {
        id: `error-${Date.now()}`,
        project_id: projectId!,
        user_id: '',
        role: 'system',
        content: `Error cancelling subtasks: ${err instanceof Error ? err.message : 'Unknown error'}`,
        created_at: new Date().toISOString(),
      }
      setMessages((prev) => [...prev, errorMsg])
    } finally {
      setCancelling(false)
    }
  }

  const handleDiscardTaskPlan = async (feedback: string) => {
    if (sending || !feedback.trim() || !projectId || !activeSessionId) return
    setSending(true)
    try {
      await projectChat.sessions.discardTaskPlan(projectId, activeSessionId, feedback.trim())
      const discardMsg: ProjectChatMessage = {
        id: `plan-discarded-${Date.now()}`,
        project_id: projectId,
        user_id: '',
        role: 'system',
        content: `Plan discarded. Revising based on feedback...`,
        created_at: new Date().toISOString(),
      }
      setMessages((prev) => [...prev, discardMsg])
      // Mark the plan message as discarded so buttons disappear
      if (latestTaskPlanMsgId) {
        setDiscardedTaskPlans((prev) => new Set(prev).add(latestTaskPlanMsgId))
      }
      // Send feedback as a new message so the LLM generates a new plan
      await handleSend(`The previous plan wasn't right. Here's what needs to change: ${feedback.trim()}`)
    } catch (err) {
      const errorMsg: ProjectChatMessage = {
        id: `error-${Date.now()}`,
        project_id: projectId,
        user_id: '',
        role: 'system',
        content: `Error: ${err instanceof Error ? err.message : 'Failed to discard plan'}`,
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
    if (activeSessionId) {
      promptDeleteSession(activeSessionId)
    } else {
      if (!confirm('Clear chat history?')) return
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
      <div className="flex items-center justify-center h-full animate-fade-in">
        <div className="flex items-center gap-2 text-gray-600 text-sm">
          <Loader2 className="w-4 h-4 animate-spin" />
          Loading...
        </div>
      </div>
    )
  }

  const hasMessages = messages.length > 0

  // Compute the latest unapproved plan message IDs so only the newest plan shows action buttons
  const latestTaskPlanMsgId = (() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = parseMeta(messages[i])
      if (m?.action === 'task_plan_ready' && !approvedTaskPlans.has(messages[i].id) && !discardedTaskPlans.has(messages[i].id))
        return messages[i].id
    }
    return null
  })()

  const latestPlanProposedMsgId = (() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = parseMeta(messages[i])
      if (m?.action === 'plan_proposed' && !messages.some(
        (m2) => parseMeta(m2)?.action === 'plan_accepted' && m2.created_at > messages[i].created_at
      )) return messages[i].id
    }
    return null
  })()

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
                <Plus className="w-3.5 h-3.5" />
                New Chat
              </button>
            </div>

            {/* Member filter */}
            {members.length > 1 && (
              <div className="px-3 pb-2">
                <div className="flex items-center gap-1.5">
                  <Users className="w-3 h-3 text-gray-600" />
                  <select
                    value={filterUserId}
                    onChange={(e) => setFilterUserId(e.target.value)}
                    className="flex-1 px-2 py-1 bg-gray-900 border border-gray-800 rounded-lg text-[11px] text-gray-400 focus:outline-none focus:border-indigo-500 transition-colors"
                  >
                    <option value="all">All members</option>
                    {members.map((m) => (
                      <option key={m.id} value={m.id}>
                        {m.display_name}{m.id === currentUser?.id ? ' (me)' : ''}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
            )}

            <div className="flex-1 overflow-y-auto px-2 pb-2 space-y-0.5">
              {sessions
                .filter((s) => filterUserId === 'all' || s.user_id === filterUserId)
                .map((s) => {
                const isActive = activeSessionId === s.id
                const isOtherUser = currentUser && s.user_id !== currentUser.id
                return (
                  <div
                    key={s.id}
                    className={`group flex items-center rounded-lg transition-colors ${
                      isActive ? 'bg-gray-900' : 'hover:bg-gray-900/50'
                    }`}
                  >
                    <button
                      onClick={() => {
                        selectSession(s.id)
                        if (window.innerWidth < 768) setShowSidebar(false)
                      }}
                      className="flex-1 flex items-center gap-2 px-3 py-2 text-left min-w-0"
                    >
                      <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${sessionDotColors[(s.chat_mode as ChatMode) || (s.plan_mode ? 'plan' : 'chat')]}`} />
                      <div className="min-w-0 flex-1">
                        <span
                          className={`text-xs truncate block ${
                            isActive ? 'text-white' : 'text-gray-500'
                          }`}
                        >
                          {s.title}
                        </span>
                        {isOtherUser && s.creator_name && (
                          <span className="text-[10px] text-gray-600 truncate block">
                            {s.creator_name}
                          </span>
                        )}
                      </div>
                    </button>
                    {s.linked_todo_id && (
                      <Link2 className="w-3 h-3 text-indigo-500/40 shrink-0 mr-0.5" />
                    )}
                    <button
                      onClick={(e) => {
                        e.stopPropagation()
                        promptDeleteSession(s.id)
                      }}
                      className={`shrink-0 px-1.5 py-1 rounded transition-colors ${
                        isActive
                          ? 'text-gray-600 hover:text-red-400 hover:bg-red-500/10'
                          : 'opacity-0 group-hover:opacity-100 text-gray-700 hover:text-red-400 hover:bg-red-500/10'
                      }`}
                      title="Delete session"
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                  </div>
                )
              })}
            </div>
          </div>
        </>
      )}

      {/* Delete session dialog */}
      {deleteDialog && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={() => setDeleteDialog(null)}>
          <div className="bg-gray-900 border border-gray-800 rounded-xl px-5 py-4 max-w-sm mx-4 space-y-3" onClick={(e) => e.stopPropagation()}>
            {deleteDialog.linkedTodoId ? (
              <>
                <div className="flex items-center gap-2 text-sm text-white font-medium">
                  <Link2 className="w-4 h-4 text-indigo-400" />
                  Session linked to a task
                </div>
                <p className="text-xs text-gray-400 leading-relaxed">
                  <span className="text-gray-300 font-medium">{deleteDialog.sessionTitle}</span> is linked to an active task and cannot be deleted.
                </p>
                <div className="flex items-center gap-2 pt-1">
                  <button
                    onClick={() => {
                      navigate(`/todos/${deleteDialog.linkedTodoId}`)
                      setDeleteDialog(null)
                    }}
                    className="px-3 py-1.5 bg-indigo-600 hover:bg-indigo-500 rounded-lg text-xs font-medium text-white transition-colors"
                  >
                    View Task
                  </button>
                  <button
                    onClick={() => setDeleteDialog(null)}
                    className="px-3 py-1.5 bg-gray-800 hover:bg-gray-700 rounded-lg text-xs text-gray-400 transition-colors"
                  >
                    Cancel
                  </button>
                </div>
              </>
            ) : (
              <>
                <div className="flex items-center gap-2 text-sm text-white font-medium">
                  <Trash2 className="w-4 h-4 text-red-400" />
                  Delete chat session
                </div>
                <p className="text-xs text-gray-400 leading-relaxed">
                  <span className="text-gray-300 font-medium">{deleteDialog.sessionTitle}</span> and all its messages will be permanently deleted.
                </p>
                <div className="flex items-center gap-2 pt-1">
                  <button
                    onClick={confirmDeleteSession}
                    className="px-3 py-1.5 bg-red-600 hover:bg-red-500 rounded-lg text-xs font-medium text-white transition-colors"
                  >
                    Delete
                  </button>
                  <button
                    onClick={() => setDeleteDialog(null)}
                    className="px-3 py-1.5 bg-gray-800 hover:bg-gray-700 rounded-lg text-xs text-gray-400 transition-colors"
                  >
                    Cancel
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
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
              <PanelLeft className="w-4 h-4" />
            </button>
            <button
              onClick={() => navigate(`/projects/${projectId}`)}
              className="text-gray-500 hover:text-gray-300 transition-colors"
            >
              <ArrowLeft className="w-4 h-4" />
            </button>
            <div>
              <div className="flex items-center gap-2">
                <h1 className="text-sm font-medium text-white">{project.name}</h1>
                {chatMode !== 'chat' && (
                  <span className={`px-1.5 py-0.5 rounded text-[10px] ${
                    chatMode === 'auto' ? 'bg-blue-500/10 text-blue-400'
                    : chatMode === 'plan' ? 'bg-amber-500/10 text-amber-400'
                    : chatMode === 'debug' ? 'bg-orange-500/10 text-orange-400'
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
                const isOtherUserMsg = msg.role === 'user' && currentUser && msg.user_id && msg.user_id !== currentUser.id && msg.user_id !== ''
                return (
                <div
                  key={msg.id}
                  className={`flex ${msg.role === 'user' ? (isOtherUserMsg ? 'justify-start gap-2.5' : 'justify-end') : 'justify-start gap-2.5'} animate-fade-in`}
                >
                  {msg.role === 'assistant' && (
                    <div className="w-6 h-6 rounded-full bg-indigo-500/20 flex items-center justify-center shrink-0 mt-1">
                      <Bot className="w-3.5 h-3.5 text-indigo-400" />
                    </div>
                  )}
                  {isOtherUserMsg && (
                    <div className="w-6 h-6 rounded-full bg-gray-800 flex items-center justify-center shrink-0 mt-1 text-[10px] text-gray-400 font-medium overflow-hidden">
                      {msg.sender_avatar_url ? (
                        <img src={msg.sender_avatar_url} className="w-6 h-6 rounded-full object-cover" alt="" />
                      ) : (
                        (msg.sender_name || '?')[0].toUpperCase()
                      )}
                    </div>
                  )}
                  <div
                    className={`max-w-[80%] rounded-xl px-4 py-2.5 text-sm ${
                      msg.role === 'user'
                        ? isOtherUserMsg
                          ? 'bg-gray-900 border border-gray-800 text-gray-300'
                          : 'bg-indigo-600 text-white'
                        : msg.role === 'system'
                          ? msg.content.startsWith('Error')
                            ? 'bg-red-900/30 text-red-400 border border-red-800/50'
                            : 'bg-gray-800/50 text-gray-400 border border-gray-700/50'
                          : 'bg-gray-900 border border-gray-800 text-gray-300'
                    }`}
                  >
                    <div className={`text-[10px] mb-1 font-mono ${msg.role === 'user' ? (isOtherUserMsg ? 'text-gray-600' : 'text-indigo-300/50') : 'text-gray-600'}`}>
                      {isOtherUserMsg && msg.sender_name && (
                        <span className="text-gray-500 mr-1.5">{msg.sender_name}</span>
                      )}
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
                              className="inline-flex items-center gap-1 px-2 py-1 text-gray-600 rounded-lg text-xs hover:text-gray-400 hover:bg-gray-800 transition-colors"
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
                            isLatest={msg.id === latestPlanProposedMsgId}
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
                        {/* Task plan ready — needs user approval */}
                        {meta?.action === 'task_plan_ready' && !approvedTaskPlans.has(msg.id) && !discardedTaskPlans.has(msg.id) && (
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
                            {meta.plan_data?.summary && (
                              <div className="px-3 py-2 border-b border-gray-800/50">
                                <p className="text-[11px] text-gray-500 leading-relaxed">{meta.plan_data.summary}</p>
                              </div>
                            )}
                            {meta.plan_data?.sub_tasks && (
                              <div className="px-3 py-2 space-y-1.5">
                                {meta.plan_data.sub_tasks.map((st, i: number) => (
                                  <details key={i} className="group">
                                    <summary className="flex items-start gap-2 text-xs cursor-pointer list-none hover:bg-gray-800/30 rounded px-1 -mx-1 py-0.5">
                                      <span className="text-gray-700 font-mono w-4 text-right shrink-0 mt-0.5">{i + 1}</span>
                                      <span className="px-1.5 py-0.5 bg-gray-800 rounded text-[10px] text-gray-500 font-mono shrink-0">
                                        {st.agent_role}
                                      </span>
                                      <span className="text-gray-400 flex-1">{st.title}</span>
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
                                          {'\u2192'} #{st.depends_on.map((d: number) => d + 1).join(', #')}
                                        </span>
                                      )}
                                    </summary>
                                    <div className="ml-7 pl-3 border-l border-gray-800 mt-1 mb-1.5 space-y-1.5 py-1">
                                      {st.description && (
                                        <p className="text-[11px] text-gray-500 leading-relaxed">{String(st.description)}</p>
                                      )}
                                      {Array.isArray(st.context?.relevant_files) && st.context!.relevant_files.length > 0 && (
                                        <div>
                                          <span className="text-[10px] text-gray-600 uppercase tracking-wider">Files</span>
                                          <div className="mt-0.5 flex flex-wrap gap-1">
                                            {st.context!.relevant_files.map((f: string, fi: number) => (
                                              <span key={fi} className="text-[11px] font-mono text-indigo-400/70 bg-indigo-500/5 px-1.5 py-0.5 rounded">{String(f)}</span>
                                            ))}
                                          </div>
                                        </div>
                                      )}
                                      {st.context?.what_to_change && (
                                        <div>
                                          <span className="text-[10px] text-gray-600 uppercase tracking-wider">What to change</span>
                                          <p className="text-[11px] text-gray-500 mt-0.5 leading-relaxed">{String(st.context.what_to_change)}</p>
                                        </div>
                                      )}
                                      {st.context?.current_state && (
                                        <div>
                                          <span className="text-[10px] text-gray-600 uppercase tracking-wider">Current state</span>
                                          <p className="text-[11px] text-gray-500 mt-0.5 leading-relaxed">{String(st.context.current_state)}</p>
                                        </div>
                                      )}
                                      {st.context?.patterns_to_follow && (
                                        <div>
                                          <span className="text-[10px] text-gray-600 uppercase tracking-wider">Patterns to follow</span>
                                          <p className="text-[11px] text-gray-500 mt-0.5 leading-relaxed">{String(st.context.patterns_to_follow)}</p>
                                        </div>
                                      )}
                                      {st.context?.related_code && (
                                        <div>
                                          <span className="text-[10px] text-gray-600 uppercase tracking-wider">Related code</span>
                                          <pre className="text-[11px] text-gray-500 mt-0.5 font-mono whitespace-pre-wrap leading-relaxed bg-gray-950 rounded px-2 py-1.5 border border-gray-800/50 max-h-32 overflow-y-auto">{String(st.context.related_code)}</pre>
                                        </div>
                                      )}
                                      {st.context?.integration_points && (
                                        <div>
                                          <span className="text-[10px] text-gray-600 uppercase tracking-wider">Integration points</span>
                                          <p className="text-[11px] text-gray-500 mt-0.5 leading-relaxed">{String(st.context.integration_points)}</p>
                                        </div>
                                      )}
                                    </div>
                                  </details>
                                ))}
                              </div>
                            )}
                            {msg.id === latestTaskPlanMsgId ? (
                            <div className="px-3 py-2 border-t border-gray-800/50">
                              {taskPlanFeedbackFor === msg.id ? (
                                <div className="flex items-center gap-2">
                                  <input
                                    type="text"
                                    value={taskPlanFeedback}
                                    onChange={(e) => setTaskPlanFeedback(e.target.value)}
                                    onKeyDown={(e) => {
                                      if (e.key === 'Enter' && taskPlanFeedback.trim()) {
                                        handleDiscardTaskPlan(taskPlanFeedback)
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
                                    onClick={() => handleDiscardTaskPlan(taskPlanFeedback)}
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
                                    onClick={() => handleApproveTaskPlan()}
                                    disabled={sending}
                                    className="px-3 py-1.5 bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 rounded-lg text-xs font-medium text-white transition-colors"
                                  >
                                    Approve Plan
                                  </button>
                                  <button
                                    onClick={() => setTaskPlanFeedbackFor(msg.id)}
                                    disabled={sending}
                                    className="px-3 py-1.5 bg-gray-800 hover:bg-gray-700 disabled:opacity-50 rounded-lg text-xs text-gray-400 transition-colors"
                                  >
                                    Discard &amp; Revise
                                  </button>
                                </div>
                              )}
                            </div>
                            ) : (
                            <div className="px-3 py-2 border-t border-gray-800/50">
                              <span className="text-[11px] text-gray-600">Earlier plan — superseded</span>
                            </div>
                            )}
                          </div>
                        )}
                        {/* Task plan approved badge */}
                        {meta?.action === 'task_plan_ready' && approvedTaskPlans.has(msg.id) && (
                          <div className="flex items-center gap-2 px-3 py-1.5 bg-emerald-500/5 border border-emerald-500/20 rounded-lg mt-1">
                            <svg className="w-3.5 h-3.5 text-emerald-400 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                              <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
                            </svg>
                            <span className="text-xs text-emerald-400">Plan approved — executing</span>
                          </div>
                        )}
                        {/* Cancel existing subtasks dialog — shown after task plan approval */}
                        {meta?.action === 'task_plan_ready' && approvedTaskPlans.has(msg.id) && cancelDialog && (
                          <div className="mt-2 bg-gray-950 border border-red-500/20 rounded-lg overflow-hidden">
                            <div className="px-3 py-2 border-b border-gray-800/50 flex items-center gap-2">
                              <svg className="w-3.5 h-3.5 text-red-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                                <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
                              </svg>
                              <span className="text-xs font-medium text-red-400">Active subtasks on previous task</span>
                              <span className="text-[10px] text-gray-600 ml-auto">
                                {cancelSelection.size}/{cancelDialog.subtasks.length} selected
                              </span>
                            </div>
                            <div className="px-3 py-2 space-y-1">
                              {cancelDialog.subtasks.map((st) => (
                                <label key={st.id} className="flex items-center gap-2 py-1 px-1 hover:bg-gray-800/30 rounded cursor-pointer">
                                  <input
                                    type="checkbox"
                                    checked={cancelSelection.has(st.id)}
                                    onChange={() => {
                                      setCancelSelection((prev) => {
                                        const next = new Set(prev)
                                        if (next.has(st.id)) next.delete(st.id); else next.add(st.id)
                                        return next
                                      })
                                    }}
                                    className="rounded border-gray-700 bg-gray-900 text-red-500 focus:ring-red-500/20 w-3.5 h-3.5"
                                  />
                                  <span className="px-1.5 py-0.5 bg-gray-800 rounded text-[10px] text-gray-500 font-mono shrink-0">
                                    {st.agent_role}
                                  </span>
                                  <span className="text-xs text-gray-400 flex-1 truncate">{st.title}</span>
                                  <span className={`text-[10px] px-1.5 py-0.5 rounded ${
                                    st.status === 'running' ? 'bg-blue-500/10 text-blue-400' :
                                    st.status === 'assigned' ? 'bg-amber-500/10 text-amber-400' :
                                    'bg-gray-800 text-gray-500'
                                  }`}>
                                    {st.status}
                                  </span>
                                </label>
                              ))}
                            </div>
                            <div className="px-3 py-2 border-t border-gray-800/50 flex items-center gap-2">
                              <button
                                onClick={handleCancelSubtasks}
                                disabled={cancelling || cancelSelection.size === 0}
                                className="px-3 py-1.5 bg-red-600 hover:bg-red-500 disabled:opacity-40 rounded-lg text-xs font-medium text-white transition-colors"
                              >
                                {cancelling ? 'Cancelling...' : `Cancel ${cancelSelection.size} subtask${cancelSelection.size !== 1 ? 's' : ''}`}
                              </button>
                              <button
                                onClick={() => { setCancelDialog(null); setCancelSelection(new Set()) }}
                                disabled={cancelling}
                                className="px-3 py-1.5 bg-gray-800 hover:bg-gray-700 disabled:opacity-40 rounded-lg text-xs text-gray-400 transition-colors"
                              >
                                Keep All
                              </button>
                            </div>
                          </div>
                        )}
                        {/* Task plan discarded badge */}
                        {meta?.action === 'task_plan_ready' && discardedTaskPlans.has(msg.id) && (
                          <div className="flex items-center gap-2 px-3 py-1.5 bg-amber-500/5 border border-amber-500/20 rounded-lg mt-1">
                            <svg className="w-3.5 h-3.5 text-amber-400 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                            </svg>
                            <span className="text-xs text-amber-400">Plan discarded — revising</span>
                          </div>
                        )}
                        {/* Plan review verdict */}
                        {meta?.action === 'plan_review_verdict' && (
                          <ReviewFeedbackCard
                            type="plan"
                            approved={!!meta.approved}
                            feedback={meta.feedback as string | undefined}
                            iteration={meta.iteration as number | undefined}
                          />
                        )}
                        {/* Code review verdict */}
                        {meta?.action === 'code_review_verdict' && (
                          <ReviewFeedbackCard
                            type="code"
                            approved={meta.verdict === 'approved'}
                            feedback={meta.feedback as string | undefined}
                            summary={meta.summary as string | undefined}
                            issues={meta.issues}
                            subtaskTitle={meta.subtask_title as string | undefined}
                          />
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
              {(sending || activity || isStreaming) && (
                <div className="flex justify-start">
                  <div className="max-w-[80%]">
                    <div className="bg-gray-900 border border-gray-800 rounded-xl px-4 py-2.5 text-sm">
                      {activity ? (
                        <span className="inline-flex items-center gap-2 text-gray-500">
                          <Loader2 className="w-3 h-3 animate-spin text-indigo-400" />
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
                    <StreamingPanel liveText={streamingText} completedText={completedStreaming} isStreaming={isStreaming} />
                  </div>
                </div>
              )}
              {!sending && !activity && !isStreaming && completedStreaming && (
                <div className="flex justify-start">
                  <div className="max-w-[80%]">
                    <StreamingPanel liveText="" completedText={completedStreaming} isStreaming={false} />
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
                <ChevronDown className="w-3 h-3 opacity-50" />
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
                <Syringe className="w-4 h-4" />
              </button>
            ) : (
              <button
                onClick={() => handleSend()}
                disabled={!input.trim() || sending}
                className={`shrink-0 px-3 py-2.5 rounded-xl text-sm text-white transition-colors ${sendButtonStyles[chatMode]}`}
              >
                <Send className="w-4 h-4" />
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
