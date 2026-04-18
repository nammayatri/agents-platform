import { useEffect, useState, useCallback } from 'react'
import { useParams, useNavigate, useSearchParams } from 'react-router-dom'
import { projects as projectsApi, providers as providersApi, gitProviders as gitProvidersApi, skills as skillsApi, mcpServers as mcpApi, projectConfig as projectConfigApi } from '../services/api'
import {
  FileText, GitBranch, Link as LinkIcon, BookOpen, Hammer,
  Bug, Zap, Users, Brain, Bot, Settings as SettingsIcon,
  ChevronRight, Code2, Target, Shield, Webhook,
} from 'lucide-react'
import { InlineError } from '../components/ui/InlineError'
import type { Project, ProjectDependency, ProjectMember, ProviderConfig, GitProviderConfig, Skill, McpServer, WorkRules, ProjectMemory } from '../types'
import type { LucideIcon } from 'lucide-react'

import ProjectWizard from '../components/project/ProjectWizard'
import ProjectGeneralTab from '../components/project/ProjectGeneralTab'
import ProjectRepoTab from '../components/project/ProjectRepoTab'
import ProjectDepsTab from '../components/project/ProjectDepsTab'
import ProjectRulesTab from '../components/project/ProjectRulesTab'
import ProjectBuildMergeTab from '../components/project/ProjectBuildMergeTab'
import ProjectCapabilitiesTab from '../components/project/ProjectCapabilitiesTab'
import ProjectMembersTab from '../components/project/ProjectMembersTab'
import ProjectUnderstandingTab from '../components/project/ProjectUnderstandingTab'
import ProjectAgentsTab from '../components/project/ProjectAgentsTab'
import ProjectDebugTab from '../components/project/ProjectDebugTab'
import ProjectReleaseTab from '../components/project/ProjectReleaseTab'
import ProjectMergePipelineTab from '../components/project/ProjectMergePipelineTab'
import ProjectPlanningTab from '../components/project/ProjectPlanningTab'
import ProjectSettingsJsonEditor from '../components/project/ProjectSettingsJsonEditor'

interface ProjectUnderstanding {
  summary?: string
  purpose?: string
  architecture?: string
  tech_stack?: string[]
  key_patterns?: string[]
  dependency_map?: { name: string; role: string }[]
  api_surface?: string
  testing_approach?: string
  important_context?: string[]
}

// ── Sidebar section definitions ──

interface SidebarItem {
  key: string
  label: string
  Icon: LucideIcon
}

interface SidebarGroup {
  label: string
  Icon: LucideIcon
  items: SidebarItem[]
}

const sidebarGroups: SidebarGroup[] = [
  {
    label: 'General',
    Icon: FileText,
    items: [
      { key: 'profile', label: 'Profile', Icon: FileText },
      { key: 'repository', label: 'Repository', Icon: GitBranch },
    ],
  },
  {
    label: 'Planning & Execution',
    Icon: Target,
    items: [
      { key: 'planning', label: 'Planning', Icon: Target },
      { key: 'rules', label: 'Work Rules', Icon: BookOpen },
      { key: 'agents', label: 'Agents', Icon: Bot },
    ],
  },
  {
    label: 'Code & Git',
    Icon: Code2,
    items: [
      { key: 'build', label: 'Build & Quality', Icon: Hammer },
      { key: 'merge', label: 'Merge', Icon: GitBranch },
      { key: 'release', label: 'Release Webhooks', Icon: Webhook },
    ],
  },
  {
    label: 'Context',
    Icon: Brain,
    items: [
      { key: 'dependencies', label: 'Dependencies', Icon: LinkIcon },
      { key: 'understanding', label: 'Understanding', Icon: Brain },
      { key: 'debugging', label: 'Debugging', Icon: Bug },
    ],
  },
  {
    label: 'Access',
    Icon: Shield,
    items: [
      { key: 'members', label: 'Members', Icon: Users },
      { key: 'capabilities', label: 'Capabilities', Icon: Zap },
    ],
  },
]

export default function ProjectSettingsPage() {
  const { projectId } = useParams<{ projectId: string }>()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const isNew = !projectId

  const [loading, setLoading] = useState(!isNew)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [repoUrl, setRepoUrl] = useState('')
  const [defaultBranch, setDefaultBranch] = useState('main')
  const [aiProviderId, setAiProviderId] = useState('')
  const [dependencies, setDependencies] = useState<ProjectDependency[]>([])
  const [iconUrl, setIconUrl] = useState('')
  const [gitProviderId, setGitProviderId] = useState('')
  const [providerList, setProviderList] = useState<ProviderConfig[]>([])
  const [gitProviderList, setGitProviderList] = useState<GitProviderConfig[]>([])
  const [skillList, setSkillList] = useState<Skill[]>([])
  const [mcpList, setMcpList] = useState<McpServer[]>([])
  const [disabledSkillIds, setDisabledSkillIds] = useState<Set<string>>(new Set())
  const [disabledMcpIds, setDisabledMcpIds] = useState<Set<string>>(new Set())
  const [disabledProviderIds, setDisabledProviderIds] = useState<Set<string>>(new Set())
  const [analysisStatus, setAnalysisStatus] = useState<string | null>(null)
  const [projectUnderstanding, setProjectUnderstanding] = useState<ProjectUnderstanding | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [userRole, setUserRole] = useState<'owner' | 'member'>('owner')
  const [members, setMembers] = useState<ProjectMember[]>([])
  const [memberOwner, setMemberOwner] = useState<ProjectMember | null>(null)
  const [workRules, setWorkRules] = useState<WorkRules>({})
  const [buildCommands, setBuildCommands] = useState<string[]>([])
  const [mergeMethod, setMergeMethod] = useState<'merge' | 'squash' | 'rebase'>('squash')
  const [requireMergeApproval, setRequireMergeApproval] = useState(false)
  const [requirePlanApproval, setRequirePlanApproval] = useState(false)
  const [memories, setMemories] = useState<ProjectMemory[]>([])
  const [memoriesLoading, setMemoriesLoading] = useState(false)
  const [architectEditorEnabled, setArchitectEditorEnabled] = useState(false)
  const [architectModel, setArchitectModel] = useState('')
  const [editorModel, setEditorModel] = useState('')
  const [providerModels, setProviderModels] = useState<{id: string; name: string}[]>([])
  const [planningGuidelines, setPlanningGuidelines] = useState('')
  const [showJsonEditor, setShowJsonEditor] = useState(false)
  const [rawSettingsJson, setRawSettingsJson] = useState<Record<string, unknown> | null>(null)

  // Section from URL (preserves on refresh)
  const activeSection = searchParams.get('section') || 'profile'
  const setActiveSection = (s: string) => setSearchParams({ section: s })

  const loadProject = useCallback(async () => {
    if (!projectId) return
    try {
      const p = (await projectsApi.get(projectId)) as Project
      setName(p.name)
      setDescription(p.description || '')
      setRepoUrl(p.repo_url || '')
      setDefaultBranch(p.default_branch || 'main')
      setAiProviderId(p.ai_provider_id || '')
      const docs = typeof p.context_docs === 'string' ? JSON.parse(p.context_docs) : p.context_docs
      setDependencies(docs || [])
      setIconUrl(p.icon_url || '')
      setGitProviderId(p.git_provider_id || '')
      setUserRole(p.user_role || 'owner')
      const settings = typeof p.settings_json === 'string' ? JSON.parse(p.settings_json) : (p.settings_json || {})
      setRawSettingsJson(settings)

      // New namespaced format
      const planning = settings.planning || {}
      setPlanningGuidelines(planning.guidelines || '')
      setRequirePlanApproval(planning.require_approval ?? settings.require_plan_approval ?? false)

      const execution = settings.execution || {}
      setWorkRules(execution.work_rules || settings.work_rules || {})
      const ae = execution.architect_editor || {}
      setArchitectEditorEnabled(ae.enabled ?? p.architect_editor_enabled ?? false)
      setArchitectModel(ae.architect_model || p.architect_model || '')
      setEditorModel(ae.editor_model || p.editor_model || '')

      const git = settings.git || {}
      setBuildCommands(git.build_commands || settings.build_commands || [])
      setMergeMethod(git.merge_method || settings.merge_method || 'squash')
      setRequireMergeApproval(git.require_merge_approval ?? settings.require_merge_approval ?? false)

      const understanding = settings.understanding || {}
      setAnalysisStatus(understanding.status || settings.analysis_status || null)
      setProjectUnderstanding(understanding.project || settings.project_understanding || null)

      try {
        const m = await projectsApi.members.list(projectId!)
        setMemberOwner(m.owner as ProjectMember)
        setMembers(m.members as ProjectMember[])
      } catch { /* ignore */ }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load project')
    } finally {
      setLoading(false)
    }
  }, [projectId])

  useEffect(() => {
    providersApi.list().then((p) => setProviderList(p as ProviderConfig[])).catch(() => {})
    gitProvidersApi.list().then((g) => setGitProviderList(g as GitProviderConfig[])).catch(() => {})
    skillsApi.list().then((s) => setSkillList(s as Skill[])).catch(() => {})
    mcpApi.list().then((m) => setMcpList(m as McpServer[])).catch(() => {})
    loadProject()
    if (projectId) {
      projectConfigApi.getEnablement(projectId).then((e) => {
        setDisabledSkillIds(new Set(e.disabled_skill_ids))
        setDisabledMcpIds(new Set(e.disabled_mcp_server_ids))
        setDisabledProviderIds(new Set(e.disabled_provider_ids))
      }).catch(() => {})
    }
  }, [loadProject, projectId])

  // Load memories lazily
  useEffect(() => {
    if (activeSection === 'agents' && projectId && memories.length === 0 && !memoriesLoading) {
      setMemoriesLoading(true)
      projectsApi.memories.list(projectId).then((m) => {
        setMemories(m as ProjectMemory[])
      }).catch(() => {}).finally(() => setMemoriesLoading(false))
    }
  }, [activeSection, projectId]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (aiProviderId) {
      providersApi.listModels(aiProviderId).then((resp) => {
        const r = resp as { models?: {id: string; name: string}[] }
        setProviderModels(r.models || [])
      }).catch(() => {})
    }
  }, [aiProviderId])

  const handleSave = async () => {
    if (!name.trim()) { setError('Project name is required'); return }
    setSaving(true)
    setError('')
    try {
      const validDeps = dependencies.filter((d) => d.name.trim())
      const data = {
        name: name.trim(),
        description: description.trim() || undefined,
        repo_url: repoUrl.trim() || undefined,
        default_branch: defaultBranch.trim() || 'main',
        ai_provider_id: aiProviderId || undefined,
        architect_editor_enabled: architectEditorEnabled,
        architect_model: architectModel || undefined,
        editor_model: editorModel || undefined,
        context_docs: validDeps.length > 0 ? validDeps : undefined,
        git_provider_id: gitProviderId || undefined,
        icon_url: iconUrl.trim() || undefined,
      }
      await projectsApi.update(projectId!, data)
      await projectConfigApi.updateEnablement(projectId!, {
        disabled_skill_ids: Array.from(disabledSkillIds),
        disabled_mcp_server_ids: Array.from(disabledMcpIds),
        disabled_provider_ids: Array.from(disabledProviderIds),
      })
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to save project')
    } finally {
      setSaving(false)
    }
  }

  const activeProviders = providerList.filter((p) => p.is_active)

  if (loading) {
    return (
      <div className="p-6 max-w-4xl mx-auto">
        <div className="space-y-4">
          <div className="h-6 skeleton w-48" />
          <div className="h-4 skeleton w-72" />
          <div className="flex gap-6 mt-6">
            <div className="w-52 space-y-2">
              {[1,2,3,4,5].map(i => <div key={i} className="h-8 skeleton rounded" />)}
            </div>
            <div className="flex-1 space-y-3">
              <div className="h-10 skeleton" />
              <div className="h-10 skeleton" />
              <div className="h-10 skeleton w-3/4" />
            </div>
          </div>
        </div>
      </div>
    )
  }

  if (isNew) {
    return (
      <ProjectWizard
        onComplete={(id) => navigate(`/projects/${id}`)}
        providers={providerList}
        gitProviders={gitProviderList}
      />
    )
  }

  // Determine which group is expanded based on active section
  const activeGroupIdx = sidebarGroups.findIndex(g =>
    g.items.some(i => i.key === activeSection)
  )

  return (
    <div className="p-4 md:p-6 max-w-5xl mx-auto">
      {/* Header */}
      <div className="mb-6 flex items-center justify-between">
        <div>
          <div className="flex items-center gap-2.5">
            <SettingsIcon className="w-5 h-5 text-gray-500" />
            <h1 className="text-xl font-semibold text-white">Project Settings</h1>
          </div>
          <p className="text-sm text-gray-500 mt-1">{name}</p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setShowJsonEditor(!showJsonEditor)}
            className="px-3 py-1.5 text-xs text-gray-500 hover:text-gray-300 bg-gray-900 border border-gray-800 hover:border-gray-700 rounded-lg transition-colors font-mono"
          >
            {showJsonEditor ? 'Form View' : '{ } JSON'}
          </button>
          <button
            onClick={() => navigate(`/projects/${projectId}`)}
            className="px-3 py-1.5 text-xs text-gray-500 hover:text-gray-300 transition-colors"
          >
            Back
          </button>
        </div>
      </div>

      {showJsonEditor ? (
        <ProjectSettingsJsonEditor
          projectId={projectId!}
          settingsJson={rawSettingsJson}
          onSaved={(updated) => {
            setRawSettingsJson(updated)
            loadProject()
          }}
          setError={setError}
        />
      ) : (
        <div className="flex flex-col md:flex-row gap-4 md:gap-6 min-h-[400px] md:min-h-[600px]">
          {/* Sidebar — horizontal scroll on mobile, vertical on desktop */}
          <nav className="md:w-52 md:shrink-0">
            <div className="flex md:flex-col gap-1 overflow-x-auto md:overflow-x-visible pb-2 md:pb-0">
              {sidebarGroups.map((group, gi) => {
                const isExpanded = gi === activeGroupIdx
                return (
                  <div key={group.label} className="shrink-0 md:shrink">
                    <button
                      onClick={() => {
                        if (!isExpanded) {
                          setActiveSection(group.items[0].key)
                        }
                      }}
                      className={`w-full flex items-center gap-2 px-3 py-2 rounded-lg text-xs font-medium transition-colors ${
                        isExpanded
                          ? 'text-white bg-gray-900/50'
                          : 'text-gray-500 hover:text-gray-300 hover:bg-gray-900/30'
                      }`}
                    >
                      <group.Icon className="w-3.5 h-3.5" />
                      <span className="flex-1 text-left">{group.label}</span>
                      <ChevronRight className={`w-3 h-3 transition-transform ${isExpanded ? 'rotate-90' : ''}`} />
                    </button>
                    {isExpanded && (
                      <div className="hidden md:block ml-3 mt-0.5 space-y-0.5 border-l border-gray-800 pl-2">
                        {group.items.map((item) => (
                          <button
                            key={item.key}
                            onClick={() => setActiveSection(item.key)}
                            className={`w-full flex items-center gap-2 px-2.5 py-1.5 rounded text-xs transition-colors ${
                              activeSection === item.key
                                ? 'text-indigo-400 bg-indigo-500/10'
                                : 'text-gray-500 hover:text-gray-300'
                            }`}
                          >
                            <item.Icon className="w-3 h-3" />
                            {item.label}
                          </button>
                        ))}
                      </div>
                    )}
                    {/* Mobile: show sub-items inline when expanded */}
                    {isExpanded && (
                      <div className="flex md:hidden gap-1 mt-1">
                        {group.items.map((item) => (
                          <button
                            key={item.key}
                            onClick={() => setActiveSection(item.key)}
                            className={`px-2 py-1 rounded text-[11px] whitespace-nowrap transition-colors ${
                              activeSection === item.key
                                ? 'text-indigo-400 bg-indigo-500/10'
                                : 'text-gray-500'
                            }`}
                          >
                            {item.label}
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>

            {/* Delete at bottom — desktop only, on mobile it's in the content area */}
            {userRole === 'owner' && (
              <div className="hidden md:block mt-8 pt-4 border-t border-gray-800">
                <button
                  onClick={async () => {
                    if (confirm('Delete this project and all its tasks?')) {
                      await projectsApi.delete(projectId!)
                      navigate('/')
                    }
                  }}
                  className="w-full px-3 py-2 text-xs text-red-400/60 hover:text-red-400 hover:bg-red-500/5 rounded-lg transition-colors text-left"
                >
                  Delete Project
                </button>
              </div>
            )}
          </nav>

          {/* Content */}
          <div className="flex-1 min-w-0">
            <div className="space-y-6">
              {activeSection === 'profile' && (
                <ProjectGeneralTab
                  name={name} setName={setName}
                  description={description} setDescription={setDescription}
                  iconUrl={iconUrl} setIconUrl={setIconUrl}
                  aiProviderId={aiProviderId} setAiProviderId={setAiProviderId}
                  activeProviders={activeProviders}
                />
              )}

              {activeSection === 'repository' && (
                <ProjectRepoTab
                  repoUrl={repoUrl} setRepoUrl={setRepoUrl}
                  defaultBranch={defaultBranch} setDefaultBranch={setDefaultBranch}
                  gitProviderId={gitProviderId} setGitProviderId={setGitProviderId}
                  gitProviderList={gitProviderList}
                />
              )}

              {activeSection === 'planning' && (
                <ProjectPlanningTab
                  projectId={projectId!}
                  planningGuidelines={planningGuidelines}
                  setPlanningGuidelines={setPlanningGuidelines}
                  requirePlanApproval={requirePlanApproval}
                  setRequirePlanApproval={setRequirePlanApproval}
                  setError={setError}
                />
              )}

              {activeSection === 'rules' && (
                <ProjectRulesTab
                  projectId={projectId!}
                  workRules={workRules} setWorkRules={setWorkRules}
                  setError={setError}
                />
              )}

              {activeSection === 'agents' && (
                <div className="space-y-6">
                  <ProjectAgentsTab />

                  {/* Architect/Editor Split */}
                  <div className="pt-4 border-t border-gray-800">
                    <h3 className="text-sm font-medium text-gray-300 mb-1">Architect / Editor Split</h3>
                    <p className="text-xs text-gray-600 mb-4">Use a powerful model for analysis and a fast model for code changes.</p>
                    <label className="flex items-center gap-3 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={architectEditorEnabled}
                        onChange={(e) => setArchitectEditorEnabled(e.target.checked)}
                        disabled={userRole !== 'owner'}
                        className="w-4 h-4 rounded bg-gray-900 border-gray-700 text-indigo-500 focus:ring-indigo-500"
                      />
                      <span className="text-sm text-gray-300">Enable dual-model execution</span>
                    </label>
                    {architectEditorEnabled && (
                      <div className="mt-4 space-y-3 pl-7">
                        <div>
                          <label className="text-xs text-gray-500 mb-1 block">Architect Model (reasoning)</label>
                          <select value={architectModel} onChange={(e) => setArchitectModel(e.target.value)} disabled={userRole !== 'owner'}
                            className="w-full px-3 py-2 bg-gray-900 border border-gray-800 rounded-lg text-white text-sm focus:outline-none focus:border-indigo-500 transition-colors">
                            <option value="">Use provider default</option>
                            {providerModels.map(m => <option key={m.id} value={m.id}>{m.name}</option>)}
                          </select>
                        </div>
                        <div>
                          <label className="text-xs text-gray-500 mb-1 block">Editor Model (fast writes)</label>
                          <select value={editorModel} onChange={(e) => setEditorModel(e.target.value)} disabled={userRole !== 'owner'}
                            className="w-full px-3 py-2 bg-gray-900 border border-gray-800 rounded-lg text-white text-sm focus:outline-none focus:border-indigo-500 transition-colors">
                            <option value="">Use provider default</option>
                            {providerModels.map(m => <option key={m.id} value={m.id}>{m.name}</option>)}
                          </select>
                        </div>
                      </div>
                    )}
                  </div>

                  {/* Memories */}
                  <div className="pt-4 border-t border-gray-800">
                    <h3 className="text-sm font-medium text-gray-300 mb-1">Project Memories</h3>
                    <p className="text-xs text-gray-600 mb-3">Learnings extracted from completed tasks, injected into agent context.</p>
                    {memoriesLoading && (
                      <div className="animate-pulse space-y-2">
                        {[1,2,3].map(i => <div key={i} className="h-16 bg-gray-800 rounded-lg" />)}
                      </div>
                    )}
                    {!memoriesLoading && memories.length === 0 && (
                      <div className="py-4 text-center text-xs text-gray-600 border border-dashed border-gray-800 rounded-lg">
                        No memories yet.
                      </div>
                    )}
                    {!memoriesLoading && memories.map((mem) => (
                      <div key={mem.id} className="px-3 py-2.5 bg-gray-900 border border-gray-800 rounded-lg group mb-1.5">
                        <div className="flex items-center gap-2 mb-1">
                          <span className={`px-2 py-0.5 rounded text-[10px] font-medium ${
                            mem.category === 'architecture' ? 'bg-purple-500/10 text-purple-400' :
                            mem.category === 'pattern' ? 'bg-blue-500/10 text-blue-400' :
                            mem.category === 'pitfall' ? 'bg-red-500/10 text-red-400' :
                            mem.category === 'build' ? 'bg-amber-500/10 text-amber-400' :
                            'bg-emerald-500/10 text-emerald-400'
                          }`}>{mem.category}</span>
                          <span className="text-[10px] text-gray-700 ml-auto">{new Date(mem.created_at).toLocaleDateString()}</span>
                          {userRole === 'owner' && (
                            <button
                              onClick={async () => {
                                if (confirm('Delete this memory?')) {
                                  await projectsApi.memories.delete(projectId!, mem.id)
                                  setMemories(memories.filter(m => m.id !== mem.id))
                                }
                              }}
                              className="text-red-400/60 hover:text-red-400 text-[11px] hidden group-hover:inline transition-colors"
                            >delete</button>
                          )}
                        </div>
                        <p className="text-sm text-gray-400 leading-relaxed">{mem.content}</p>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {activeSection === 'build' && (
                <ProjectBuildMergeTab
                  projectId={projectId!}
                  buildCommands={buildCommands} setBuildCommands={setBuildCommands}
                  mergeMethod={mergeMethod} setMergeMethod={setMergeMethod}
                  requireMergeApproval={requireMergeApproval} setRequireMergeApproval={setRequireMergeApproval}
                  setError={setError}
                />
              )}

              {activeSection === 'merge' && (
                <ProjectMergePipelineTab
                  projectId={projectId!}
                  userRole={userRole}
                  setError={setError}
                />
              )}

              {activeSection === 'release' && (
                <ProjectReleaseTab
                  projectId={projectId!}
                  userRole={userRole}
                  setError={setError}
                />
              )}

              {activeSection === 'dependencies' && (
                <ProjectDepsTab
                  dependencies={dependencies} setDependencies={setDependencies}
                  gitProviderList={gitProviderList}
                />
              )}

              {activeSection === 'understanding' && (
                <ProjectUnderstandingTab
                  projectId={projectId!}
                  repoUrl={repoUrl}
                  analysisStatus={analysisStatus}
                  projectUnderstanding={projectUnderstanding}
                  setAnalysisStatus={setAnalysisStatus}
                  setProjectUnderstanding={setProjectUnderstanding}
                  setError={setError}
                />
              )}

              {activeSection === 'debugging' && (
                <ProjectDebugTab
                  projectId={projectId!}
                  setError={setError}
                />
              )}

              {activeSection === 'members' && (
                <ProjectMembersTab
                  projectId={projectId!}
                  userRole={userRole}
                  memberOwner={memberOwner}
                  members={members} setMembers={setMembers}
                />
              )}

              {activeSection === 'capabilities' && (
                <ProjectCapabilitiesTab
                  activeProviders={activeProviders}
                  skillList={skillList} mcpList={mcpList}
                  disabledProviderIds={disabledProviderIds} setDisabledProviderIds={setDisabledProviderIds}
                  disabledSkillIds={disabledSkillIds} setDisabledSkillIds={setDisabledSkillIds}
                  disabledMcpIds={disabledMcpIds} setDisabledMcpIds={setDisabledMcpIds}
                />
              )}

              {/* Error */}
              {error && <InlineError message={error} onDismiss={() => setError('')} />}

              {/* Save button — shown for sections that need it */}
              {['profile', 'repository', 'dependencies', 'capabilities', 'agents'].includes(activeSection) && userRole === 'owner' && (
                <div className="pt-4 border-t border-gray-800">
                  <button
                    onClick={handleSave}
                    disabled={saving}
                    className="px-5 py-2 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 rounded-lg text-sm font-medium text-white transition-colors"
                  >
                    {saving ? 'Saving...' : 'Save Changes'}
                  </button>
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
