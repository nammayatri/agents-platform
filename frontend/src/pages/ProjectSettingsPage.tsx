import { useEffect, useState, useCallback, useRef } from 'react'
import { useParams, useNavigate, useSearchParams } from 'react-router-dom'
import { projects as projectsApi, providers as providersApi, gitProviders as gitProvidersApi, skills as skillsApi, mcpServers as mcpApi, projectConfig as projectConfigApi } from '../services/api'
import type { Project, ProjectDependency, ProjectMember, ProviderConfig, GitProviderConfig, Skill, McpServer, WorkRules } from '../types'

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
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const [userRole, setUserRole] = useState<'owner' | 'member'>('owner')
  const [members, setMembers] = useState<ProjectMember[]>([])
  const [memberOwner, setMemberOwner] = useState<ProjectMember | null>(null)
  const [workRules, setWorkRules] = useState<WorkRules>({})
  const [buildCommands, setBuildCommands] = useState<string[]>([])
  const [mergeMethod, setMergeMethod] = useState<'merge' | 'squash' | 'rebase'>('squash')

  const editTabs = ['General', 'Repository', 'Dependencies', 'Rules', 'Build & Merge', 'Capabilities', 'Members', 'Understanding', 'Agents']
  const activeTab = searchParams.get('tab') || editTabs[0]
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

  useEffect(() => {
    if (analysisStatus === 'analyzing' && projectId) {
      pollRef.current = setInterval(async () => {
        try {
          const p = (await projectsApi.get(projectId)) as Project
          const settings = typeof p.settings_json === 'string' ? JSON.parse(p.settings_json) : p.settings_json
          const status = settings?.analysis_status || null
          setAnalysisStatus(status)
          if (status === 'complete' || status === 'failed' || status === 'no_docs') {
            setProjectUnderstanding((settings?.project_understanding as ProjectUnderstanding) || null)
            if (pollRef.current) clearInterval(pollRef.current)
          }
        } catch { /* ignore */ }
      }, 3000)
    }
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [analysisStatus, projectId])

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
        <div className="animate-pulse space-y-4">
          <div className="h-6 bg-gray-800 rounded w-48" />
          <div className="h-4 bg-gray-800 rounded w-72" />
          <div className="h-10 bg-gray-900 rounded" />
          <div className="h-10 bg-gray-900 rounded" />
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
    <div className="p-6 max-w-2xl mx-auto">
      <div className="mb-6">
        <h1 className="text-xl font-semibold text-white">Project Settings</h1>
        <p className="text-sm text-gray-500 mt-1">{name}</p>
      </div>

      {/* Tabs */}
      <div className="flex gap-1 mb-6 border-b border-gray-900 -mx-1">
        {editTabs.map((tab) => (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            className={`px-3 py-2 text-sm font-medium transition-colors border-b-2 -mb-px ${
              activeTab === tab
                ? 'text-white border-indigo-500'
                : 'text-gray-500 border-transparent hover:text-gray-300'
            }`}
          >
            {tab}
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
            setError={setError}
          />
        )}

        {activeTab === 'Agents' && <ProjectAgentsTab />}

        {/* Error */}
        {error && (
          <div className="px-4 py-2.5 bg-red-500/10 border border-red-500/20 rounded-lg text-red-400 text-sm">{error}</div>
        )}

        {/* Actions */}
        {activeTab !== 'Understanding' && activeTab !== 'Agents' && activeTab !== 'Members' && activeTab !== 'Rules' && activeTab !== 'Build & Merge' && (
          <div className="flex items-center gap-3 pt-2">
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
