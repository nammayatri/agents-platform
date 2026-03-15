export interface User {
  id: string
  email: string
  display_name: string
  role: 'user' | 'admin'
  avatar_url?: string
}

export interface ProjectDependency {
  name: string
  repo_url?: string
  description?: string
  git_provider_id?: string
}

export type GitProviderType = 'github' | 'gitlab' | 'bitbucket' | 'custom'

export interface WorkRules {
  coding?: string[]
  testing?: string[]
  review?: string[]
  quality?: string[]
  general?: string[]
}

export interface Project {
  id: string
  owner_id: string
  name: string
  description?: string
  repo_url?: string
  default_branch: string
  ai_provider_id?: string
  context_docs?: ProjectDependency[]
  git_provider_id?: string
  workspace_path?: string
  icon_url?: string
  user_role?: 'owner' | 'member'
  settings_json?: {
    analysis_status?: 'analyzing' | 'complete' | 'failed' | 'no_docs'
    work_rules?: WorkRules
    build_commands?: string[]
    merge_method?: 'merge' | 'squash' | 'rebase'
    project_understanding?: {
      summary?: string
      purpose?: string
      tech_stack?: string[]
      [key: string]: unknown
    }
  }
  created_at: string
  updated_at: string
}

export interface ProjectMember {
  id: string
  email: string
  display_name: string
  avatar_url?: string
  role: 'owner' | 'member'
  added_at?: string
}

export type TodoState = 'scheduled' | 'intake' | 'planning' | 'plan_ready' | 'in_progress' | 'review' | 'completed' | 'failed' | 'cancelled'

export interface IterationLogEntry {
  iteration: number
  timestamp: string
  action: string
  outcome: string
  error_output?: string | null
  learnings: string[]
  files_changed?: string[]
  stuck_check?: { stuck: boolean; pattern?: string; advice?: string } | null
  tokens_used: number
}

export interface SubTask {
  id: string
  todo_id: string
  parent_id?: string
  title: string
  description?: string
  agent_role: string
  execution_order: number
  status: 'pending' | 'assigned' | 'running' | 'completed' | 'failed'
  progress_pct: number
  progress_message?: string
  error_message?: string
  iteration_log?: IterationLogEntry[]
  review_loop?: boolean
  review_chain_id?: string
  review_verdict?: 'approved' | 'needs_changes'
  output_result?: {
    verdict?: 'approved' | 'needs_changes'
    approved?: boolean
    matches_plan?: boolean
    issues?: Array<{
      severity: 'critical' | 'major' | 'minor' | 'nit'
      description: string
      suggestion?: string
    }>
    summary?: string
    needs_human_review?: boolean
    content?: string
    raw_content?: string
    [key: string]: unknown
  }
  target_repo?: {
    repo_url: string
    name: string
    default_branch: string
    git_provider_id?: string | null
  }
  created_at: string
  completed_at?: string
}

export interface ProgressLogEntry {
  sub_task_id: string
  sub_task_title: string
  iterations_used: number
  outcome: string
  key_learnings: string[]
  completed_at: string
}

export interface TodoItem {
  id: string
  project_id: string
  creator_id: string
  title: string
  description?: string
  priority: 'critical' | 'high' | 'medium' | 'low'
  labels: string[]
  task_type: 'code' | 'research' | 'document' | 'general'
  intake_data?: Record<string, unknown>
  plan_json?: PlanData
  ai_provider_id?: string
  state: TodoState
  sub_state?: string
  state_changed_at: string
  retry_count: number
  error_message?: string
  result_summary?: string
  actual_tokens: number
  cost_usd: number
  rules_override_json?: WorkRules
  progress_log?: ProgressLogEntry[]
  max_iterations?: number
  created_at: string
  updated_at: string
  scheduled_at?: string
  completed_at?: string
  sub_tasks?: SubTask[]
  // Enriched provider info from backend
  provider_name?: string
  provider_model?: string
  provider_type?: string
}

export interface ChatMessage {
  id: string
  todo_id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  agent_run_id?: string
  created_at: string
}

export interface Deliverable {
  id: string
  todo_id: string
  agent_run_id?: string
  sub_task_id?: string
  type: 'pull_request' | 'report' | 'code_diff' | 'document' | 'test_results'
  title: string
  content_md?: string
  content_json?: Record<string, unknown>
  pr_url?: string
  pr_number?: number
  pr_state?: string
  branch_name?: string
  status: 'pending' | 'approved' | 'rejected' | 'needs_revision'
  reviewer_notes?: string
  merged_at?: string
  merge_method?: string
  target_repo_name?: string
  created_at: string
}

export interface AgentRun {
  id: string
  todo_id: string
  sub_task_id?: string
  agent_role: string
  agent_model: string
  provider_type: string
  status: 'running' | 'completed' | 'failed' | 'cancelled'
  progress_pct: number
  progress_message?: string
  tokens_input: number
  tokens_output: number
  duration_ms: number
  cost_usd: number
  error_type?: string
  error_detail?: string
  started_at: string
  completed_at?: string
}

export interface Skill {
  id: string
  owner_id: string
  name: string
  description?: string
  prompt: string
  category: string
  is_active: boolean
  created_at: string
  updated_at: string
}

export interface McpTool {
  name: string
  description: string
  input_schema?: Record<string, unknown>
}

export interface McpServer {
  id: string
  owner_id: string
  name: string
  description?: string
  command: string
  args: string[]
  env_json: Record<string, string>
  transport: 'stdio' | 'sse' | 'streamable-http'
  url?: string
  tools_json?: McpTool[]
  is_active: boolean
  created_at: string
  updated_at: string
}

export interface ProjectEnablement {
  disabled_skill_ids: string[]
  disabled_mcp_server_ids: string[]
  disabled_provider_ids: string[]
}

export interface GitProviderConfig {
  id: string
  owner_id: string
  provider_type: GitProviderType
  display_name: string
  api_base_url?: string
  has_token: boolean
  is_active: boolean
  created_at: string
  updated_at: string
}

export interface ProviderConfig {
  id: string
  owner_id?: string
  provider_type: 'anthropic' | 'openai' | 'self_hosted'
  display_name: string
  api_base_url?: string
  default_model: string
  fast_model?: string
  max_tokens: number
  temperature: number
  is_active: boolean
}

export interface ProgressUpdate {
  type: 'progress'
  sub_task_id: string
  progress_pct: number
  message: string
}

export interface PlanSubTask {
  title: string
  description: string
  agent_role: string
  execution_order: number
  depends_on: number[]
}

export interface PlanData {
  summary: string
  sub_tasks: PlanSubTask[]
  estimated_tokens?: number
}

export interface ChatSession {
  id: string
  project_id: string
  user_id: string
  title: string
  mode: 'chat' | 'plan'
  plan_mode: boolean
  plan_json?: Record<string, unknown>
  is_active: boolean
  created_at: string
  updated_at: string
}

export interface AgentConfig {
  id: string
  owner_id: string
  name: string
  role: string
  description?: string
  system_prompt: string
  model_preference?: string
  tools_enabled: string[]
  is_active: boolean
  created_at: string
  updated_at: string
}

export interface DefaultAgentInfo {
  role: string
  name: string
  description: string
  system_prompt: string
  default_tools: string[]
  default_model: string | null
  is_default: true
}

export interface AvailableTool {
  name: string
  description: string
  category: 'builtin' | 'mcp'
  server_name?: string
}

export interface AgentChatMessage {
  id: string
  user_id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  metadata_json?: {
    action?: string
    agent_id?: string
    agent_name?: string
  }
  created_at: string
}

export interface NotificationChannel {
  id: string
  user_id: string
  channel_type: 'email' | 'slack' | 'webhook'
  display_name: string
  config_json: Record<string, string>
  is_active: boolean
  notify_on: string[]
  created_at: string
}

export interface PlanTaskSubtask {
  title: string
  description?: string
  agent_role: string
  depends_on?: number[]
  parallel?: boolean
}

export interface PlanTask {
  title: string
  description?: string
  priority?: string
  task_type?: string
  subtasks?: PlanTaskSubtask[]
}

export interface ChatPlanData {
  action?: string
  plan_title?: string
  tasks: PlanTask[]
}

export interface ProjectChatMessage {
  id: string
  project_id: string
  user_id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  metadata_json?: {
    action?: string
    task_id?: string
    task_title?: string
    intent?: string
    plan_data?: ChatPlanData
    plan_title?: string
    task_count?: number
    tasks_created?: number
    task_ids?: string[]
  }
  created_at: string
}

export interface WSEvent {
  type: 'state_change' | 'chat_message' | 'progress' | 'deliverable_created' | 'ping'
  state?: TodoState
  message?: { role: string; content: string; id?: string }
  sub_task_id?: string
  progress_pct?: number
}

// ── API Payload Types ──────────────────────────────────────────────

export interface ProjectCreatePayload {
  name: string
  description?: string
  repo_url?: string
  default_branch?: string
  ai_provider_id?: string
  context_docs?: ProjectDependency[]
  git_provider_id?: string
  icon_url?: string
}

export interface ProjectUpdatePayload {
  name?: string
  description?: string
  repo_url?: string
  default_branch?: string
  ai_provider_id?: string
  context_docs?: ProjectDependency[]
  git_provider_id?: string
  icon_url?: string
  settings_json?: Record<string, unknown>
}

export interface TodoCreatePayload {
  title: string
  description?: string
  priority?: string
  task_type?: string
  labels?: string[]
  ai_provider_id?: string
  scheduled_at?: string
  rules_override_json?: WorkRules
  max_iterations?: number
}

export interface TodoUpdatePayload {
  title?: string
  description?: string
  priority?: string
  labels?: string[]
}

export interface ProviderCreatePayload {
  provider_type: string
  display_name: string
  api_key?: string
  api_base_url?: string
  default_model: string
  fast_model?: string
}

export interface ProviderUpdatePayload {
  provider_type?: string
  display_name?: string
  api_key?: string
  api_base_url?: string
  default_model?: string
  fast_model?: string
}

export interface GitProviderPayload {
  provider_type: string
  display_name: string
  api_base_url?: string
  token?: string
}

export interface SkillPayload {
  name: string
  description?: string
  prompt: string
  category?: string
}

export interface McpServerPayload {
  name: string
  description?: string
  command: string
  args?: string[]
  env_json?: Record<string, string>
  transport?: 'stdio' | 'sse' | 'streamable-http'
  url?: string
}

export interface NotificationChannelPayload {
  channel_type: string
  display_name: string
  config_json: Record<string, string>
  notify_on?: string[]
}

export interface AgentCreatePayload {
  name: string
  role: string
  description?: string
  system_prompt: string
  model_preference?: string
  tools_enabled?: string[]
}

export interface AgentUpdatePayload {
  name?: string
  description?: string
  system_prompt?: string
  model_preference?: string
  tools_enabled?: string[]
  is_active?: boolean
}
