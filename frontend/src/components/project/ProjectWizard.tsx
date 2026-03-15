import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { projects as projectsApi } from '../../services/api'
import type { Project, ProjectDependency, ProviderConfig, GitProviderConfig } from '../../types'

import ProjectGeneralTab from './ProjectGeneralTab'
import ProjectRepoTab from './ProjectRepoTab'
import ProjectDepsTab from './ProjectDepsTab'

interface ProjectWizardProps {
  onComplete: (projectId: string) => void
  providers: ProviderConfig[]
  gitProviders: GitProviderConfig[]
}

export default function ProjectWizard({ onComplete, providers, gitProviders }: ProjectWizardProps) {
  const navigate = useNavigate()

  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [iconUrl, setIconUrl] = useState('')
  const [aiProviderId, setAiProviderId] = useState('')
  const [repoUrl, setRepoUrl] = useState('')
  const [defaultBranch, setDefaultBranch] = useState('main')
  const [gitProviderId, setGitProviderId] = useState('')
  const [dependencies, setDependencies] = useState<ProjectDependency[]>([])
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  const [wizardStep, setWizardStep] = useState(0)
  const wizardSteps = ['Project', 'Repository', 'Dependencies', 'Review']

  const activeProviders = providers.filter((p) => p.is_active)

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
      const created = (await projectsApi.create(data)) as Project
      onComplete(created.id)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to save project')
    } finally {
      setSaving(false)
    }
  }

  const canNext = wizardStep === 0 ? name.trim().length > 0 : true

  return (
    <div className="p-6 max-w-xl mx-auto">
      {/* Header */}
      <div className="mb-8">
        <h1 className="text-xl font-semibold text-white">New Project</h1>
        <p className="text-sm text-gray-500 mt-1">
          Step {wizardStep + 1} of {wizardSteps.length}
        </p>
      </div>

      {/* Step indicator */}
      <div className="flex items-center gap-1 mb-8">
        {wizardSteps.map((s, i) => (
          <div key={s} className="flex items-center gap-1 flex-1">
            <button
              onClick={() => i < wizardStep && setWizardStep(i)}
              className={`flex items-center gap-2 text-xs font-medium transition-colors ${
                i === wizardStep
                  ? 'text-indigo-400'
                  : i < wizardStep
                    ? 'text-gray-400 cursor-pointer hover:text-gray-300'
                    : 'text-gray-700 cursor-default'
              }`}
            >
              <span className={`w-5 h-5 rounded-full flex items-center justify-center text-[10px] font-semibold border ${
                i === wizardStep
                  ? 'border-indigo-500 bg-indigo-500/20 text-indigo-300'
                  : i < wizardStep
                    ? 'border-gray-600 bg-gray-800 text-gray-400'
                    : 'border-gray-800 text-gray-700'
              }`}>
                {i < wizardStep ? '\u2713' : i + 1}
              </span>
              <span className="hidden sm:inline">{s}</span>
            </button>
            {i < wizardSteps.length - 1 && (
              <div className={`flex-1 h-px mx-1 ${i < wizardStep ? 'bg-gray-700' : 'bg-gray-900'}`} />
            )}
          </div>
        ))}
      </div>

      {/* Step content */}
      <div className="space-y-4 min-h-[200px]">
        {wizardStep === 0 && (
          <ProjectGeneralTab
            name={name} setName={setName}
            description={description} setDescription={setDescription}
            iconUrl={iconUrl} setIconUrl={setIconUrl}
            aiProviderId={aiProviderId} setAiProviderId={setAiProviderId}
            activeProviders={activeProviders}
          />
        )}

        {wizardStep === 1 && (
          <ProjectRepoTab
            repoUrl={repoUrl} setRepoUrl={setRepoUrl}
            defaultBranch={defaultBranch} setDefaultBranch={setDefaultBranch}
            gitProviderId={gitProviderId} setGitProviderId={setGitProviderId}
            gitProviderList={gitProviders}
          />
        )}

        {wizardStep === 2 && (
          <ProjectDepsTab
            dependencies={dependencies} setDependencies={setDependencies}
            gitProviderList={gitProviders}
          />
        )}

        {wizardStep === 3 && (
          <div className="space-y-4">
            <p className="text-sm text-gray-400">Review your project configuration before creating.</p>
            <div className="bg-gray-900 border border-gray-800 rounded-lg divide-y divide-gray-800">
              <div className="px-4 py-3">
                <span className="text-[11px] text-gray-600 uppercase tracking-wider">Name</span>
                <p className="text-sm text-white mt-0.5">{name}</p>
                {description && <p className="text-xs text-gray-500 mt-1">{description}</p>}
              </div>
              <div className="px-4 py-3">
                <span className="text-[11px] text-gray-600 uppercase tracking-wider">Repository</span>
                <p className="text-sm text-gray-300 mt-0.5 font-mono">{repoUrl || 'Not configured'}</p>
                {repoUrl && <p className="text-xs text-gray-600 mt-0.5">Branch: {defaultBranch}</p>}
              </div>
              <div className="px-4 py-3">
                <span className="text-[11px] text-gray-600 uppercase tracking-wider">AI Provider</span>
                <p className="text-sm text-gray-300 mt-0.5">
                  {aiProviderId ? activeProviders.find((p) => p.id === aiProviderId)?.display_name || 'Selected' : 'Auto-resolve (default)'}
                </p>
              </div>
              <div className="px-4 py-3">
                <span className="text-[11px] text-gray-600 uppercase tracking-wider">Dependencies</span>
                {dependencies.filter((d) => d.name.trim()).length > 0 ? (
                  <div className="flex flex-wrap gap-1.5 mt-1.5">
                    {dependencies.filter((d) => d.name.trim()).map((d, i) => (
                      <span key={i} className="px-2 py-0.5 bg-gray-800 rounded text-[11px] text-gray-400">{d.name}</span>
                    ))}
                  </div>
                ) : (
                  <p className="text-sm text-gray-600 mt-0.5">None</p>
                )}
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Error */}
      {error && (
        <div className="mt-4 px-4 py-2.5 bg-red-500/10 border border-red-500/20 rounded-lg text-red-400 text-sm">{error}</div>
      )}

      {/* Navigation */}
      <div className="flex items-center justify-between mt-8 pt-4 border-t border-gray-900">
        <button
          onClick={() => wizardStep === 0 ? navigate('/') : setWizardStep(wizardStep - 1)}
          className="px-4 py-2 text-sm text-gray-400 hover:text-gray-300 transition-colors"
        >
          {wizardStep === 0 ? 'Cancel' : 'Back'}
        </button>
        {wizardStep < wizardSteps.length - 1 ? (
          <button
            onClick={() => { setError(''); setWizardStep(wizardStep + 1) }}
            disabled={!canNext}
            className="px-5 py-2 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 rounded-lg text-sm font-medium text-white transition-colors"
          >
            Next
          </button>
        ) : (
          <button
            onClick={handleSave}
            disabled={saving}
            className="px-5 py-2 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 rounded-lg text-sm font-medium text-white transition-colors"
          >
            {saving ? 'Creating...' : 'Create Project'}
          </button>
        )}
      </div>
    </div>
  )
}
