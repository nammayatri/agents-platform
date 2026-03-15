import { create } from 'zustand'
import type { TodoItem, ChatMessage, Deliverable, SubTask, ProviderConfig } from '../types'
import { todos as todosApi, chat as chatApi, deliverables as delApi, providers as providersApi } from '../services/api'

interface TodoState {
  todos: Record<string, TodoItem>
  chatMessages: Record<string, ChatMessage[]>
  deliverablesByTodo: Record<string, Deliverable[]>
  providers: ProviderConfig[]
  activeTodoId: string | null
  isLoading: boolean
  isCreating: boolean
  createError: string | null

  fetchProviders: () => Promise<void>
  fetchTodos: (projectId: string) => Promise<void>
  fetchTodo: (todoId: string) => Promise<void>
  createTodo: (projectId: string, data: { title: string; description?: string; priority?: string; task_type?: string; ai_provider_id?: string; scheduled_at?: string }) => Promise<TodoItem>
  clearCreateError: () => void
  cancelTodo: (todoId: string) => Promise<void>
  retryTodo: (todoId: string, withContext?: boolean) => Promise<void>
  acceptDeliverables: (todoId: string) => Promise<void>
  requestChanges: (todoId: string, feedback: string) => Promise<void>
  approvePlan: (todoId: string) => Promise<void>
  rejectPlan: (todoId: string, feedback: string) => Promise<void>

  fetchChat: (todoId: string) => Promise<void>
  sendChat: (todoId: string, content: string) => Promise<void>
  appendChatMessage: (todoId: string, msg: ChatMessage) => void

  fetchDeliverables: (todoId: string) => Promise<void>

  updateTodoState: (todoId: string, state: string) => void
  updateSubTaskProgress: (todoId: string, subTaskId: string, pct: number, message: string) => void
  setActiveTodo: (todoId: string | null) => void
}

export const useTodoStore = create<TodoState>((set, get) => ({
  todos: {},
  chatMessages: {},
  deliverablesByTodo: {},
  providers: [],
  activeTodoId: null,
  isLoading: false,
  isCreating: false,
  createError: null,

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

  fetchChat: async (todoId) => {
    const messages = await chatApi.history(todoId) as ChatMessage[]
    set({ chatMessages: { ...get().chatMessages, [todoId]: messages } })
  },

  sendChat: async (todoId, content) => {
    const msg = await chatApi.send(todoId, content) as ChatMessage
    const current = get().chatMessages[todoId] || []
    set({ chatMessages: { ...get().chatMessages, [todoId]: [...current, msg] } })
  },

  appendChatMessage: (todoId, msg) => {
    const current = get().chatMessages[todoId] || []
    // Deduplicate: skip if a message with the same id already exists
    if (msg.id && current.some((m) => m.id === msg.id)) return
    set({ chatMessages: { ...get().chatMessages, [todoId]: [...current, msg] } })
  },

  fetchDeliverables: async (todoId) => {
    const items = await delApi.list(todoId) as Deliverable[]
    set({ deliverablesByTodo: { ...get().deliverablesByTodo, [todoId]: items } })
  },

  updateTodoState: (todoId, state) => {
    const { todos } = get()
    if (todos[todoId]) {
      set({ todos: { ...todos, [todoId]: { ...todos[todoId], state: state as TodoItem['state'] } } })
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

  setActiveTodo: (todoId) => set({ activeTodoId: todoId }),
}))
