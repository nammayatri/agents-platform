import type {
  Project, TodoItem, ChatMessage, Deliverable, AgentRun,
  ProviderConfig, GitProviderConfig, Skill, McpServer,
  NotificationChannel, AgentConfig, DefaultAgentInfo, AvailableTool,
  ChatSession, ProjectEnablement, AgentChatMessage, ProjectChatMessage,
  ProjectCreatePayload, ProjectUpdatePayload,
  TodoCreatePayload, TodoUpdatePayload,
  ProviderCreatePayload, ProviderUpdatePayload,
  GitProviderPayload, SkillPayload, McpServerPayload,
  NotificationChannelPayload, AgentCreatePayload, AgentUpdatePayload,
  ProjectMember, DebugContext, ProjectMemory,
  FileTreeNode, FileContent, GitStatus,
  ProviderRepos, ReleaseConfig,
  MergePipelineConfig, PipelineRun, PipelineVariable,
  PostMergeRepoConfig,
} from '../types'

const API_BASE = '/api'

function getToken(): string | null {
  return localStorage.getItem('token')
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const token = getToken()
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(options.headers as Record<string, string> || {}),
  }
  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }

  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers,
  })

  if (response.status === 401) {
    localStorage.removeItem('token')
    window.location.href = '/login'
    throw new Error('Unauthorized')
  }

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: 'Request failed' }))
    throw new Error(error.detail || `HTTP ${response.status}`)
  }

  if (response.status === 204) return undefined as T
  return response.json()
}

// Auth
export const auth = {
  login: (email: string, password: string) =>
    request<{ access_token: string }>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    }),
  register: (email: string, display_name: string, password: string) =>
    request<{ access_token: string }>('/auth/register', {
      method: 'POST',
      body: JSON.stringify({ email, display_name, password }),
    }),
  me: () => request<{ id: string; email: string; display_name: string; role: string }>('/auth/me'),
}

// Projects
export const projects = {
  list: () => request<Project[]>('/projects'),
  create: (data: ProjectCreatePayload) =>
    request<Project>('/projects', { method: 'POST', body: JSON.stringify(data) }),
  get: (id: string) => request<Project>(`/projects/${id}`),
  update: (id: string, data: ProjectUpdatePayload) =>
    request<Project>(`/projects/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  delete: (id: string) => request<void>(`/projects/${id}`, { method: 'DELETE' }),
  analyze: (id: string) => request<{ status: string }>(`/projects/${id}/analyze`, { method: 'POST' }),
  cancelAnalysis: (id: string) => request<{ status: string }>(`/projects/${id}/cancel-analysis`, { method: 'POST' }),
  getSettings: (id: string) =>
    request<Record<string, unknown>>(`/projects/${id}/settings`),
  updateSettingsSection: (id: string, section: string, data: Record<string, unknown>) =>
    request<Record<string, unknown>>(`/projects/${id}/settings/${section}`, {
      method: 'PUT', body: JSON.stringify(data),
    }),
  rules: {
    get: (projectId: string) =>
      request<Record<string, string[]>>(`/projects/${projectId}/rules`),
    update: (projectId: string, rules: Record<string, string[]>) =>
      request<Record<string, string[]>>(`/projects/${projectId}/rules`, {
        method: 'PUT',
        body: JSON.stringify(rules),
      }),
  },
  debugContext: {
    get: (projectId: string) =>
      request<DebugContext>(`/projects/${projectId}/debug-context`),
    update: (projectId: string, data: DebugContext) =>
      request<DebugContext>(`/projects/${projectId}/debug-context`, {
        method: 'PUT',
        body: JSON.stringify(data),
      }),
  },
  buildSettings: {
    get: (projectId: string) =>
      request<{ build_commands: string[]; merge_method: string; require_merge_approval: boolean; require_plan_approval: boolean }>(
        `/projects/${projectId}/build-settings`,
      ),
    update: (projectId: string, data: { build_commands?: string[]; merge_method?: string; require_merge_approval?: boolean; require_plan_approval?: boolean }) =>
      request<{ build_commands: string[]; merge_method: string; require_merge_approval: boolean; require_plan_approval: boolean }>(
        `/projects/${projectId}/build-settings`,
        { method: 'PUT', body: JSON.stringify(data) },
      ),
  },
  releaseSettings: {
    get: (projectId: string) =>
      request<{ release_pipeline_enabled: boolean; release_configs: Record<string, ReleaseConfig>; repos: Array<{ name: string; repo_url: string }> }>(
        `/projects/${projectId}/release-settings`,
      ),
    update: (projectId: string, data: { release_pipeline_enabled?: boolean; release_configs?: Record<string, ReleaseConfig> }) =>
      request<{ release_pipeline_enabled: boolean; release_configs: Record<string, ReleaseConfig> }>(
        `/projects/${projectId}/release-settings`,
        { method: 'PUT', body: JSON.stringify(data) },
      ),
  },
  mergePipeline: {
    getSettings: (projectId: string) =>
      request<{ merge_pipelines: Record<string, MergePipelineConfig>; repos: Array<{ name: string; repo_url: string }> }>(
        `/projects/${projectId}/merge-pipeline-settings`,
      ),
    updateSettings: (projectId: string, data: { merge_pipelines: Record<string, MergePipelineConfig> }) =>
      request<{ merge_pipelines: Record<string, MergePipelineConfig> }>(
        `/projects/${projectId}/merge-pipeline-settings`,
        { method: 'PUT', body: JSON.stringify(data) },
      ),
    listRuns: (projectId: string) =>
      request<PipelineRun[]>(`/projects/${projectId}/pipeline-runs`),
    getRun: (projectId: string, runId: string) =>
      request<PipelineRun>(`/projects/${projectId}/pipeline-runs/${runId}`),
    cancelRun: (projectId: string, runId: string) =>
      request<{ status: string }>(`/projects/${projectId}/pipeline-runs/${runId}/cancel`, { method: 'POST' }),
    getVariables: (projectId: string) =>
      request<{ variables: PipelineVariable[] }>(`/projects/${projectId}/pipeline-variables`),
  },
  postMergeActions: {
    get: (projectId: string) =>
      request<{ post_merge_actions: Record<string, PostMergeRepoConfig>; repos: Array<{ name: string; repo_url: string }> }>(
        `/projects/${projectId}/post-merge-actions`,
      ),
    update: (projectId: string, data: { post_merge_actions: Record<string, PostMergeRepoConfig> }) =>
      request<{ post_merge_actions: Record<string, PostMergeRepoConfig> }>(
        `/projects/${projectId}/post-merge-actions`,
        { method: 'PUT', body: JSON.stringify(data) },
      ),
  },
  members: {
    list: (projectId: string) =>
      request<{ owner: ProjectMember; members: ProjectMember[] }>(
        `/projects/${projectId}/members`
      ),
    add: (projectId: string, email: string) =>
      request<ProjectMember>(
        `/projects/${projectId}/members`,
        { method: 'POST', body: JSON.stringify({ email }) },
      ),
    remove: (projectId: string, userId: string) =>
      request<void>(`/projects/${projectId}/members/${userId}`, { method: 'DELETE' }),
  },
  memories: {
    list: (projectId: string) =>
      request<ProjectMemory[]>(`/projects/${projectId}/memories`),
    update: (projectId: string, memoryId: string, data: { content?: string; category?: string; confidence?: number }) =>
      request<{ status: string }>(`/projects/${projectId}/memories/${memoryId}`, {
        method: 'PUT',
        body: JSON.stringify(data),
      }),
    delete: (projectId: string, memoryId: string) =>
      request<void>(`/projects/${projectId}/memories/${memoryId}`, { method: 'DELETE' }),
  },
}

// Todos
export const todos = {
  list: (projectId: string, params?: { state?: string; priority?: string }) => {
    const search = new URLSearchParams()
    if (params?.state) search.set('state', params.state)
    if (params?.priority) search.set('priority', params.priority)
    const qs = search.toString()
    return request<TodoItem[]>(`/projects/${projectId}/todos${qs ? `?${qs}` : ''}`)
  },
  create: (projectId: string, data: TodoCreatePayload) =>
    request<TodoItem>(`/projects/${projectId}/todos`, { method: 'POST', body: JSON.stringify(data) }),
  get: (id: string) => request<TodoItem>(`/todos/${id}`),
  update: (id: string, data: TodoUpdatePayload) =>
    request<TodoItem>(`/todos/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  cancel: (id: string) => request<TodoItem>(`/todos/${id}/cancel`, { method: 'POST' }),
  retry: (id: string, withContext?: boolean) =>
    request<TodoItem>(`/todos/${id}/retry`, {
      method: 'POST',
      body: JSON.stringify({ with_context: withContext ?? false }),
    }),
  triggerSubTask: (todoId: string, subTaskId: string, force: boolean = false) =>
    request<{ status: string; sub_task_id: string; force: boolean }>(
      `/todos/${todoId}/sub-tasks/${subTaskId}/trigger`,
      { method: 'POST', body: JSON.stringify({ force }) },
    ),
  injectSubtask: (todoId: string, subtaskId: string, content: string) =>
    request<{ status: string }>(`/todos/${todoId}/subtasks/${subtaskId}/inject`, {
      method: 'POST',
      body: JSON.stringify({ content }),
    }),
  acceptDeliverables: (id: string) =>
    request<TodoItem>(`/todos/${id}/accept-deliverables`, { method: 'POST' }),
  requestChanges: (id: string, feedback: string) =>
    request<TodoItem>(`/todos/${id}/request-changes`, {
      method: 'POST',
      body: JSON.stringify({ feedback }),
    }),
  approvePlan: (id: string) =>
    request<TodoItem>(`/todos/${id}/approve-plan`, { method: 'POST' }),
  rejectPlan: (id: string, feedback: string) =>
    request<TodoItem>(`/todos/${id}/reject-plan`, {
      method: 'POST',
      body: JSON.stringify({ feedback }),
    }),
  approveMerge: (id: string) =>
    request<{ status: string }>(`/todos/${id}/approve-merge`, { method: 'POST' }),
  rejectMerge: (id: string, feedback: string) =>
    request<{ status: string }>(`/todos/${id}/reject-merge`, {
      method: 'POST',
      body: JSON.stringify({ feedback }),
    }),
  approveRelease: (id: string) =>
    request<{ status: string }>(`/todos/${id}/approve-release`, { method: 'POST' }),
  rejectRelease: (id: string, feedback: string) =>
    request<{ status: string }>(`/todos/${id}/reject-release`, {
      method: 'POST',
      body: JSON.stringify({ feedback }),
    }),
  resume: (id: string) =>
    request<{ status: string }>(`/todos/${id}/resume`, { method: 'POST' }),
  workspace: {
    repos: (todoId: string) =>
      request<Array<{ name: string; label: string }>>(`/todos/${todoId}/workspace/repos`),
    tree: (todoId: string, repo?: string) => {
      const params = new URLSearchParams()
      if (repo) params.set('repo', repo)
      const qs = params.toString()
      return request<FileTreeNode[]>(`/todos/${todoId}/workspace/tree${qs ? `?${qs}` : ''}`)
    },
    file: (todoId: string, path: string, repo?: string) => {
      const params = new URLSearchParams({ path })
      if (repo) params.set('repo', repo)
      return request<FileContent>(`/todos/${todoId}/workspace/file?${params}`)
    },
    saveFile: (todoId: string, path: string, content: string, repo?: string) =>
      request<{ path: string; size: number; saved: boolean }>(`/todos/${todoId}/workspace/file`, {
        method: 'PUT',
        body: JSON.stringify({ path, content, repo: repo || undefined }),
      }),
    gitStatus: (todoId: string, repo?: string) => {
      const params = new URLSearchParams()
      if (repo) params.set('repo', repo)
      const qs = params.toString()
      return request<GitStatus>(`/todos/${todoId}/workspace/git/status${qs ? `?${qs}` : ''}`)
    },
    gitDiff: (todoId: string, staged?: boolean, path?: string, repo?: string) => {
      const params = new URLSearchParams()
      if (staged) params.set('staged', 'true')
      if (path) params.set('path', path)
      if (repo) params.set('repo', repo)
      const qs = params.toString()
      return request<{ diff: string; stats: string }>(`/todos/${todoId}/workspace/git/diff${qs ? `?${qs}` : ''}`)
    },
    taskDiff: (todoId: string, repo?: string) => {
      const params = new URLSearchParams()
      if (repo) params.set('repo', repo)
      const qs = params.toString()
      return request<{ diff: string; stats: string; files: Array<{ status: string; path: string }>; has_changes: boolean; base_commit?: string }>(
        `/todos/${todoId}/workspace/git/task-diff${qs ? `?${qs}` : ''}`,
      )
    },
    subtaskDiff: (todoId: string, subtaskId: string, repo?: string) => {
      const params = new URLSearchParams()
      if (repo) params.set('repo', repo)
      const qs = params.toString()
      return request<{ diff: string; stats: string; files: Array<{ status: string; path: string }>; has_changes: boolean; commit_hash?: string }>(
        `/todos/${todoId}/workspace/git/subtask-diff/${subtaskId}${qs ? `?${qs}` : ''}`,
      )
    },
    commits: (todoId: string, repo?: string) => {
      const params = new URLSearchParams()
      if (repo) params.set('repo', repo)
      const qs = params.toString()
      return request<Array<{ hash: string; author: string; date: string; message: string; files_changed: number; files: Array<{ status: string; path: string }> }>>(
        `/todos/${todoId}/workspace/git/commits${qs ? `?${qs}` : ''}`,
      )
    },
    commitDetail: (todoId: string, hash: string, repo?: string) => {
      const params = new URLSearchParams()
      if (repo) params.set('repo', repo)
      const qs = params.toString()
      return request<{ hash: string; message: string; diff: string; stats: string }>(
        `/todos/${todoId}/workspace/git/commit/${hash}${qs ? `?${qs}` : ''}`,
      )
    },
    gitAdd: (todoId: string, paths: string[], repo?: string) =>
      request<GitStatus>(`/todos/${todoId}/workspace/git/add`, {
        method: 'POST',
        body: JSON.stringify({ paths, repo: repo || undefined }),
      }),
    gitCommit: (todoId: string, message: string, repo?: string) =>
      request<{ hash: string; message: string; success: boolean }>(`/todos/${todoId}/workspace/git/commit`, {
        method: 'POST',
        body: JSON.stringify({ message, repo: repo || undefined }),
      }),
    gitPush: (todoId: string, repo?: string) =>
      request<{ success: boolean; output: string; branch: string }>(`/todos/${todoId}/workspace/git/push`, {
        method: 'POST',
        body: JSON.stringify({ repo: repo || undefined }),
      }),
  },
}

// Chat (todo-level)
export const chat = {
  history: (todoId: string) => request<ChatMessage[]>(`/todos/${todoId}/chat`),
  send: (todoId: string, content: string) =>
    request<ChatMessage>(`/todos/${todoId}/chat`, {
      method: 'POST',
      body: JSON.stringify({ content }),
    }),
}

// Project-level chat
export const projectChat = {
  // Legacy (no-session) endpoints
  history: (projectId: string) => request<ProjectChatMessage[]>(`/projects/${projectId}/chat`),
  send: (projectId: string, content: string, intent?: string) =>
    request<{
      user_message: ProjectChatMessage
      assistant_message: ProjectChatMessage
    }>(`/projects/${projectId}/chat`, {
      method: 'POST',
      body: JSON.stringify({ content, intent }),
    }),
  clear: (projectId: string) =>
    request<{ status: string }>(`/projects/${projectId}/chat`, { method: 'DELETE' }),
  deleteMessage: (projectId: string, messageId: string) =>
    request<{ status: string }>(`/projects/${projectId}/chat/${messageId}`, { method: 'DELETE' }),

  // Session endpoints
  sessions: {
    list: (projectId: string) =>
      request<ChatSession[]>(`/projects/${projectId}/chat/sessions`),
    create: (projectId: string, data: { title?: string; mode?: 'chat' | 'plan' }) =>
      request<ChatSession>(`/projects/${projectId}/chat/sessions`, {
        method: 'POST',
        body: JSON.stringify(data),
      }),
    togglePlanMode: (projectId: string, sessionId: string) =>
      request<{ plan_mode: boolean }>(
        `/projects/${projectId}/chat/sessions/${sessionId}/toggle-plan`,
        { method: 'POST' },
      ),
    setChatMode: (projectId: string, sessionId: string, mode: string) =>
      request<{ chat_mode: string; plan_mode: boolean }>(
        `/projects/${projectId}/chat/sessions/${sessionId}/mode`,
        { method: 'POST', body: JSON.stringify({ mode }) },
      ),
    get: (projectId: string, sessionId: string) =>
      request<ChatSession & { messages: ProjectChatMessage[] }>(`/projects/${projectId}/chat/sessions/${sessionId}`),
    update: (projectId: string, sessionId: string, data: { title: string }) =>
      request<ChatSession>(`/projects/${projectId}/chat/sessions/${sessionId}`, {
        method: 'PUT',
        body: JSON.stringify(data),
      }),
    delete: (projectId: string, sessionId: string) =>
      request<void>(`/projects/${projectId}/chat/sessions/${sessionId}`, { method: 'DELETE' }),
    acceptPlan: (projectId: string, sessionId: string) =>
      request<{ user_message: ProjectChatMessage; assistant_message: ProjectChatMessage }>(
        `/projects/${projectId}/chat/sessions/${sessionId}/accept-plan`,
        { method: 'POST' },
      ),
    acceptTaskPlan: (projectId: string, sessionId: string) =>
      request<{
        user_message: ProjectChatMessage
        assistant_message: ProjectChatMessage
        task_id: string
        existing_active_subtasks?: Array<{ id: string; title: string; agent_role: string; status: string }>
        old_todo_id?: string
      }>(
        `/projects/${projectId}/chat/sessions/${sessionId}/accept-task-plan`,
        { method: 'POST' },
      ),
    cancelSubtasks: (projectId: string, sessionId: string, subtaskIds: string[]) =>
      request<{ cancelled: string[]; todo_id: string }>(
        `/projects/${projectId}/chat/sessions/${sessionId}/cancel-subtasks`,
        { method: 'POST', body: JSON.stringify({ subtask_ids: subtaskIds }) },
      ),
    discardTaskPlan: (projectId: string, sessionId: string, feedback: string) =>
      request<{ status: string }>(
        `/projects/${projectId}/chat/sessions/${sessionId}/discard-task-plan`,
        { method: 'POST', body: JSON.stringify({ feedback }) },
      ),
  },
  sendInSession: (projectId: string, sessionId: string, content: string, intent?: string, model?: string) =>
    request<{ user_message: ProjectChatMessage; assistant_message: ProjectChatMessage; routing_mode?: string; mode_auto_switched?: boolean }>(
      `/projects/${projectId}/chat/sessions/${sessionId}/messages`,
      { method: 'POST', body: JSON.stringify({ content, intent, model: model || undefined }) },
    ),
  deleteSessionMessage: (projectId: string, sessionId: string, messageId: string) =>
    request<{ status: string }>(
      `/projects/${projectId}/chat/sessions/${sessionId}/messages/${messageId}`,
      { method: 'DELETE' },
    ),
  injectInSession: (projectId: string, sessionId: string, content: string) =>
    request<{ status: string }>(`/projects/${projectId}/chat/sessions/${sessionId}/inject`, {
      method: 'POST',
      body: JSON.stringify({ content }),
    }),
}

// Deliverables
export const deliverables = {
  list: (todoId: string) => request<Deliverable[]>(`/todos/${todoId}/deliverables`),
  get: (id: string) => request<Deliverable>(`/deliverables/${id}`),
  getDiff: (id: string) =>
    request<{ diff: string; stats: string; files: Array<{ status: string; path: string }> }>(
      `/deliverables/${id}/diff`
    ),
}

// Agent runs
export const agentRuns = {
  list: (todoId: string) => request<AgentRun[]>(`/todos/${todoId}/runs`),
}

// Providers
export const providers = {
  list: () => request<ProviderConfig[]>('/providers'),
  create: (data: ProviderCreatePayload) =>
    request<ProviderConfig>('/providers', { method: 'POST', body: JSON.stringify(data) }),
  update: (id: string, data: ProviderUpdatePayload) =>
    request<ProviderConfig>(`/providers/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  delete: (id: string) => request<void>(`/providers/${id}`, { method: 'DELETE' }),
  test: (id: string) => request<{ status: string; detail?: string }>(`/providers/${id}/test`, { method: 'POST' }),
  listModels: (id: string) =>
    request<{ provider_id: string; models: Array<{ id: string; name: string; is_default: boolean }> }>(`/providers/${id}/models`),
}

// Git Providers
export const gitProviders = {
  list: () => request<GitProviderConfig[]>('/config/git-providers'),
  create: (data: GitProviderPayload) =>
    request<GitProviderConfig>('/config/git-providers', { method: 'POST', body: JSON.stringify(data) }),
  update: (id: string, data: Partial<GitProviderPayload>) =>
    request<GitProviderConfig>(`/config/git-providers/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  delete: (id: string) => request<void>(`/config/git-providers/${id}`, { method: 'DELETE' }),
  test: (id: string) => request<{ status: string; detail?: string }>(`/config/git-providers/${id}/test`, { method: 'POST' }),
  listRepos: () => request<ProviderRepos[]>('/config/git-providers/repos'),
}

// Skills
export const skills = {
  list: () => request<Skill[]>('/config/skills'),
  create: (data: SkillPayload) =>
    request<Skill>('/config/skills', { method: 'POST', body: JSON.stringify(data) }),
  update: (id: string, data: Partial<SkillPayload>) =>
    request<Skill>(`/config/skills/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  delete: (id: string) => request<void>(`/config/skills/${id}`, { method: 'DELETE' }),
}

// MCP Servers
export const mcpServers = {
  list: () => request<McpServer[]>('/config/mcp-servers'),
  create: (data: McpServerPayload) =>
    request<McpServer>('/config/mcp-servers', { method: 'POST', body: JSON.stringify(data) }),
  update: (id: string, data: Partial<McpServerPayload>) =>
    request<McpServer>(`/config/mcp-servers/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  delete: (id: string) => request<void>(`/config/mcp-servers/${id}`, { method: 'DELETE' }),
  discoverTools: (id: string) =>
    request<{ status: string; detail?: string; tools: { name: string; description: string }[]; transport_updated?: string; url_updated?: string }>(
      `/config/mcp-servers/${id}/discover-tools`, { method: 'POST' }
    ),
  testConnection: (id: string) =>
    request<{ status: string; current_transport: string; current_url: string; probes: Record<string, unknown>[]; recommendation: { transport: string; url: string } | null }>(
      `/config/mcp-servers/${id}/test-connection`, { method: 'POST' }
    ),
}

// Project enablement
export const projectConfig = {
  getEnablement: (projectId: string) =>
    request<ProjectEnablement>(`/config/projects/${projectId}/enabled`),
  updateEnablement: (projectId: string, data: ProjectEnablement) =>
    request<ProjectEnablement>(`/config/projects/${projectId}/enabled`, { method: 'PUT', body: JSON.stringify(data) }),
}

// Agents
export const agents = {
  list: () =>
    request<{
      defaults: DefaultAgentInfo[]
      overrides: Record<string, AgentConfig>
      custom: AgentConfig[]
    }>('/config/agents'),
  listTools: () =>
    request<{
      builtin: AvailableTool[]
      mcp: AvailableTool[]
    }>('/config/agents/tools'),
  create: (data: AgentCreatePayload) =>
    request<AgentConfig>('/config/agents', { method: 'POST', body: JSON.stringify(data) }),
  createOverride: (role: string, data: AgentUpdatePayload) =>
    request<AgentConfig>(`/config/agents/defaults/${role}/override`, {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  update: (id: string, data: AgentUpdatePayload) =>
    request<AgentConfig>(`/config/agents/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  delete: (id: string) =>
    request<void>(`/config/agents/${id}`, { method: 'DELETE' }),
  chatHistory: () =>
    request<AgentChatMessage[]>('/config/agents/chat'),
  chatSend: (content: string) =>
    request<{ user_message: AgentChatMessage; assistant_message: AgentChatMessage }>('/config/agents/chat', {
      method: 'POST',
      body: JSON.stringify({ content }),
    }),
  chatClear: () =>
    request<{ status: string }>('/config/agents/chat', { method: 'DELETE' }),
  chatUndo: () =>
    request<{ status: string; action: string; agent_id: string }>('/config/agents/chat/undo', { method: 'DELETE' }),
}

// Notifications
export const notifications = {
  list: () => request<NotificationChannel[]>('/notifications/channels'),
  create: (data: NotificationChannelPayload) =>
    request<NotificationChannel>('/notifications/channels', { method: 'POST', body: JSON.stringify(data) }),
  update: (id: string, data: Partial<NotificationChannelPayload> & { is_active?: boolean }) =>
    request<NotificationChannel>(`/notifications/channels/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  delete: (id: string) => request<void>(`/notifications/channels/${id}`, { method: 'DELETE' }),
  test: (id: string) => request<{ status: string; detail?: string }>(`/notifications/channels/${id}/test`, { method: 'POST' }),
}

// Admin
export const admin = {
  todos: (state?: string) => {
    const qs = state ? `?state=${state}` : ''
    return request<TodoItem[]>(`/admin/todos${qs}`)
  },
  users: () => request<{ id: string; email: string; display_name: string; role: string; created_at: string }[]>('/admin/users'),
  updateUserRole: (userId: string, role: string) =>
    request<{ id: string; email: string; display_name: string; role: string }>(
      `/admin/users/${userId}/role`,
      { method: 'PUT', body: JSON.stringify({ role }) },
    ),
  stats: () => request<Record<string, number>>('/admin/stats'),
  auditLog: (limit = 100) => request<Record<string, unknown>[]>(`/admin/audit-log?limit=${limit}`),
  testEmail: () => request<{ status: string; detail?: string }>('/admin/settings/email/test', { method: 'POST' }),
  getSetting: (key: string) => request<Record<string, unknown>>(`/admin/settings/${key}`),
  putSetting: (key: string, value: Record<string, string>) =>
    request<Record<string, unknown>>(`/admin/settings/${key}`, { method: 'PUT', body: JSON.stringify({ value_json: value }) }),
}
