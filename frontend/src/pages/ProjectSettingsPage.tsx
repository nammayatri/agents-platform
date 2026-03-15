import { useEffect, useState, useCallback } from 'react'
import { useParams, useNavigate, useSearchParams } from 'react-router-dom'
import { projects as projectsApi, providers as providersApi, gitProviders as gitProvidersApi, skills as skillsApi, mcpServers as mcpApi, projectConfig as projectConfigApi } from '../services/api'
import { FileText, GitBranch, Link as LinkIcon, BookOpen, Hammer, Rocket, Bug, Zap, Users, Brain, Bot, Database, Settings as SettingsIcon } from 'lucide-react'
import { tabBar, tabBtn } from '../styles/classes'
import { InlineError } from '../components/ui/InlineError'
import type { Project, ProjectDependency, ProjectMember, ProviderConfig, GitProviderConfig, Skill, McpServer, WorkRules, ProjectMemory, ReleaseConfig } from '../types'
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
  const [releaseEnabled, setReleaseEnabled] = useState(false)
  const [releaseConfig, setReleaseConfig] = useState<ReleaseConfig>({ build_provider: 'github_actions' })
  const [architectEditorEnabled, setArchitectEditorEnabled] = useState(false)
  const [architectModel, setArchitectModel] = useState('')
  const [editorModel, setEditorModel] = useState('')
  const [providerModels, setProviderModels] = useState<{id: string; name: string}[]>([])

  const editTabs: { key: string; label: string; Icon: LucideIcon }[] = [
    { key: 'General', label: 'General', Icon: FileText },
    { key: 'Repository', label: 'Repository', Icon: GitBranch },
    { key: 'Dependencies', label: 'Dependencies', Icon: LinkIcon },
    { key: 'Rules', label: 'Rules', Icon: BookOpen },
    { key: 'Build & Merge', label: 'Build & Merge', Icon: Hammer },
    { key: 'Release', label: 'Release', Icon: Rocket },
    { key: 'Debug', label: 'Debug', Icon: Bug },
    { key: 'Capabilities', label: 'Capabilities', Icon: Zap },
    { key: 'Members', label: 'Members', Icon: Users },
    { key: 'Understanding', label: 'Understanding', Icon: Brain },
    { key: 'Agents', label: 'Agents', Icon: Bot },
    { key: 'Memories', label: 'Memories', Icon: Database },
  ]
  const activeTab = searchParams.get('tab') || editTabs[0].key
  const setActiveTab = (t: string) => setSearchParams({ tab: t })

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
      const settings = typeof p.settings_json === 'string' ? JSON.parse(p.settings_json) : p.settings_json
      setAnalysisStatus(settings?.analysis_status || null)
      setProjectUnderstanding((settings?.project_understanding as ProjectUnderstanding) || null)
      setWorkRules((settings?.work_rules as WorkRules) || {})
      setBuildCommands((settings?.build_commands as string[]) || [])
      setMergeMethod((settings?.merge_method as 'merge' | 'squash' | 'rebase') || 'squash')
      setRequireMergeApproval(!!settings?.require_merge_approval)
      setRequirePlanApproval(!!settings?.require_plan_approval)
      setReleaseEnabled(!!settings?.release_pipeline_enabled)
      setReleaseConfig(settings?.release_config as ReleaseConfig || { build_provider: 'github_actions' })
      setArchitectEditorEnabled(!!p.architect_editor_enabled)
      setArchitectModel(p.architect_model || '')
      setEditorModel(p.editor_model || '')
      try {
        const m = await projectsApi.members.list(projectId!)
        setMemberOwner(m.owner as ProjectMember)
        setMembers(m.members as ProjectMember[])
      } catch { /* ignore if not loaded */ }
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

  // Load memories when Memories tab is selected
  useEffect(() => {
    if (activeTab === 'Memories' && projectId && memories.length === 0 && !memoriesLoading) {
      setMemoriesLoading(true)
      projectsApi.memories.list(projectId).then((m) => {
        setMemories(m as ProjectMemory[])
      }).catch(() => {}).finally(() => setMemoriesLoading(false))
    }
  }, [activeTab, projectId]) // eslint-disable-line react-hooks/exhaustive-deps

  // Load provider models for architect/editor dropdowns
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
      navigate(`/projects/${projectId}`)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to save project')
    } finally {
      setSaving(false)
    }
  }

  const activeProviders = providerList.filter((p) => p.is_active)

  if (loading) {
    return (
      <div className="p-6 max-w-2xl mx-auto">
        <div className="space-y-4">
          <div className="h-6 skeleton w-48" />
          <div className="h-4 skeleton w-72" />
          <div className="flex gap-2 mt-4 mb-6 border-b border-gray-800 pb-2">
            {[1,2,3,4,5].map(i => <div key={i} className="h-5 skeleton w-20" />)}
          </div>
          <div className="h-10 skeleton" />
          <div className="h-10 skeleton" />
          <div className="h-10 skeleton w-3/4" />
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

  return (
    <div className="p-4 md:p-6 max-w-2xl mx-auto">
      <div className="mb-6 animate-fade-in">
        <div className="flex items-center gap-2.5">
          <SettingsIcon className="w-5 h-5 text-gray-500" />
          <h1 className="text-xl font-semibold text-white">Project Settings</h1>
        </div>
        <p className="text-sm text-gray-500 mt-1">{name}</p>
      </div>

      {/* Tabs */}
      <div className={`${tabBar} mb-6`}>
        {editTabs.map((tab) => (
          <button
            key={tab.key}
            onClick={() => setActiveTab(tab.key)}
            className={tabBtn(activeTab === tab.key)}
          >
            <tab.Icon className="w-3.5 h-3.5" />
            {tab.label}
          </button>
        ))}
      </div>

      <div className="space-y-6">
        {activeTab === 'General' && (
          <ProjectGeneralTab
            name={name} setName={setName}
            description={description} setDescription={setDescription}
            iconUrl={iconUrl} setIconUrl={setIconUrl}
            aiProviderId={aiProviderId} setAiProviderId={setAiProviderId}
            activeProviders={activeProviders}
          />
        )}

        {activeTab === 'Repository' && (
          <ProjectRepoTab
            repoUrl={repoUrl} setRepoUrl={setRepoUrl}
            defaultBranch={defaultBranch} setDefaultBranch={setDefaultBranch}
            gitProviderId={gitProviderId} setGitProviderId={setGitProviderId}
            gitProviderList={gitProviderList}
          />
        )}

        {activeTab === 'Dependencies' && (
          <ProjectDepsTab
            dependencies={dependencies} setDependencies={setDependencies}
            gitProviderList={gitProviderList}
          />
        )}

        {activeTab === 'Rules' && (
          <ProjectRulesTab
            projectId={projectId!}
            workRules={workRules} setWorkRules={setWorkRules}
            setError={setError}
          />
        )}

        {activeTab === 'Build & Merge' && (
          <ProjectBuildMergeTab
            projectId={projectId!}
            buildCommands={buildCommands} setBuildCommands={setBuildCommands}
            mergeMethod={mergeMethod} setMergeMethod={setMergeMethod}
            requireMergeApproval={requireMergeApproval} setRequireMergeApproval={setRequireMergeApproval}
            requirePlanApproval={requirePlanApproval} setRequirePlanApproval={setRequirePlanApproval}
            setError={setError}
          />
        )}

        {activeTab === 'Release' && (
          <ProjectReleaseTab
            projectId={projectId!}
            releaseEnabled={releaseEnabled} setReleaseEnabled={setReleaseEnabled}
            releaseConfig={releaseConfig} setReleaseConfig={setReleaseConfig}
            setError={setError}
          />
        )}

        {activeTab === 'Debug' && (
          <ProjectDebugTab
            projectId={projectId!}
            setError={setError}
          />
        )}

        {activeTab === 'Capabilities' && (
          <ProjectCapabilitiesTab
            activeProviders={activeProviders}
            skillList={skillList} mcpList={mcpList}
            disabledProviderIds={disabledProviderIds} setDisabledProviderIds={setDisabledProviderIds}
            disabledSkillIds={disabledSkillIds} setDisabledSkillIds={setDisabledSkillIds}
            disabledMcpIds={disabledMcpIds} setDisabledMcpIds={setDisabledMcpIds}
          />
        )}

        {activeTab === 'Members' && (
          <ProjectMembersTab
            projectId={projectId!}
            userRole={userRole}
            memberOwner={memberOwner}
            members={members} setMembers={setMembers}
          />
        )}

        {activeTab === 'Understanding' && (
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

        {activeTab === 'Agents' && <ProjectAgentsTab />}

        {activeTab === 'Memories' && (
          <div className="space-y-4">
            <div>
              <h3 className="text-sm font-medium text-gray-300 mb-1">Project Memories</h3>
              <p className="text-xs text-gray-600">Learnings extracted from completed tasks. These are automatically injected into agent context for future tasks.</p>
            </div>
            {memoriesLoading && (
              <div className="animate-pulse space-y-2">
                {[1,2,3].map(i => <div key={i} className="h-16 bg-gray-800 rounded-lg" />)}
              </div>
            )}
            {!memoriesLoading && memories.length === 0 && (
              <div className="py-6 text-center text-sm text-gray-600 border border-dashed border-gray-800 rounded-lg">
                No memories yet. Memories are automatically extracted when tasks complete.
              </div>
            )}
            {!memoriesLoading && memories.map((mem) => (
              <div key={mem.id} className="px-4 py-3 bg-gray-900 border border-gray-800 rounded-lg group">
                <div className="flex items-center gap-2 mb-1.5">
                  <span className={`px-2 py-0.5 rounded text-[10px] font-medium ${
                    mem.category === 'architecture' ? 'bg-purple-500/10 text-purple-400' :
                    mem.category === 'pattern' ? 'bg-blue-500/10 text-blue-400' :
                    mem.category === 'convention' ? 'bg-cyan-500/10 text-cyan-400' :
                    mem.category === 'pitfall' ? 'bg-red-500/10 text-red-400' :
                    'bg-emerald-500/10 text-emerald-400'
                  }`}>
                    {mem.category}
                  </span>
                  <span className="text-[10px] text-gray-600">
                    confidence: {(mem.confidence * 100).toFixed(0)}%
                  </span>
                  <span className="text-[10px] text-gray-700 ml-auto">
                    {new Date(mem.created_at).toLocaleDateString()}
                  </span>
                  {userRole === 'owner' && (
                    <button
                      onClick={async () => {
                        if (confirm('Delete this memory?')) {
                          await projectsApi.memories.delete(projectId!, mem.id)
                          setMemories(memories.filter(m => m.id !== mem.id))
                        }
                      }}
                      className="text-red-400/60 hover:text-red-400 text-[11px] hidden group-hover:inline transition-colors"
                    >
                      delete
                    </button>
                  )}
                </div>
                <p className="text-sm text-gray-400 leading-relaxed">{mem.content}</p>
              </div>
            ))}

            {/* Architect/Editor Split Settings */}
            <div className="mt-8 pt-6 border-t border-gray-800">
              <h3 className="text-sm font-medium text-gray-300 mb-1">Architect / Editor Split</h3>
              <p className="text-xs text-gray-600 mb-4">Use a powerful model for analysis and planning, and a fast model for applying code changes.</p>

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
                    <select
                      value={architectModel}
                      onChange={(e) => setArchitectModel(e.target.value)}
                      disabled={userRole !== 'owner'}
                      className="w-full px-3 py-2 bg-gray-900 border border-gray-800 rounded-lg text-white text-sm focus:outline-none focus:border-indigo-500 transition-colors"
                    >
                      <option value="">Use provider default</option>
                      {providerModels.map(m => (
                        <option key={m.id} value={m.id}>{m.name}</option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label className="text-xs text-gray-500 mb-1 block">Editor Model (fast writes)</label>
                    <select
                      value={editorModel}
                      onChange={(e) => setEditorModel(e.target.value)}
                      disabled={userRole !== 'owner'}
                      className="w-full px-3 py-2 bg-gray-900 border border-gray-800 rounded-lg text-white text-sm focus:outline-none focus:border-indigo-500 transition-colors"
                    >
                      <option value="">Use provider default</option>
                      {providerModels.map(m => (
                        <option key={m.id} value={m.id}>{m.name}</option>
                      ))}
                    </select>
                  </div>
                </div>
              )}
            </div>
          </div>
        )}

        {/* Error */}
        {error && <InlineError message={error} onDismiss={() => setError('')} />}

        {/* Actions */}
        {activeTab !== 'Understanding' && activeTab !== 'Agents' && activeTab !== 'Members' && activeTab !== 'Rules' && activeTab !== 'Build & Merge' && activeTab !== 'Release' && activeTab !== 'Debug' && (
          <div className="sticky bottom-0 bg-gray-950/80 backdrop-blur-sm border-t border-gray-800 py-3 -mx-6 px-6 flex items-center gap-3 flex-wrap">
            {userRole === 'owner' && (
              <button onClick={handleSave} disabled={saving} className="px-5 py-2 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 rounded-lg text-sm font-medium text-white transition-colors">
                {saving ? 'Saving...' : 'Save Changes'}
              </button>
            )}
            <button onClick={() => navigate(`/projects/${projectId}`)} className="px-5 py-2 bg-gray-800 hover:bg-gray-700 rounded-lg text-sm text-gray-300 transition-colors">
              {userRole === 'owner' ? 'Cancel' : 'Back'}
            </button>
            {userRole === 'owner' && (
              <button
                onClick={async () => {
                  if (confirm('Delete this project and all its tasks?')) {
                    await projectsApi.delete(projectId!)
                    navigate('/')
                  }
                }}
                className="ml-auto px-4 py-2 text-sm text-red-400 hover:text-red-300 transition-colors"
              >
                Delete
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
