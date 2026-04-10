import { create } from 'zustand'
import type { TodoItem, ChatMessage, Deliverable, AgentRun, SubTask, ProviderConfig, ExecutionEvent } from '../types'
import { todos as todosApi, chat as chatApi, deliverables as delApi, agentRuns as agentRunsApi, providers as providersApi } from '../services/api'

interface TodoState {
  todos: Record<string, TodoItem>
  chatMessages: Record<string, ChatMessage[]>
  deliverablesByTodo: Record<string, Deliverable[]>
  agentRunsByTodo: Record<string, AgentRun[]>
  providers: ProviderConfig[]
  activeTodoId: string | null
  isLoading: boolean
  isCreating: boolean
  createError: string | null
  /** Per-subtask activity log: subtaskId → recent activity strings */
  activityLogs: Record<string, string[]>
  /** Per-subtask latest LLM response: subtaskId → { content, iteration } */
  llmResponses: Record<string, { content: string; iteration: number }>
  /** Per-todo execution events for streaming visibility */
  executionEvents: Record<string, ExecutionEvent[]>

  fetchProviders: () => Promise<void>
  fetchTodos: (projectId: string) => Promise<void>
  fetchTodo: (todoId: string) => Promise<void>
  createTodo: (projectId: string, data: { title: string; description?: string; priority?: string; task_type?: string; ai_provider_id?: string; ai_model?: string; scheduled_at?: string }) => Promise<TodoItem>
  clearCreateError: () => void
  cancelTodo: (todoId: string) => Promise<void>
  retryTodo: (todoId: string, withContext?: boolean) => Promise<void>
  triggerSubTask: (todoId: string, subTaskId: string, force?: boolean) => Promise<void>
  acceptDeliverables: (todoId: string) => Promise<void>
  requestChanges: (todoId: string, feedback: string) => Promise<void>
  approvePlan: (todoId: string) => Promise<void>
  rejectPlan: (todoId: string, feedback: string) => Promise<void>
  approveMerge: (todoId: string) => Promise<void>
  rejectMerge: (todoId: string, feedback: string) => Promise<void>
  resumeTodo: (todoId: string) => Promise<void>

  fetchChat: (todoId: string) => Promise<void>
  sendChat: (todoId: string, content: string) => Promise<void>
  appendChatMessage: (todoId: string, msg: ChatMessage) => void

  fetchAgentRuns: (todoId: string) => Promise<void>
  fetchDeliverables: (todoId: string) => Promise<void>

  updateTodoState: (todoId: string, state: string, errorMessage?: string, subState?: string) => void
  updateSubTaskProgress: (todoId: string, subTaskId: string, pct: number, message: string) => void
  appendActivity: (subTaskId: string, activity: string) => void
  batchAppendActivity: (entries: [string, string[]][]) => void
  clearActivity: (subTaskId: string) => void
  setLlmResponse: (subTaskId: string, content: string, iteration: number) => void
  appendExecutionEvent: (todoId: string, event: ExecutionEvent) => void
  setActiveTodo: (todoId: string | null) => void
}

const MAX_ACTIVITY_ENTRIES = 50

export const useTodoStore = create<TodoState>((set, get) => ({
  todos: {},
  chatMessages: {},
  deliverablesByTodo: {},
  agentRunsByTodo: {},
  providers: [],
  activeTodoId: null,
  isLoading: false,
  isCreating: false,
  createError: null,
  activityLogs: {},
  llmResponses: {},
  executionEvents: {},

  fetchProviders: async () => {
    const items = await providersApi.list() as ProviderConfig[]
    set({ providers: items })
  },

  fetchTodos: async (projectId) => {
    set({ isLoading: true })
    const items = await todosApi.list(projectId) as TodoItem[]
    const todosMap: Record<string, TodoItem> = {}
    for (const item of items) todosMap[item.id] = item
    set({ todos: { ...get().todos, ...todosMap }, isLoading: false })
  },

  fetchTodo: async (todoId) => {
    const item = await todosApi.get(todoId) as TodoItem
    // Seed execution events from persisted subtask data (for completed tasks or page refreshes)
    const { executionEvents } = get()
    const existing = executionEvents[todoId] || []
    if (existing.length === 0 && item.sub_tasks) {
      const persisted: ExecutionEvent[] = []
      for (const st of item.sub_tasks) {
        if (st.execution_events && st.execution_events.length > 0) {
          persisted.push(...st.execution_events)
        }
      }
      if (persisted.length > 0) {
        // Sort by backend timestamp
        persisted.sort((a, b) => (a.ts || 0) - (b.ts || 0))
        set({
          todos: { ...get().todos, [todoId]: item },
          executionEvents: { ...get().executionEvents, [todoId]: persisted },
        })
        return
      }
    }
    set({ todos: { ...get().todos, [todoId]: item } })
  },

  clearCreateError: () => set({ createError: null }),

  createTodo: async (projectId, data) => {
    set({ isCreating: true, createError: null })
    try {
      const item = await todosApi.create(projectId, data) as TodoItem
      set({ todos: { ...get().todos, [item.id]: item }, isCreating: false })
      return item
    } catch (e) {
      const message = e instanceof Error ? e.message : 'Failed to create task'
      set({ isCreating: false, createError: message })
      throw e
    }
  },

  cancelTodo: async (todoId) => {
    await todosApi.cancel(todoId)
    const { todos } = get()
    if (todos[todoId]) {
      set({ todos: { ...todos, [todoId]: { ...todos[todoId], state: 'cancelled' } } })
    }
  },

  retryTodo: async (todoId, withContext?: boolean) => {
    await todosApi.retry(todoId, withContext)
    const { todos } = get()
    if (todos[todoId]) {
      set({ todos: { ...todos, [todoId]: { ...todos[todoId], state: 'intake' } } })
    }
  },

  triggerSubTask: async (todoId, subTaskId, force?: boolean) => {
    await todosApi.triggerSubTask(todoId, subTaskId, force ?? false)
    // Optimistically update the subtask status to pending
    const { todos } = get()
    const todo = todos[todoId]
    if (todo?.sub_tasks) {
      const updated = todo.sub_tasks.map((st: SubTask) =>
        st.id === subTaskId ? { ...st, status: 'pending' as const } : st
      )
      set({ todos: { ...todos, [todoId]: { ...todo, sub_tasks: updated } } })
    }
  },

  acceptDeliverables: async (todoId) => {
    await todosApi.acceptDeliverables(todoId)
    const { todos } = get()
    if (todos[todoId]) {
      set({ todos: { ...todos, [todoId]: { ...todos[todoId], state: 'completed' } } })
    }
  },

  requestChanges: async (todoId, feedback) => {
    await todosApi.requestChanges(todoId, feedback)
    const { todos } = get()
    if (todos[todoId]) {
      set({ todos: { ...todos, [todoId]: { ...todos[todoId], state: 'in_progress' } } })
    }
  },

  approvePlan: async (todoId) => {
    await todosApi.approvePlan(todoId)
    const { todos } = get()
    if (todos[todoId]) {
      set({ todos: { ...todos, [todoId]: { ...todos[todoId], state: 'in_progress', plan_json: todos[todoId].plan_json } } })
    }
  },

  rejectPlan: async (todoId, feedback) => {
    await todosApi.rejectPlan(todoId, feedback)
    const { todos } = get()
    if (todos[todoId]) {
      set({ todos: { ...todos, [todoId]: { ...todos[todoId], state: 'planning', plan_json: undefined } } })
    }
  },

  approveMerge: async (todoId) => {
    await todosApi.approveMerge(todoId)
    const { todos } = get()
    if (todos[todoId]) {
      set({ todos: { ...todos, [todoId]: { ...todos[todoId], sub_state: 'merge_approved' } } })
    }
  },

  rejectMerge: async (todoId, feedback) => {
    await todosApi.rejectMerge(todoId, feedback)
    const { todos } = get()
    if (todos[todoId]) {
      set({ todos: { ...todos, [todoId]: { ...todos[todoId], sub_state: 'executing' } } })
    }
  },

  resumeTodo: async (todoId) => {
    await todosApi.resume(todoId)
    const { todos } = get()
    if (todos[todoId]) {
      set({ todos: { ...todos, [todoId]: { ...todos[todoId], sub_state: 'executing' } } })
    }
  },

  fetchChat: async (todoId) => {
    const messages = await chatApi.history(todoId) as ChatMessage[]
    set({ chatMessages: { ...get().chatMessages, [todoId]: messages } })
  },

  sendChat: async (todoId, content) => {
    const msg = await chatApi.send(todoId, content) as ChatMessage
    // Use appendChatMessage for dedup — the WebSocket may have already delivered this message
    get().appendChatMessage(todoId, msg)
  },

  appendChatMessage: (todoId, msg) => {
    const current = get().chatMessages[todoId] || []
    // Deduplicate: skip if a message with the same id already exists
    if (msg.id && current.some((m) => m.id === msg.id)) return
    set({ chatMessages: { ...get().chatMessages, [todoId]: [...current, msg] } })
  },

  fetchAgentRuns: async (todoId) => {
    const items = await agentRunsApi.list(todoId) as AgentRun[]
    set({ agentRunsByTodo: { ...get().agentRunsByTodo, [todoId]: items } })
  },

  fetchDeliverables: async (todoId) => {
    const items = await delApi.list(todoId) as Deliverable[]
    set({ deliverablesByTodo: { ...get().deliverablesByTodo, [todoId]: items } })
  },

  updateTodoState: (todoId, state, errorMessage, subState) => {
    const { todos } = get()
    if (todos[todoId]) {
      const updated = { ...todos[todoId], state: state as TodoItem['state'] }
      if (errorMessage) {
        updated.error_message = errorMessage
      }
      if (subState !== undefined) {
        updated.sub_state = subState
      }
      set({ todos: { ...todos, [todoId]: updated } })
    }
  },

  updateSubTaskProgress: (todoId, subTaskId, pct, message) => {
    const { todos } = get()
    const todo = todos[todoId]
    if (!todo?.sub_tasks) return
    const updated = todo.sub_tasks.map((st: SubTask) =>
      st.id === subTaskId ? { ...st, progress_pct: pct, progress_message: message } : st
    )
    set({ todos: { ...todos, [todoId]: { ...todo, sub_tasks: updated } } })
  },

  appendActivity: (subTaskId, activity) => {
    const { activityLogs } = get()
    const current = activityLogs[subTaskId] || []
    // Dedup consecutive identical messages
    if (current.length > 0 && current[current.length - 1] === activity) return
    const updated = [...current, activity].slice(-MAX_ACTIVITY_ENTRIES)
    set({ activityLogs: { ...activityLogs, [subTaskId]: updated } })
  },

  batchAppendActivity: (entries) => {
    const { activityLogs } = get()
    const next = { ...activityLogs }
    for (const [subTaskId, newEntries] of entries) {
      const current = next[subTaskId] || []
      // Dedup consecutive identical messages within the batch
      const deduped: string[] = []
      let last = current.length > 0 ? current[current.length - 1] : null
      for (const entry of newEntries) {
        if (entry !== last) {
          deduped.push(entry)
          last = entry
        }
      }
      if (deduped.length > 0) {
        next[subTaskId] = [...current, ...deduped].slice(-MAX_ACTIVITY_ENTRIES)
      }
    }
    set({ activityLogs: next })
  },

  clearActivity: (subTaskId) => {
    const { activityLogs } = get()
    const { [subTaskId]: _, ...rest } = activityLogs
    set({ activityLogs: rest })
  },

  setLlmResponse: (subTaskId, content, iteration) => {
    const { llmResponses } = get()
    set({ llmResponses: { ...llmResponses, [subTaskId]: { content, iteration } } })
  },

  appendExecutionEvent: (todoId, event) => {
    const { executionEvents } = get()
    const current = executionEvents[todoId] || []
    set({ executionEvents: { ...executionEvents, [todoId]: [...current, event] } })
  },

  setActiveTodo: (todoId) => set({ activeTodoId: todoId }),
}))
