export interface User {
  id: string
  email: string
  display_name: string
  role: 'user' | 'admin'
  avatar_url?: string
}

export interface DebugLogSource {
  service_name: string
  log_path?: string
  log_command?: string
  description?: string
}

export interface DebugMcpHint {
  mcp_server_name: string
  available_data?: string[]
  example_queries?: string[]
  notes?: string
}

export interface DebugContext {
  log_sources?: DebugLogSource[]
  mcp_hints?: DebugMcpHint[]
  custom_instructions?: string
}

export interface ProjectDependency {
  name: string
  repo_url?: string
  description?: string
  git_provider_id?: string
  debug_context?: DebugContext
}

export interface DepUnderstanding {
  purpose: string
  architecture: string
  tech_stack: string[]
  key_patterns: string[]
  api_surface: string
  exports?: string[]
  important_context: string[]
  summary: string
  raw?: boolean
}

export interface LinkingIntegration {
  source_repo: string
  target_repo: string
  pattern: string
  shared_interfaces: string[]
  data_flow: string
}

export interface LinkingDocument {
  overview: string
  integrations: LinkingIntegration[]
  shared_types: string[]
  architecture_diagram_text?: string
}

export interface IndexMetadata {
  main: { indexed?: boolean; has_repo_map?: boolean }
  deps: Record<string, { indexed?: boolean; has_repo_map?: boolean }>
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
  architect_editor_enabled?: boolean
  architect_model?: string
  editor_model?: string
  user_role?: 'owner' | 'member'
  settings_json?: {
    analysis_status?: 'analyzing' | 'complete' | 'failed' | 'no_docs'
    work_rules?: WorkRules
    build_commands?: string[]
    merge_method?: 'merge' | 'squash' | 'rebase'
    require_merge_approval?: boolean
    require_plan_approval?: boolean
    debug_context?: DebugContext
    project_understanding?: {
      summary?: string
      purpose?: string
      tech_stack?: string[]
      [key: string]: unknown
    }
    dep_understandings?: Record<string, DepUnderstanding>
    linking_document?: LinkingDocument
    index_metadata?: IndexMetadata
    release_pipeline_enabled?: boolean
    release_config?: ReleaseConfig
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

export type TodoState = 'scheduled' | 'intake' | 'planning' | 'plan_ready' | 'in_progress' | 'testing' | 'review' | 'completed' | 'failed' | 'cancelled'

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
  llm_response?: string | null
}

export interface SubTask {
  id: string
  todo_id: string
  parent_id?: string
  title: string
  description?: string
  agent_role: string
  execution_order: number
  depends_on?: string[]  // UUID[] of sub-task IDs this task depends on
  status: 'pending' | 'assigned' | 'running' | 'completed' | 'failed' | 'cancelled'
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
      file?: string
      line?: number | null
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
  input_context?: {
    relevant_files?: string[]
    current_state?: string
    what_to_change?: string
    patterns_to_follow?: string
    related_code?: string
    integration_points?: string
    [key: string]: unknown
  }
  execution_events?: ExecutionEvent[]
  commit_hash?: string
  created_at: string
  started_at?: string
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
  ai_model?: string
  state: TodoState
  sub_state?: string
  state_changed_at: string
  retried_at?: string
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
  chat_session_id?: string
  base_commit?: string
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
  metadata_json?: Record<string, unknown>
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
  owner_id: string | null
  provider_type: GitProviderType
  display_name: string
  api_base_url?: string
  has_token: boolean
  is_active: boolean
  is_shared?: boolean
  created_at: string
  updated_at: string
}

export interface ProviderConfig {
  id: string
  owner_id?: string
  provider_type: 'anthropic' | 'openai' | 'self_hosted' | 'claude_code'
  display_name: string
  api_base_url?: string
  default_model: string
  fast_model?: string
  max_tokens: number
  temperature: number
  is_active: boolean
  extra_config?: Record<string, unknown>
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
  review_loop?: boolean
  target_repo?: string
  context?: {
    relevant_files?: string[]
    current_state?: string
    what_to_change?: string
    patterns_to_follow?: string
    related_code?: string
    integration_points?: string
  }
}

export interface PlanData {
  summary: string
  sub_tasks: PlanSubTask[]
  estimated_tokens?: number
}

export type ChatMode = 'auto' | 'chat' | 'plan' | 'debug' | 'create_task'

export interface ChatSession {
  id: string
  project_id: string
  user_id: string
  title: string
  mode: 'chat' | 'plan'
  plan_mode: boolean
  chat_mode?: ChatMode
  last_routing_mode?: string
  plan_json?: Record<string, unknown>
  ai_model?: string
  linked_todo_id?: string
  is_active: boolean
  creator_name?: string
  creator_avatar_url?: string | null
  created_at: string
  updated_at: string
}

export interface ModelInfo {
  id: string
  name: string
  is_default: boolean
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
    proposed_config?: {
      name: string
      role: string
      description?: string
      system_prompt: string
      tools_enabled: string[]
    }
    previous_state?: Record<string, unknown>
    undone?: boolean
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
  agent_role: string
  depends_on?: number[]
  target_repo?: string
  review_loop?: boolean
  // Structured description fields
  scope?: string
  requirements?: string
  approach?: string
  goal?: string
  context?: string
  // Legacy fields (backward compat with old plans)
  description?: string
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
  // New format: singular task
  task?: PlanTask
  // Legacy format: tasks array (backward compat)
  tasks?: PlanTask[]
}

export interface ChatExecutionInfo {
  tool_calls?: { name: string; result_preview?: string }[]
  rounds?: number
  total_tokens_in?: number
  total_tokens_out?: number
  model?: string
  stop_reason?: string
}

export interface ProjectChatMessage {
  id: string
  project_id: string
  user_id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  sender_name?: string
  sender_avatar_url?: string | null
  metadata_json?: {
    action?: string
    task_id?: string
    task_title?: string
    intent?: string
    plan_data?: ChatPlanData & {
      summary?: string
      sub_tasks?: Array<{
        title: string
        agent_role: string
        description?: string
        execution_order?: number
        depends_on?: number[]
        review_loop?: boolean
        target_repo?: string
        scope?: string
        requirements?: string
        approach?: string
        goal?: string
        context?: string
      }>
    }
    plan_title?: string
    task_count?: number
    tasks_created?: number
    task_ids?: string[]
    execution?: ChatExecutionInfo
    raw_output?: string
    routing_mode?: string
    mode_auto_switched?: boolean
    // Review verdict metadata (plan_review_verdict / code_review_verdict)
    verdict?: 'approved' | 'needs_changes'
    approved?: boolean
    feedback?: string
    issues?: Array<{
      severity: 'critical' | 'major' | 'minor' | 'nit'
      file?: string
      line?: number | null
      description: string
      suggestion?: string
    }>
    subtask_title?: string
    summary?: string
    iteration?: number
    [key: string]: unknown
  }
  created_at: string
}

export interface WSEvent {
  type: 'state_change' | 'subtask_update' | 'chat_message' | 'progress' | 'activity' | 'deliverable_created' | 'task_cancelled' | 'llm_response' | 'workspace_commit' | 'workspace_push' | 'tool_start' | 'tool_result' | 'llm_thinking' | 'iteration_start' | 'iteration_end' | 'testing_step' | 'user_inject' | 'index_search' | 'index_build' | 'ping'
  state?: TodoState
  status?: string
  message?: string | { role: string; content: string; id?: string; metadata_json?: Record<string, unknown> }
  sub_task_id?: string
  progress_pct?: number
  activity?: string
  content?: string
  preview?: string
  iteration?: number
  error_message?: string
  sub_state?: string
  phase?: string
  ts?: number
  // Streaming execution event fields
  name?: string
  args_summary?: string
  result_preview?: string
  chars?: number
  tokens_in?: number
  tokens_out?: number
  round?: number
  tool_index?: number
  total_tools?: number
  subtask?: string
  // File/tool detail fields
  file_path?: string
  pattern?: string
  error?: boolean
  // Testing phase event fields
  command?: string
  // Index event fields
  query?: string
  results_count?: number
  top_score?: number
  latency_ms?: number
  source?: string       // 'cache' | 'disk' | 'cold_build' | 'error'
  has_repo_map?: boolean
  repo_map_chars?: number
  from_base?: boolean
}

// ── API Payload Types ──────────────────────────────────────────────

// ── Release Pipeline Types ────────────────────────────────────────

export interface ReleaseEndpointConfig {
  enabled: boolean
  api_url?: string
  http_method?: string
  headers?: Record<string, string>
  body_template?: string
  success_status_codes?: number[]
  poll_status_url?: string
  poll_success_value?: string
  require_approval?: boolean
}

export interface BuildProviderConfig {
  workflow_name?: string      // GitHub Actions
  job_url?: string            // Jenkins
  token?: string              // Jenkins auth token
  timeout_minutes?: number
  poll_interval_seconds?: number
}

export interface ReleaseConfig {
  build_provider: 'github_actions' | 'jenkins'
  build_config?: BuildProviderConfig
  test_release?: ReleaseEndpointConfig
  prod_release?: ReleaseEndpointConfig
}

// ── Merge Pipeline Types ──────────────────────────────────────────────

export type PipelineRunStatus =
  | 'pending' | 'testing' | 'test_passed' | 'test_failed'
  | 'deploying' | 'deploy_success' | 'deploy_failed'
  | 'skipped' | 'cancelled'

export interface PipelineRun {
  id: string
  project_id: string
  repo_name: string
  pr_number: number
  pr_title?: string
  branch_name?: string
  commit_hash?: string
  repo_url?: string
  status: PipelineRunStatus
  test_mode?: 'webhook' | 'poll'
  test_config?: Record<string, unknown>
  test_result?: { passed: boolean; output?: unknown; received_at?: string }
  deploy_config?: Record<string, unknown>
  deploy_result?: Record<string, unknown>
  webhook_token?: string
  started_at?: string
  test_completed_at?: string
  deploy_completed_at?: string
  created_at: string
  updated_at: string
}

export interface MergePipelineTestConfig {
  mode: 'poll' | 'webhook'
  poll_url?: string
  poll_interval_seconds?: number
  poll_timeout_minutes?: number
  poll_success_value?: string
  poll_headers?: Record<string, string>
  timeout_minutes?: number
}

export interface MergePipelineDeployConfig {
  enabled: boolean
  deploy_type: 'http' | 'kubernetes'
  api_url?: string
  http_method?: string
  headers?: Record<string, string>
  body_template?: string
  success_status_codes?: number[]
  kube_commands?: string[]
  kube_context?: string
}

export interface MergePipelineConfig {
  enabled: boolean
  test_config?: MergePipelineTestConfig
  deploy_config?: MergePipelineDeployConfig
}

export interface PipelineVariable {
  key: string
  example: string
}

// ── Post-Merge Actions ──────────────────────────────────────────────

export interface PostMergeAction {
  type: 'webhook' | 'script'
  url?: string
  method?: string
  headers?: Record<string, string>
  body_template?: string
  command?: string
  timeout_seconds?: number
}

export interface PostMergeRepoConfig {
  enabled: boolean
  actions?: PostMergeAction[]
}


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
  architect_editor_enabled?: boolean
  architect_model?: string
  editor_model?: string
}

export interface TodoCreatePayload {
  title: string
  description?: string
  priority?: string
  task_type?: string
  labels?: string[]
  ai_provider_id?: string
  ai_model?: string
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
  extra_config?: Record<string, unknown>
}

export interface ProviderUpdatePayload {
  provider_type?: string
  display_name?: string
  api_key?: string
  api_base_url?: string
  default_model?: string
  fast_model?: string
  extra_config?: Record<string, unknown>
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

// ── Workspace IDE Types ──────────────────────────────────────────────

export interface FileTreeNode {
  name: string
  path: string
  type: 'file' | 'dir'
  size?: number
  children?: FileTreeNode[]
}

export interface FileContent {
  path: string
  content: string
  size: number
  language: string
  binary: boolean
  truncated?: boolean
}

export interface GitFileStatus {
  path: string
  status: string
  staged: boolean
}

export interface GitStatus {
  branch: string
  files: GitFileStatus[]
  clean: boolean
}

// ── Project Memories ──────────────────────────────────────────────

export interface ProjectMemory {
  id: string
  project_id: string
  category: 'architecture' | 'pattern' | 'convention' | 'pitfall' | 'dependency' | 'build' | 'workflow'
  content: string
  source_todo_id?: string
  confidence: number
  created_at: string
  updated_at: string
}

// ── Execution Events ──────────────────────────────────────────────

export interface ExecutionEvent {
  type: 'iteration_start' | 'tool_start' | 'tool_result' | 'llm_thinking' | 'iteration_end' | 'activity' | 'index_search' | 'index_build'
  timestamp: number
  ts?: number          // Unix epoch from backend (seconds)
  iteration?: number
  subtask?: string
  sub_task_id?: string
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
  content?: string       // LLM reasoning text (for llm_thinking events)
  // File/tool detail fields
  file_path?: string
  pattern?: string
  command?: string
  error?: boolean
  // Index event fields
  query?: string
  results_count?: number
  top_score?: number
  latency_ms?: number
  source?: string
  has_repo_map?: boolean
  repo_map_chars?: number
  from_base?: boolean
}

export interface RepoInfo {
  name: string
  full_name: string
  clone_url: string
  html_url: string
  default_branch: string
  private: boolean
  description?: string
}

export interface ProviderRepos {
  provider_id: string
  provider_name: string
  provider_type: string
  repos: RepoInfo[]
  error?: string
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
